#!/usr/bin/env python3
"""Slice Showdown's pokemonicons-sheet.png into individual 40×30 PNGs.

Showdown serves the gen7-style box icons as a single 12-column
sprite sheet, with each Pokémon assigned an icon index. The index
is `BattlePokedex[id].num` for base species, overridden by
`BattlePokemonIconIndexes[id]` for forms that have their own icon
(Pikachu cosplay variants, Unown letters, Mega forms, etc.).

This script:
  1. Pulls pokemonicons-sheet.png from Showdown's CDN.
  2. Pulls battle-dex-data.ts (source of BattlePokemonIconIndexes)
     and pokedex.js (source of base .num) from the showdown-client
     repo / CDN.
  3. Walks damage-calc's Pokémon list, computes each entry's icon
     index via the same lookup, crops 40×30 at (idx % 12 * 40,
     idx // 12 * 30), and writes the crop as <key>.png using our
     app's spriteKeyFor naming.
  4. Bundles all crops into packs/icons.zip alongside bw/dex packs.

The box icons are direct rips of Game Freak's box-UI art (gen7+),
same provenance class as dex.zip's HOME 3D models — no Smogon
Sprite Project / X/Y Project community involvement, so the pack
covers all Pokémon (no gen1-5 scope restriction)."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from PIL import Image

# Re-use the same spriteKey + naming logic as build_packs.py.
sys.path.insert(0, str(Path(__file__).parent))
from build_packs import sprite_key, collect_names_all  # noqa: E402

DATA_DIR = Path('data')
PACKS_DIR = Path('packs')
WORK_DIR = Path('work')
SHEET_URL = 'https://play.pokemonshowdown.com/sprites/pokemonicons-sheet.png'
DEX_DATA_URL = ('https://raw.githubusercontent.com/smogon/'
                'pokemon-showdown-client/master/'
                'play.pokemonshowdown.com/src/battle-dex-data.ts')
POKEDEX_URL = 'https://play.pokemonshowdown.com/data/pokedex.js'

ICON_W = 40
ICON_H = 30
COLS = 12


def fetch(url: str) -> bytes:
    r = subprocess.run(
        ['curl', '-sSL', '--max-time', '60', url],
        capture_output=True, check=True,
    )
    return r.stdout


def to_id(name: str) -> str:
    """Showdown's BattlePokedex / BattlePokemonIconIndexes id for a
    Pokémon's display name. Mirrors PS's internal key derivation:
    species first, then the form suffix joined without separator —
    'Mega Charizard X' → 'charizardmegax', not 'megacharizardx'.
    Reuse spriteKeyFor's classification logic to get the parts in
    the right order, then strip the hyphen we use for our own keys."""
    return sprite_key(name).replace('-', '')


def parse_icon_indexes(ts_source: str) -> dict[str, int]:
    """Pull BattlePokemonIconIndexes (and the Left variant we don't
    need) out of the TS source. Entries look like:
        pikachubelle: 1032 + 2,
        venusaurmega: 1500,
        cresceidonmega: 1532 + 5,
    so we tokenise on commas inside the object body and parse each."""
    m = re.search(
        r'BattlePokemonIconIndexes:\s*\{[^}]*?\}\s*=\s*\{(.*?)\n\}\s*;',
        ts_source, flags=re.DOTALL,
    )
    if not m:
        # Fallback: looser regex without the type annotation.
        m = re.search(
            r'BattlePokemonIconIndexes[^=]*=\s*\{(.*?)\n\}\s*;',
            ts_source, flags=re.DOTALL,
        )
    if not m:
        raise RuntimeError('BattlePokemonIconIndexes not found in source')
    body = m.group(1)
    out: dict[str, int] = {}
    # Strip JS-style // comments before parsing entries.
    body_no_comments = re.sub(r'//[^\n]*', '', body)
    for entry in re.finditer(
            r'(\w+)\s*:\s*([\d+\- *()]+?)\s*,', body_no_comments):
        key = entry.group(1)
        expr = entry.group(2).strip()
        try:
            # Entries are simple arithmetic like '1032 + 7'. eval is
            # safe here because we restrict the expression alphabet
            # via the regex above (digits/operators only).
            value = eval(expr, {'__builtins__': {}}, {})
            out[key] = int(value)
        except Exception:
            continue
    return out


def parse_pokedex_nums(js_source: str) -> dict[str, int]:
    """Pull the `num` field for each *base* species from Showdown's
    pokedex.js. Entries look like:
        pikachu:{num:25,name:"Pikachu",types:[...]}
        venusaurmega:{num:3,name:"Venusaur-Mega",baseSpecies:"Venusaur",forme:"Mega",...}
    Form entries (those with a baseSpecies field) inherit their
    base's num and would just duplicate the base species' icon if
    we included them — Showdown's icon sheet for the form key
    points to the base position. We skip them here so the pack
    contains one icon per base species, and the app's
    base-species fallback chain shows the base icon for any form
    request."""
    out: dict[str, int] = {}
    # Match the full entry up to its closing brace so we can detect
    # baseSpecies inside it. Use a non-greedy match bounded by the
    # next `,name:` (which always opens a new entry) to avoid
    # crossing entry boundaries.
    for m in re.finditer(
            r'(\w+):\{num:(-?\d+),(.*?)(?=\}\,\w+:\{num:|\}\;?$)',
            js_source):
        key, num, body = m.group(1), int(m.group(2)), m.group(3)
        if 'baseSpecies:' in body:
            continue  # form, would duplicate base
        out[key] = num
    return out


def icon_index(name: str, overrides: dict[str, int],
               pokedex: dict[str, int]) -> int | None:
    """Resolve a Pokémon's icon index — restricted to gen1-7 base
    species (num 1-809) only.

    Background: 40×30 box icons are the gen6-7 (Sun/Moon era) official
    Game Freak style. From gen8 onwards Game Freak switched to 68×56
    icons (Pokémon HOME's actual format), so the 40×30 icons Showdown
    serves for gen8+ Pokémon were drawn by community projects
    (msikma/pokesprite issue #72 and similar) to fill the gap. We
    don't redistribute that community work — gen8+ Pokémon fall
    through to the app's base-species fallback chain (which shows
    the base BW / HOME sprite scaled down).

    The override table (BattlePokemonIconIndexes) is also ignored
    here for the same reason: it mixes official Mega/regional icons
    (gen6-7) with community-drawn ones (ZA Megas, CAP, etc.) and
    we can't separate them by code alone."""
    sid = to_id(name)
    if sid in pokedex:
        num = pokedex[sid]
        # Gen 1-7 covers num 1..809; gen8 starts at 810.
        if 1 <= num <= 809:
            return num
    return None


def main() -> int:
    print('Fetching pokemonicons-sheet.png...')
    sheet_bytes = fetch(SHEET_URL)
    print(f'  {len(sheet_bytes)} bytes')
    sheet_path = WORK_DIR / 'pokemonicons-sheet.png'
    WORK_DIR.mkdir(exist_ok=True)
    sheet_path.write_bytes(sheet_bytes)
    sheet = Image.open(sheet_path).convert('RGBA')
    print(f'  sheet dimensions: {sheet.width}×{sheet.height}')

    print('Fetching battle-dex-data.ts...')
    dex_ts = fetch(DEX_DATA_URL).decode('utf-8', errors='replace')
    overrides = parse_icon_indexes(dex_ts)
    print(f'  override entries: {len(overrides)}')

    print('Fetching pokedex.js...')
    pokedex_js = fetch(POKEDEX_URL).decode('utf-8', errors='replace')
    pokedex_nums = parse_pokedex_nums(pokedex_js)
    print(f'  pokedex entries: {len(pokedex_nums)}')

    out_dir = WORK_DIR / 'icons'
    if out_dir.exists():
        for f in out_dir.iterdir():
            f.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(collect_names_all())
    print(f'\nSlicing icons for {len(names)} Pokémon names...')
    ok, miss = 0, []
    for name in names:
        idx = icon_index(name, overrides, pokedex_nums)
        if idx is None:
            miss.append(name)
            continue
        col, row = idx % COLS, idx // COLS
        x0, y0 = col * ICON_W, row * ICON_H
        if y0 + ICON_H > sheet.height or x0 + ICON_W > sheet.width:
            miss.append(name)
            continue
        crop = sheet.crop((x0, y0, x0 + ICON_W, y0 + ICON_H))
        key = sprite_key(name)
        crop.save(out_dir / f'{key}.png', 'PNG', optimize=True)
        ok += 1
    print(f'  sliced: {ok} / {len(names)}')
    if miss:
        print(f'  missing icon index (will fall back to pokéball): '
              f'{len(miss)}')
        for n in miss[:10]:
            print(f'    - {n}')

    # ZIP the icons folder. shutil.make_archive matches build_packs.py.
    import shutil
    PACKS_DIR.mkdir(exist_ok=True)
    zip_base = PACKS_DIR / 'icons'
    shutil.make_archive(str(zip_base), 'zip', root_dir=out_dir)
    zip_path = zip_base.with_suffix('.zip')
    print(f'\nicons.zip: {zip_path.stat().st_size / 1024:.1f} KB')
    return 0


if __name__ == '__main__':
    sys.exit(main())
