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
    {id → {'num': int, 'forme': str|None, 'isNonstandard': str|None}}.

    The isNonstandard field is how Showdown marks speculative /
    community-extension entries vs ones that have appeared in
    real games:
      * None   — currently legal in some format (real Pokémon)
      * "Past" — was in real games but isn't legal now (e.g.,
                 every Mega Evolution post-SwSh, ORAS-only formes)
      * "Future" — fan-/community-speculation, not in any real
                   game yet (Mega Excadrill, Mega Barbaracle etc.)
      * "CAP"  — Smogon's Create-A-Pokémon project

    For 40×30 icons we want the union of {None, "Past"} — those
    had real-game icons. Future / CAP are community-drawn extras
    on the icon sheet and we don't redistribute those."""
    out: dict[str, dict] = {}
    # Brace-balanced extraction so we capture the FULL entry body
    # — earlier regex stopped at the next num: pattern and missed
    # isNonstandard / requiredItem fields that come later.
    i = 0
    while True:
        m = re.search(r'(\w+):\{num:(-?\d+),', js_source[i:])
        if not m:
            break
        key = m.group(1)
        num = int(m.group(2))
        # m.start() / m.end() are relative to js_source[i:]; absolute
        # position of the '{' that opens the entry body is
        # (i + m.start()) + len(key) + 1 (the colon between key
        # and the brace).
        body_start = i + m.start() + len(key) + 1
        depth = 0
        j = body_start
        while j < len(js_source):
            if js_source[j] == '{':
                depth += 1
            elif js_source[j] == '}':
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = js_source[body_start:j + 1]
        forme_m = re.search(r'forme:"([^"]+)"', body)
        ns_m = re.search(r'isNonstandard:"([^"]+)"', body)
        out[key] = {
            'num': num,
            'forme': forme_m.group(1) if forme_m else None,
            'isNonstandard': ns_m.group(1) if ns_m else None,
        }
        i = j + 1
    return out


def icon_index(name: str, overrides: dict[str, int],
               pokedex_entries: dict[str, dict]) -> int | None:
    """Resolve a Pokémon's icon index using Showdown's lookup chain.

    Inclusion rules:
      - Must exist in Showdown's BattlePokedex (filters out
        Champions-original Megas and similar fan entries we have in
        mega.json but Showdown doesn't ship icons for).
      - Base species num must be 1-1025 (gen1-9; future-gen entries
        the Showdown sheet hasn't added yet just won't resolve).
      - isNonstandard ∈ {None, "Past"} — drops Future (community-
        speculation Megas like Mega Excadrill, Mega Barbaracle) and
        CAP (Smogon Create-A-Pokémon) entries that have positions on
        the sheet but aren't real Pokémon.

    Scope basis:
      - gen1-7 icons + their forms (Megas, Alolan, Primal, etc.) are
        Game Freak ROM rips from Sun/Moon.
      - gen8+ icons + forms (Galarian, Hisuian, Paldean, Gigantamax,
        Eternamax) are community-extension work by the msikma/
        pokesprite project, which is MIT-licensed. Sprite IP is still
        Game Freak / Nintendo, but the redistribution license is
        permissive ("Feel free to use PokéSprite in your own projects
        or to create derivative works. We appreciate it if you credit
        the project, but it's not required."). So community-extension
        forms are in scope alongside the official ROM-rip ones.

    Position lookup:
      - BattlePokemonIconIndexes override first (forms with their own
        icon position — Megas, Alolan, Primal, regional variants
        across all gens, formes that don't share the base species'
        slot).
      - Fall back to BattlePokedex base num (plain species)."""
    sid = to_id(name)
    entry = pokedex_entries.get(sid)
    if entry is None:
        return None
    if not (1 <= entry['num'] <= 1025):
        return None
    if entry['isNonstandard'] not in (None, 'Past'):
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
