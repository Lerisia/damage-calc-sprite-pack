#!/usr/bin/env python3
"""Download Showdown's 24×24 held-item icons into work/items/.

The app needs these to mark held items in list-style views (the speed
tier table's Choice Scarf lines were the first caller). They ride
along inside every style ZIP under `items/`, the same way box icons
and trainer sprites do, so a user manages one download per style.

Item ids come from damage-calc's assets/items.json, fetched over HTTPS
like build_packs.py does for trainer keys. Showdown's naming is
inconsistent — some ids keep their hyphens (`life-orb`), others drop
them (`blackglasses`) — so each id is tried both ways.

Roughly 230 of our ~530 items resolve that way. The rest — Mega
Stones, Z-Crystals, Silvally memories, most gen 8–9 items — have no
standalone file, but they do have a cell in Showdown's item icon sheet
(`itemicons-sheet.png`, 24×24 cells, 16 per row), addressed by the
`spritenum` field in pokemon-showdown's `data/items.ts`. Those are cut
out of the sheet as a second pass, so the item picker shows an icon for
practically every item. Anything Showdown doesn't know at all stays
absent and callers fall back to a placeholder.
"""
from __future__ import annotations

import io
import json
import re
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

WORK_DIR = Path('work')
BASE = 'https://play.pokemonshowdown.com/sprites/itemicons'
SHEET_URL = 'https://play.pokemonshowdown.com/sprites/itemicons-sheet.png'
SHOWDOWN_ITEMS_URL = ('https://raw.githubusercontent.com/smogon/'
                      'pokemon-showdown/master/data/items.ts')
CELL = 24
SHEET_COLUMNS = 16
ITEMS_URL = ('https://raw.githubusercontent.com/Lerisia/damage-calc/'
             'main/assets/items.json')


def item_ids() -> list[str]:
    with urllib.request.urlopen(ITEMS_URL, timeout=30) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    return [e['name'] for e in data if e.get('name')]


def download(url: str, out: Path) -> bool:
    r = subprocess.run(
        ['curl', '-sS', '-o', str(out), '-w', '%{http_code}',
         '--max-time', '20', url],
        capture_output=True, text=True,
    )
    code = (r.stdout or '').strip()[-3:]
    if code != '200' or not out.exists() or out.stat().st_size < 50:
        if out.exists():
            out.unlink()
        return False
    return True


def showdown_id(item_id: str) -> str:
    """Our id → Showdown's: lowercase alphanumerics only. A held
    Z-Crystal is `firium-z--held` here and `firiumz` there."""
    return re.sub(r'[^a-z0-9]', '', item_id.replace('--held', ''))


def showdown_spritenums() -> dict[str, int]:
    """Showdown item id → `spritenum`, parsed from data/items.ts."""
    with urllib.request.urlopen(SHOWDOWN_ITEMS_URL, timeout=60) as resp:
        src = resp.read().decode('utf-8')
    nums: dict[str, int] = {}
    for m in re.finditer(r'\n\t([a-z0-9]+): \{(.*?)\n\t\},', src, re.S):
        sn = re.search(r'spritenum: (\d+)', m.group(2))
        if sn:
            nums[m.group(1)] = int(sn.group(1))
    return nums


def cut_from_sheet(missing: list[str], out_dir: Path) -> int:
    """Second pass: cut [missing] ids out of the icon sheet. Returns
    how many were written."""
    if not missing:
        return 0
    nums = showdown_spritenums()
    # curl, like the standalone icons: Showdown's CDN answers 403 to
    # urllib's default User-Agent.
    sheet_file = WORK_DIR / 'itemicons-sheet.png'
    if not download(SHEET_URL, sheet_file):
        print('  icon sheet unavailable — skipping the second pass')
        return 0
    sheet = Image.open(io.BytesIO(sheet_file.read_bytes())).convert('RGBA')
    sheet_file.unlink()
    cells = (sheet.width // CELL) * (sheet.height // CELL)
    done = 0
    for item_id in missing:
        n = nums.get(showdown_id(item_id))
        if n is None or n >= cells:
            continue
        x, y = (n % SHEET_COLUMNS) * CELL, (n // SHEET_COLUMNS) * CELL
        cell = sheet.crop((x, y, x + CELL, y + CELL))
        # An empty cell is not an icon — leave the item absent rather
        # than ship a blank square.
        if cell.getextrema()[3][1] == 0:
            continue
        cell.save(out_dir / f'{item_id}.png')
        done += 1
    return done


def main() -> int:
    out_dir = WORK_DIR / 'items'
    out_dir.mkdir(parents=True, exist_ok=True)
    ids = item_ids()
    print(f'Item ids from damage-calc: {len(ids)}')

    def fetch_one(item_id: str) -> bool:
        dst = out_dir / f'{item_id}.png'
        # Save under OUR id whichever spelling Showdown uses, so the
        # app can look up by the id it already holds.
        for candidate in (item_id, item_id.replace('-', '')):
            if download(f'{BASE}/{candidate}.png', dst):
                return True
        return False

    ok = 0
    with ThreadPoolExecutor(max_workers=16) as ex:
        for got in ex.map(fetch_one, ids):
            if got:
                ok += 1
    print(f'  standalone item icons: {ok} / {len(ids)}')
    missing = [i for i in ids if not (out_dir / f'{i}.png').exists()]
    cut = cut_from_sheet(missing, out_dir)
    print(f'  cut from the icon sheet: {cut} / {len(missing)}')
    left = [i for i in ids if not (out_dir / f'{i}.png').exists()]
    print(f'  item icons total: {len(ids) - len(left)} / {len(ids)}')
    if left:
        print(f'  still without an icon ({len(left)}): {", ".join(left[:40])}'
              + (' …' if len(left) > 40 else ''))
    return 0


if __name__ == '__main__':
    sys.exit(main())
