#!/usr/bin/env python3
"""Build per-style sprite packs by fetching from play.pokemonshowdown.com.

Reads every Pokémon name out of damage-calc's pokemon/*.json files
(downloaded into ./data by the workflow), maps each to its Showdown
sprite slug using the same heuristic as the app's spriteKeyFor (so
the keys inside the ZIP match what the app looks up), and downloads
the corresponding files from Showdown's CDN into per-style
directories that get zipped at the end.

The packs are intentionally just the raw image files at the top
level — no nested directories — so the mobile app's archive
extraction can drop them straight into its per-style cache without
path-walking.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

DATA_DIR = Path('data')
PACKS_DIR = Path('packs')
WORK_DIR = Path('work')

# (style key, Showdown CDN subdir, file extension, scope)
#
# 'scope' decides which Pokémon names get fetched for that style:
#  * 'all'   — every entry in damage-calc's pokedex (~1239 sprites).
#              Used for HOME 3D (dex) where the IP is uniformly Game
#              Freak / Nintendo — same gray-zone redistribution
#              status everywhere, no per-key license filter needed.
#
#  * 'bw_credited' — gen1-5 base species + their gen1-5-era forms
#              (Game Freak ROM rips from BW) ∪ keys credited to the
#              X/Y / Sun/Moon / Sword/Shield Sprite Projects per
#              sprite_credits.json. Filters out non-pixel placeholders
#              that Showdown's community sometimes drops into gen5/
#              for newly-announced Pokémon (e.g. ZA Megas like Mega
#              Feraligatr that have no BW art yet — Showdown puts the
#              official Game Freak ZA illustration there as a stand-
#              in, and we don't want that mixed into our pixel pack).
#
# 'ani' is intentionally omitted from this list — animated GIFs use
# the same Smogon project license, but the source-of-truth project
# threads have a separate audit pending.
STYLES = [
    ('bw',  'gen5', 'png', 'bw_credited'),
    ('dex', 'dex',  'png', 'all'),
]

# Each entry above also gets a shiny companion fetched from
# `<sd_dir>-shiny/` on the same CDN. The shiny files land inside
# the regular style's ZIP at `shiny/<key>.png` (see zip_style),
# so the user downloads one bundle per style and the app picks
# regular vs shiny based on a per-Pokemon flag.

# Per-name overrides — same set as the Dart side. Discovered
# empirically against Showdown's CDN.
OVERRIDES = {
    'Zacian (Crowned Sword)': 'zacian-crowned',
    'Zamazenta (Crowned Shield)': 'zamazenta-crowned',
    'Minior (Core Form)': 'minior',
}

NOISE_FORM_WORDS = {
    'Forme', 'Form', 'Mode', 'Mask', 'Cloak',
    'Size', 'Style', 'Face', 'Flower',
}

REGIONAL = {
    'Alolan': 'alola',
    'Hisuian': 'hisui',
    'Galarian': 'galar',
    'Paldean': 'paldea',
}


def _strip_alnum(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())


def _strip_diacritics(s: str) -> str:
    return s.replace('é', 'e').replace('è', 'e').replace('ê', 'e')


def sprite_key(name: str) -> str:
    """Mirrors the Dart spriteKeyFor — keep in sync."""
    if name in OVERRIDES:
        return OVERRIDES[name]
    n = _strip_diacritics(name).replace('♀', 'f').replace('♂', 'm')

    m = re.fullmatch(r'Mega (\w+) ([XY])', n)
    if m:
        return f'{_strip_alnum(m.group(1))}-mega{m.group(2).lower()}'
    m = re.fullmatch(r'Mega (\w+)', n)
    if m:
        return f'{_strip_alnum(m.group(1))}-mega'
    m = re.fullmatch(r'Primal (\w+)', n)
    if m:
        return f'{_strip_alnum(m.group(1))}-primal'

    onesies = {
        'Ultra Necrozma': 'necrozma-ultra',
        'Hoopa Unbound': 'hoopa-unbound',
        'Dawn Wings Necrozma': 'necrozma-dawnwings',
        'Dusk Mane Necrozma': 'necrozma-duskmane',
        'Ice Rider Calyrex': 'calyrex-ice',
        'Shadow Rider Calyrex': 'calyrex-shadow',
        'Black Kyurem': 'kyurem-black',
        'White Kyurem': 'kyurem-white',
    }
    if n in onesies:
        return onesies[n]

    m = re.fullmatch(r'(Heat|Wash|Frost|Fan|Mow) Rotom', n)
    if m:
        return f'rotom-{m.group(1).lower()}'

    for prefix, slug in REGIONAL.items():
        if n.startswith(prefix + ' '):
            rest = n[len(prefix) + 1:]
            nested = re.fullmatch(r'(\w+) \(([^)]+)\)', rest)
            if nested:
                species = _strip_alnum(nested.group(1))
                forme_word = _strip_alnum(nested.group(2).split()[0])
                return f'{species}-{slug}{forme_word}'
            return f'{_strip_alnum(rest)}-{slug}'

    m = re.fullmatch(r"^(.+?) \(([^)]+)\)$", n)
    if m:
        species = _strip_alnum(m.group(1))
        inner = m.group(2)
        if inner == 'Female':
            return f'{species}-f'
        if inner == 'Male':
            return species
        meaningful = [w for w in inner.split() if w not in NOISE_FORM_WORDS]
        slug = _strip_alnum(''.join(meaningful or [inner]))
        return f'{species}-{slug}'

    return _strip_alnum(n)


def collect_names_all() -> set[str]:
    """Every Pokémon name in the dex — used for 'all'-scope styles."""
    names: set[str] = set()
    for p in sorted(DATA_DIR.glob('*.json')):
        for entry in json.loads(p.read_text(encoding='utf-8')):
            n = entry.get('name')
            if n:
                names.add(n)
    return names


def base_species_name(name: str) -> Optional[str]:
    """Strip form qualifiers to reveal the underlying species — mirror
    of the Dart baseSpeciesName in sprite_service.dart. Used to decide
    whether a forms.json entry belongs in the gen1-5 BW scope by
    looking up the underlying species against gen[1-5].json."""
    n = name.strip()
    m = re.fullmatch(r'Mega (\w+) [XYZ]', n)
    if m: return m.group(1)
    m = re.fullmatch(r'Mega (\w+)', n)
    if m: return m.group(1)
    m = re.fullmatch(r'Primal (\w+)', n)
    if m: return m.group(1)
    if n == 'Ultra Necrozma': return 'Necrozma'
    if n == 'Hoopa Unbound': return 'Hoopa'
    if n in ('Black Kyurem', 'White Kyurem'): return 'Kyurem'
    if n in ('Dawn Wings Necrozma', 'Dusk Mane Necrozma'): return 'Necrozma'
    if n in ('Ice Rider Calyrex', 'Shadow Rider Calyrex'): return 'Calyrex'
    m = re.fullmatch(r'(Heat|Wash|Frost|Fan|Mow) Rotom', n)
    if m: return 'Rotom'
    for prefix in REGIONAL.keys():
        if n.startswith(prefix + ' '):
            rest = n[len(prefix) + 1:]
            nested = re.fullmatch(r'(\w+) \(', rest)
            return nested.group(1) if nested else rest
    m = re.fullmatch(r"([\w\.\-' ]+?) \([^)]+\)", n)
    if m: return m.group(1).strip()
    return None


def collect_names_gen15() -> set[str]:
    """gen1-5 base species + forms whose base species is in gen1-5.

    Forms whose underlying species (per [base_species_name]) is a
    gen1-5 Pokémon get included — Deoxys formes (gen3 Deoxys), Rotom
    appliances (gen4 Rotom), Wormadam cloaks (gen4 Wormadam),
    Black/White Kyurem (gen5 Kyurem), Therian trio (gen5),
    Darmanitan-Zen (gen5), Meloetta-Pirouette (gen5), etc.

    Excludes post-gen5 regional variants and all Megas (the BW
    sprites for these are X/Y Sprite Project community work and
    aren't in scope until that group OKs redistribution)."""
    base_species: set[str] = set()
    for g in (1, 2, 3, 4, 5):
        for entry in json.loads(
                (DATA_DIR / f'gen{g}.json').read_text(encoding='utf-8')):
            n = entry.get('name')
            if n:
                base_species.add(n)
    names: set[str] = set(base_species)
    forms_path = DATA_DIR / 'forms.json'
    if not forms_path.exists():
        return names
    for entry in json.loads(forms_path.read_text(encoding='utf-8')):
        n = entry.get('name')
        if not n:
            continue
        # Skip post-gen5 regional variants outright.
        if any(n.startswith(p + ' ') for p in REGIONAL.keys()):
            continue
        # Skip Primal Groudon/Kyogre — Primal Reversion was added in
        # ORAS (gen6), so the BW sprite is X/Y Sprite Project work,
        # not original BW art. (Mega works the same way, but Megas
        # live in mega.json which we never load for this scope.)
        if n.startswith('Primal '):
            continue
        base = base_species_name(n)
        if base is not None and base in base_species:
            names.add(n)
    return names


def download(url: str, out: Path) -> bool:
    """HEAD-then-GET so 404s don't leave empty files lying around."""
    r = subprocess.run(
        ['curl', '-sS', '-o', str(out), '-w', '%{http_code}',
         '--max-time', '20', url],
        capture_output=True, text=True,
    )
    code = (r.stdout or '').strip()[-3:]
    if code != '200' or not out.exists() or out.stat().st_size < 100:
        if out.exists():
            out.unlink()
        return False
    return True


def build_style(style_key: str, sd_dir: str, ext: str, names: list[str]) -> int:
    """Download every sprite for one style into work/<style>/, returning
    the count of successful files."""
    out_dir = WORK_DIR / style_key
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    pairs: list[tuple[str, str]] = []  # (name, key)
    for n in names:
        pairs.append((n, sprite_key(n)))

    base = f'https://play.pokemonshowdown.com/sprites/{sd_dir}'
    ok = 0

    def fetch_one(name_key: tuple[str, str]) -> bool:
        n, k = name_key
        url = f'{base}/{k}.{ext}'
        dst = out_dir / f'{k}.{ext}'
        return download(url, dst)

    with ThreadPoolExecutor(max_workers=24) as ex:
        for got in ex.map(fetch_one, pairs):
            if got:
                ok += 1
    return ok


def zip_style(style_key: str) -> Path:
    """ZIP the style's sprite files at the top level, the shiny
    variants under `shiny/`, and the box-icon files under `icons/`.
    Bundling everything into a single per-style ZIP means the user
    only manages one download per style — the app extracts all
    groups in one go and the user never has to think about shiny
    vs box icons as separate assets.

    Shiny lives at `work/<style>/shiny/` (not `work/<style>-shiny/`)
    so the workflow's existing `cp -r work/<style>/. sprites/<style>/`
    carries the shiny subdir along to the jsDelivr staging tree
    without needing a workflow-yml change."""
    import zipfile
    src = WORK_DIR / style_key
    shiny_src = src / 'shiny'
    icons_src = WORK_DIR / 'icons'
    dst = PACKS_DIR / f'{style_key}.zip'
    PACKS_DIR.mkdir(exist_ok=True)
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        for f in sorted(src.iterdir()):
            if f.is_file():  # skip the shiny/ subdir entry here
                zf.write(f, arcname=f.name)
        if shiny_src.exists():
            for f in sorted(shiny_src.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f'shiny/{f.name}')
        if icons_src.exists():
            for f in sorted(icons_src.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f'icons/{f.name}')
    return dst


def collect_names_bw_credited() -> set[str]:
    """gen1-5 ROM-rip scope ∪ keys credited to a Smogon Sprite Project.

    Showdown's gen5/ folder contains two unrelated kinds of content:
      1. Real BW pixel art (gen1-5 from Game Freak's BW games + later
         pixel art from the X/Y / Sun/Moon / Sword/Shield Sprite
         Projects).
      2. Stand-in art for newly-announced Pokémon whose BW pixel
         version doesn't exist yet — typically the official Game
         Freak illustration. ZA Megas (Mega Feraligatr, Mega
         Krookodile, etc.) currently sit here.

    Class (1) is licensed; class (2) is outside our verified license
    scope AND visually breaks a pixel pack. Filter by attribution:
    if a sprite_key is in our audited credit data, it's class (1).
    Otherwise we only accept it when it's a Game Freak ROM rip
    (gen1-5 base species or gen1-5-era form)."""
    credits_path = Path('sprite_credits.json')
    credited_keys: set[str] = set()
    if credits_path.exists():
        credits = json.loads(credits_path.read_text(encoding='utf-8'))
        credited_keys = set(credits.get('by_sprite_key', {}).keys())
    rom_rip_names = collect_names_gen15()
    out: set[str] = set(rom_rip_names)
    for n in collect_names_all():
        if sprite_key(n) in credited_keys:
            out.add(n)
    return out


def main() -> int:
    all_names = sorted(collect_names_all())
    bw_credited_names = sorted(collect_names_bw_credited())
    print(f'All Pokémon names: {len(all_names)}')
    print(f'BW credited scope (gen1-5 ROM rip + Smogon-project-credited): '
          f'{len(bw_credited_names)}')
    for style_key, sd_dir, ext, scope in STYLES:
        if scope == 'bw_credited':
            names = bw_credited_names
        else:
            names = all_names
        print(f'\n== {style_key} ({sd_dir}/*.{ext}, scope={scope}, '
              f'targets={len(names)}) ==')
        n_ok = build_style(style_key, sd_dir, ext, names)
        # Shiny companion — same scope as the regular variant. Lives
        # at work/<style>/shiny/ (a subdir of the regular style's
        # work dir) so the workflow's existing
        # `cp -r work/<style>/. sprites/<style>/` propagates it to
        # the jsDelivr staging tree without a workflow edit. Some
        # entries won't exist as shiny upstream (rare niche forms,
        # ZA Megas) and just won't end up in the ZIP — the app's
        # fallback path then renders the regular variant in shiny
        # mode (better than a pokeball).
        n_shiny = build_style(
            f'{style_key}/shiny', f'{sd_dir}-shiny', ext, names)
        z = zip_style(style_key)
        print(f'  fetched: {n_ok} / {len(names)} (regular)')
        print(f'  fetched: {n_shiny} / {len(names)} (shiny)')
        print(f'  packed: {z} ({z.stat().st_size / 1024 / 1024:.1f} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
