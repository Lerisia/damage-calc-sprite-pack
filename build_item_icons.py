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

Roughly 230 of our ~530 items resolve. The rest (Mega Stones,
Z-Crystals, Silvally memories, Champions-original candies) have no
standalone icon on Showdown; callers fall back to a text label.
"""
from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

WORK_DIR = Path('work')
BASE = 'https://play.pokemonshowdown.com/sprites/itemicons'
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
    print(f'  item icons: {ok} / {len(ids)}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
