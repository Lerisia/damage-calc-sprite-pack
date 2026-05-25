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

# (style key, Showdown CDN subdir, file extension)
STYLES = [
    ('bw',  'gen5', 'png'),
    ('ani', 'ani',  'gif'),
    ('dex', 'dex',  'png'),
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


def collect_names() -> set[str]:
    names: set[str] = set()
    for p in sorted(DATA_DIR.glob('*.json')):
        for entry in json.loads(p.read_text(encoding='utf-8')):
            n = entry.get('name')
            if n:
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
    names = collect_names()
    print(f'Distinct Pokémon names: {len(names)}')
    names_sorted = sorted(names)
    for style_key, sd_dir, ext in STYLES:
        print(f'\n== {style_key} ({sd_dir}/*.{ext}) ==')
        n_ok = build_style(style_key, sd_dir, ext, names_sorted)
        z = zip_style(style_key)
        print(f'  fetched: {n_ok} / {len(names_sorted)}')
        print(f'  packed: {z} ({z.stat().st_size / 1024 / 1024:.1f} MB)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
