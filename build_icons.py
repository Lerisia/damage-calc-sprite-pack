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


def parse_pokedex_entries(js_source: str) -> dict[str, dict]:
    """Return every entry in Showdown's pokedex.js as
    {id → {'num': int, 'forme': str|None}}. Includes form variants
    (entries with baseSpecies) because we now want to keep those
    that have their own icon position (Megas, Primal, Alolan,
    gen3-7 formes) and only skip the gen8+ ones via the forme
    field."""
    out: dict[str, dict] = {}
    for m in re.finditer(
            r'(\w+):\{num:(-?\d+),(.*?)(?=\}\,\w+:\{num:|\}\;?$)',
            js_source):
        key, num, body = m.group(1), int(m.group(2)), m.group(3)
        forme_m = re.search(r'forme:"([^"]+)"', body)
        out[key] = {
            'num': num,
            'forme': forme_m.group(1) if forme_m else None,
        }
    return out


# Forme strings introduced in gen8+ — we exclude these because
# their 40×30 icons are community-extension work (Showdown's icon
# sheet was extended past the gen7 SuMo official art). Forme
# values from Showdown's pokedex use exactly these prefixes.
GEN8_PLUS_FORME_PREFIXES = (
    'Hisui',          # gen8 PLA
    'Galar',          # gen8 SwSh (Galar / Galar-Zen)
    'Paldea',         # gen9 SV (Paldea-Combat / -Blaze / -Aqua)
    'Gmax',           # gen8 Gigantamax
    'Eternamax',      # gen8 Eternatus
)


def icon_index(name: str, overrides: dict[str, int],
               pokedex_entries: dict[str, dict]) -> int | None:
    """Resolve a Pokémon's icon index using Showdown's lookup chain
    while restricting to forms that had an official 40×30 icon in
    gen6-7 games.

    Inclusion rules:
      - Must exist in Showdown's BattlePokedex (filters out
        Champions-original Megas and similar fan entries we have
        in mega.json but Showdown doesn't).
      - Base species num must be 1-809 (gen1-7 — base gen8+ species
        don't have 40×30 art).
      - Form's `forme` field must NOT start with any gen8+ prefix
        (Hisui / Galar / Paldea / Gmax / Eternamax) — those are
        community work because the 40×30 sheet stopped getting
        official additions after gen7.

    Position lookup:
      - BattlePokemonIconIndexes override first (forms with their
        own icon position — Megas, Alolan, Primal, gen3-7 formes
        all sit here, pointing to the official gen-7 sheet
        positions).
      - Fall back to BattlePokedex base num (plain species)."""
    sid = to_id(name)
    entry = pokedex_entries.get(sid)
    if entry is None:
        return None
    if not (1 <= entry['num'] <= 809):
        return None
    forme = entry['forme']
    if forme is not None:
        for prefix in GEN8_PLUS_FORME_PREFIXES:
            if forme.startswith(prefix):
                return None
    if sid in overrides:
        return overrides[sid]
    return entry['num']


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
    pokedex_entries = parse_pokedex_entries(pokedex_js)
    print(f'  pokedex entries: {len(pokedex_entries)}')

    out_dir = WORK_DIR / 'icons'
    if out_dir.exists():
        for f in out_dir.iterdir():
            f.unlink()
    out_dir.mkdir(parents=True, exist_ok=True)

    names = sorted(collect_names_all())
    print(f'\nSlicing icons for {len(names)} Pokémon names...')
    ok, miss = 0, []
    for name in names:
        idx = icon_index(name, overrides, pokedex_entries)
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
