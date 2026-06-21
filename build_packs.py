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
# Sibling of work/ — holds artist-contributed sprite overrides that
# pre-empt the CDN fetch in [build_style]. Currently used for RetroNC's
# gen5 ZA Mega series (CREDITS.md).
REPO_ROOT = Path(__file__).resolve().parent

# Manually-bumped pack revision. Read from PACK_VERSION at repo root,
# embedded as a top-level VERSION file inside every style ZIP, and
# compared against the app's hard-coded kLatestSpritePackVersion at
# install time to decide whether to nag the user to re-download.
# Bump this only when the released packs contain content that the
# current app build needs (new shinies, new Pokémon, etc.) — nightly
# Showdown CDN catch-ups that don't change the on-disk sprite tree
# should NOT bump this; the workflow re-publishes 'latest' every
# build, but VERSION stays put unless this file changes.
PACK_VERSION_FILE = Path('PACK_VERSION')


def read_pack_version() -> str:
    if not PACK_VERSION_FILE.exists():
        # Default to "0" so legacy environments still produce a valid
        # ZIP — the app treats "0" as a mismatch against its
        # current-version constant and shows the update nag.
        return '0'
    return PACK_VERSION_FILE.read_text(encoding='utf-8').strip()

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
    """Every Pokémon in damage-calc's dex that has a BW-style sprite
    on Showdown's gen5 CDN — gen1-5 Game-Freak originals AND the
    X/Y Sprite Project community remakes of post-gen5 content
    (Megas, Alolan/Hisuian/Galarian/Paldean forms, gen6+ base
    species, Primal Reversion, …).

    The X/Y Sprite Project lead has granted permission to ship
    their work as long as the credit page (assets/sprite_credits.json,
    surfaced via the in-app About → Sprite Credits dialog) names
    the project and its lead artists, which it does. Earlier
    revisions of this function excluded those entries on a
    conservative pending-permission read; now we just ship every
    name the dex knows about.

    The 'gen15' name is kept for the workflow's existing `STYLES`
    entry, even though the scope is no longer literally gen1-5 only.
    The BW package's name still reflects the visual style, which is
    what matters to the user."""
    return collect_names_all()


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


# ── champout fallback ────────────────────────────────────────────────
# Showdown's main CDN (`play.pokemonshowdown.com/sprites/...`) doesn't
# carry Pokémon Champions' newly added Mega forms (Mega Raichu X/Y,
# Mega Garchomp Z, the M-B re-enabled megas Showdown didn't have
# 3D models for yet, etc.). The smogon/sprites repo DOES — under
# `src/champions/s{id}.png`, with `id = (dex << 5) | forme_idx` and
# `forme_idx` taken from the species' `formeOrder` in
# `pokemon-showdown/data/pokedex.ts`. Same encoding both styles use,
# so one fallback covers BW and dex.

_POKEDEX_TS_URL = ('https://raw.githubusercontent.com/smogon/'
                   'pokemon-showdown/master/data/pokedex.ts')
_pokedex_cache: dict[str, tuple[int, list[str]]] | None = None


def _load_showdown_pokedex() -> dict[str, tuple[int, list[str]]]:
    """Return {forme_name_lower: (dex, [formeOrder...])}. Builds once
    from Showdown's pokedex.ts so per-name champout lookups are O(1).
    Forme entries (e.g. `raichumegax`) carry a `baseSpecies` pointer;
    we resolve them to the base species' formeOrder so the champout
    index lookup gives the right `(dex << 5) | idx`."""
    global _pokedex_cache
    if _pokedex_cache is not None:
        return _pokedex_cache
    import urllib.request
    with urllib.request.urlopen(_POKEDEX_TS_URL, timeout=30) as resp:
        text = resp.read().decode('utf-8')

    # Pass 1: collect every block (base + formes), keyed by display name.
    blocks: dict[str, dict] = {}  # name_lower -> {dex, baseSpecies, formeOrder}
    for m in re.finditer(
        r'^\t([a-z0-9]+):\s*\{(.*?)^\t\},?$', text, re.DOTALL | re.MULTILINE,
    ):
        body = m.group(2)
        name_m = re.search(r'name:\s*"([^"]+)"', body)
        num_m = re.search(r'num:\s*(-?\d+)', body)
        if not name_m or not num_m:
            continue
        base_m = re.search(r'baseSpecies:\s*"([^"]+)"', body)
        fo_m = re.search(r'formeOrder:\s*\[([^\]]+)\]', body)
        blocks[name_m.group(1).lower()] = {
            'dex': int(num_m.group(1)),
            'base': (base_m.group(1).lower() if base_m
                     else name_m.group(1).lower()),
            'formes': (
                [s.strip().strip('"') for s in fo_m.group(1).split(',')
                 if s.strip()]
                if fo_m else [name_m.group(1)]
            ),
        }

    # Pass 2: for each block, resolve its formeOrder by walking back to
    # the base entry (which is the one that owns the formeOrder list).
    out: dict[str, tuple[int, list[str]]] = {}
    for name, info in blocks.items():
        base_block = blocks.get(info['base'], info)
        formes = base_block['formes']
        # Dex# comes from the base species — all formes share it.
        out[name] = (base_block['dex'], formes)

    _pokedex_cache = out
    return out


def _to_showdown_forme(name: str) -> str:
    """Map damage-calc's display name to Showdown's pokedex.ts forme
    name. Examples: 'Mega Raichu X' → 'Raichu-Mega-X', 'Heat Rotom'
    → 'Rotom-Heat', 'Alolan Ninetales' → 'Ninetales-Alola'."""
    n = name.strip()
    m = re.fullmatch(r'Mega (\w+) ([XYZ])', n)
    if m: return f'{m.group(1)}-Mega-{m.group(2)}'
    m = re.fullmatch(r'Mega (\w+)', n)
    if m: return f'{m.group(1)}-Mega'
    m = re.fullmatch(r'Primal (\w+)', n)
    if m: return f'{m.group(1)}-Primal'
    m = re.fullmatch(r'(Heat|Wash|Frost|Fan|Mow) Rotom', n)
    if m: return f'Rotom-{m.group(1)}'
    regional_to_sd = {
        'Alolan': 'Alola',
        'Hisuian': 'Hisui',
        'Galarian': 'Galar',
        'Paldean': 'Paldea',
    }
    for pre, suf in regional_to_sd.items():
        if n.startswith(pre + ' '):
            rest = n[len(pre) + 1:]
            nested = re.fullmatch(r'(\w+) \(([^)]+)\)', rest)
            if nested:
                return f'{nested.group(1)}-{suf}-{nested.group(2).replace(" ", "")}'
            return f'{rest}-{suf}'
    # "Pokemon (Form)" → "Pokemon-Form" (e.g. "Gourgeist (Large Size)")
    m = re.fullmatch(r'([\w\.\-\' ]+?) \(([^)]+)\)', n)
    if m:
        species = m.group(1).strip()
        inner = m.group(2)
        # Strip noise words that the showdown forme names omit
        meaningful = [w for w in inner.split() if w not in NOISE_FORM_WORDS]
        slug = ''.join(meaningful or [inner])
        return f'{species}-{slug}'
    return n


def champout_id(name: str) -> Optional[int]:
    """Return the `src/champions/s{id}` integer for [name], or None
    if the species isn't in Showdown's pokedex (so no champions
    sprite candidate exists)."""
    forme = _to_showdown_forme(name).lower()
    pd = _load_showdown_pokedex()
    entry = pd.get(forme)
    if entry is None:
        return None
    dex, formes = entry
    formes_lower = [f.lower() for f in formes]
    if forme not in formes_lower:
        return None
    return (dex << 5) | formes_lower.index(forme)


_CHAMPOUT_BASE = ('https://raw.githubusercontent.com/smogon/sprites/'
                  'master/src/champions')


def download_champout(name: str, out: Path, shiny: bool = False) -> bool:
    """Try the champout sprite (raw.github smogon/sprites src/champions
    folder) for [name]. No-op when champout doesn't have this species."""
    sid = champout_id(name)
    if sid is None:
        return False
    suffix = '-s' if shiny else ''
    return download(f'{_CHAMPOUT_BASE}/s{sid}{suffix}.png', out)


def _looks_like_pixel_art(path: Path) -> bool:
    """True when a PNG is genuine low-palette pixel art (real BW or
    X/Y Sprite Project remake), False when it's an auto-downscaled
    HOME render.

    Showdown's gen5 CDN serves "best available" for every key — for
    new Champions megas (Mega Staraptor, Mega Pyroar, the ZA-era
    Raichu Mega X/Y, etc.) the X/Y Sprite Project hasn't shipped
    pixel art yet, so the CDN returns a smooth downscaled HOME
    render under the same gen5 path. We don't want those in the
    BW pack — they look out of place next to genuine 16-colour
    pixel sprites and the user would rather see a poké-ball than
    a mismatched render.

    Signal is unambiguous: real pixel art is palette-indexed
    (PIL mode 'P') with ~15 unique colours, while downscaled
    renders are RGBA with 1,000+ unique colours. The 64-colour
    fallback covers any X/Y Project sprite that happens to be
    saved as RGBA — none in today's sample, but cheap insurance."""
    try:
        from PIL import Image
        img = Image.open(path)
        if img.mode == 'P':
            return True
        # RGBA / RGB sample — count unique colours.
        colors = len(set(img.convert('RGBA').getdata()))
        return colors <= 64
    except Exception:
        # Unreadable → don't treat as pixel art; better to drop it
        # than ship a broken sprite.
        return False


def build_style(style_key: str, sd_dir: str, ext: str, names: list[str]) -> int:
    """Download every sprite for one style into work/<style>/, returning
    the count of successful files.

    Primary source is `play.pokemonshowdown.com/sprites/<sd_dir>/<key>.<ext>`.
    When that 404s, fall back to the champout (raw.github
    smogon/sprites/src/champions/) sprite for that species — the
    fallback covers Mega forms and Champions M-B additions that
    Showdown's main CDN doesn't (yet) have a kebab-named copy of.
    Champout sprites are PNG only, so when [ext] != 'png' the
    fallback is skipped (animated GIFs would need a different
    source).

    For the bw style only, every PNG goes through
    [_looks_like_pixel_art] after download — when Showdown's gen5
    CDN serves an auto-downscaled HOME render for a key that
    doesn't have a genuine X/Y Sprite Project remake yet, we drop
    it rather than ship a smooth mismatched sprite among real
    16-colour pixel art.

    Manual overrides live in `manual_sprites/<style>/<key>.<ext>` at
    the repo root — when a file exists there it pre-empts the CDN
    fetch entirely. This is how community-contributed BW sprites the
    X/Y Sprite Project hasn't merged yet (e.g. RetroNC's gen5
    versions of ZA Megas) land in the pack; see CREDITS.md for the
    artists and their attribution requirements."""
    out_dir = WORK_DIR / style_key
    if out_dir.exists():
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True)

    pairs: list[tuple[str, str]] = []  # (name, key)
    for n in names:
        pairs.append((n, sprite_key(n)))

    base = f'https://play.pokemonshowdown.com/sprites/{sd_dir}'
    ok = 0

    # The shiny variant of each style runs build_style again with a
    # `-shiny` suffix on the Showdown CDN dir — detect that here so
    # the champout fallback fetches the matching shiny `s{id}-s.png`.
    fetching_shiny = sd_dir.endswith('-shiny')

    bw_pixel_gate = (style_key == 'bw')

    # manual_sprites/<style>/ (and /<style>-shiny/ for shiny passes)
    # holds artist-contributed overrides. Files here pre-empt the CDN
    # entirely and skip the pixel-art gate — they were curated by
    # hand, so we trust them.
    manual_subdir = f'{style_key}-shiny' if fetching_shiny else style_key
    manual_dir = REPO_ROOT / 'manual_sprites' / manual_subdir

    def fetch_one(name_key: tuple[str, str]) -> bool:
        n, k = name_key
        dst = out_dir / f'{k}.{ext}'
        manual_src = manual_dir / f'{k}.{ext}'
        if manual_src.exists():
            shutil.copy(manual_src, dst)
            return True
        url = f'{base}/{k}.{ext}'
        if download(url, dst):
            if bw_pixel_gate and not _looks_like_pixel_art(dst):
                # Auto-downscaled HOME render leaked through gen5 CDN —
                # drop it so the slot stays a poké-ball instead of an
                # ugly mismatch. Champout fallback would serve the same
                # kind of smooth render, so skip that too.
                dst.unlink()
                return False
            return True
        # Fallback to champout (PNG only — animated GIFs aren't there).
        # Champout is HOME-render art too, so the BW pack never accepts it.
        if ext.lower() == 'png' and not bw_pixel_gate:
            return download_champout(n, dst, shiny=fetching_shiny)
        return False

    with ThreadPoolExecutor(max_workers=24) as ex:
        for got in ex.map(fetch_one, pairs):
            if got:
                ok += 1
    return ok


def build_trainers() -> int:
    """Download every Showdown trainer sprite into work/trainers/,
    one PNG per key. Trainers aren't style-specific, so the same
    set gets bundled into every per-style ZIP under `trainers/`.

    The key list comes from damage-calc's lib/data/trainer_keys.dart
    (1455 entries; the canonical curated set the app's trainer-card
    dialog picker draws from). We fetch that file via HTTPS, parse
    the single-quoted string literals, then pull each
    `play.pokemonshowdown.com/sprites/trainers/<key>.png`.

    Note on source: the trainer art lives on Showdown's CDN only —
    `smogon/pokemon-showdown-client` keeps the `sprites/trainers/`
    directory mostly empty in git (just an index.php), so we can't
    sparse-clone our way to it. The earlier sparse-clone version of
    this function shipped 0 trainers because of that."""
    import urllib.request

    trainers_dir = WORK_DIR / 'trainers'
    if trainers_dir.exists():
        shutil.rmtree(trainers_dir)
    trainers_dir.mkdir(parents=True)

    keys_url = ('https://raw.githubusercontent.com/Lerisia/damage-calc/'
                'main/lib/data/trainer_keys.dart')
    with urllib.request.urlopen(keys_url, timeout=30) as resp:
        keys_src = resp.read().decode('utf-8')
    keys = re.findall(r"'([^']+)'", keys_src)
    if not keys:
        print('  WARN: trainer_keys.dart parsed 0 keys — skipping')
        return 0

    base = 'https://play.pokemonshowdown.com/sprites/trainers'

    def fetch_one(k: str) -> bool:
        return download(f'{base}/{k}.png', trainers_dir / f'{k}.png')

    ok = 0
    with ThreadPoolExecutor(max_workers=24) as ex:
        for got in ex.map(fetch_one, keys):
            if got:
                ok += 1
    return ok


def zip_style(style_key: str) -> Path:
    """ZIP the style's sprite files at the top level, the shiny
    variants under `shiny/`, the box-icon files under `icons/`, and
    the shared trainer sprites under `trainers/`. Bundling
    everything into a single per-style ZIP means the user only
    manages one download per style — the app extracts all groups
    in one go and the user never has to think about shiny / box
    icons / trainer sprites as separate assets.

    Shiny lives at `work/<style>/shiny/` (not `work/<style>-shiny/`)
    so the workflow's existing `cp -r work/<style>/. sprites/<style>/`
    carries the shiny subdir along to the jsDelivr staging tree
    without needing a workflow-yml change. Trainers live at the
    shared `work/trainers/` since they're identical across styles
    — both bw.zip and dex.zip get a copy embedded under trainers/."""
    import zipfile
    src = WORK_DIR / style_key
    shiny_src = src / 'shiny'
    icons_src = WORK_DIR / 'icons'
    trainers_src = WORK_DIR / 'trainers'
    dst = PACKS_DIR / f'{style_key}.zip'
    PACKS_DIR.mkdir(exist_ok=True)
    pack_version = read_pack_version()
    with zipfile.ZipFile(dst, 'w', zipfile.ZIP_DEFLATED,
                         compresslevel=6) as zf:
        # Top-level VERSION marker. The app extracts this into the
        # per-style cache dir at install time and compares it against
        # its bundled kLatestSpritePackVersion to decide whether to
        # show the update-available nag.
        zf.writestr('VERSION', pack_version + '\n')
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
        if trainers_src.exists():
            for f in sorted(trainers_src.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f'trainers/{f.name}')
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
    pack_version = read_pack_version()
    print(f'All Pokémon names: {len(all_names)}')
    print(f'BW credited scope (gen1-5 ROM rip + Smogon-project-credited): '
          f'{len(bw_credited_names)}')
    print(f'Embedding PACK_VERSION="{pack_version}" into every style ZIP.')
    # Trainers are shared across styles — build once, embed in every
    # per-style ZIP under trainers/.
    print('\n== trainers (sparse-clone from pokemon-showdown-client) ==')
    n_trainers = build_trainers()
    print(f'  trainer sprites: {n_trainers}')
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
