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
#  * 'gen15' — gen1-5 base species + forms whose base is gen1-5. This
#              limits us to Pokémon that had real BW sprites in the
#              original Gen-5 games, plus their official formes; it
#              excludes all gen6+ species, all Megas (a gen6 mechanic,
#              and the BW-style art is the X/Y Sprite Project's
#              community work), and all post-gen5 regional variants
#              (Alolan/Galarian/Hisuian/Paldean — also X/Y Project
#              community work). We avoid redistributing X/Y Sprite
#              Project content until Layell explicitly OKs it.
#
# 'ani' is intentionally omitted from this list — the animated GIFs
# for gen6+ are also community-extension work whose provenance we
# can't separate cleanly, so we don't ship that pack at all yet.
STYLES = [
    ('bw',  'gen5', 'png', 'gen15'),
    ('dex', 'dex',  'png', 'all'),
]

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
    src = WORK_DIR / style_key
    dst = PACKS_DIR / f'{style_key}.zip'
    PACKS_DIR.mkdir(exist_ok=True)
    # store=0/deflate=8 — sprites are already PNG/GIF compressed, so
    # the deflate gain is small; default level keeps build time short.
    shutil.make_archive(str(dst.with_suffix('')), 'zip', root_dir=src)
    return dst


def main() -> int:
    all_names = sorted(collect_names_all())
    gen15_names = sorted(collect_names_gen15())
    print(f'All Pokémon names: {len(all_names)}')
    print(f'Gen 1-5 scope: {len(gen15_names)}')
    for style_key, sd_dir, ext, scope in STYLES:
        names = gen15_names if scope == 'gen15' else all_names
        print(f'\n== {style_key} ({sd_dir}/*.{ext}, scope={scope}, '
              f'targets={len(names)}) ==')
        n_ok = build_style(style_key, sd_dir, ext, names)
        z = zip_style(style_key)
        print(f'  fetched: {n_ok} / {len(names)}')
        print(f'  packed: {z} ({z.stat().st_size / 1024 / 1024:.1f} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
