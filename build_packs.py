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
import time
import urllib.request
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


def download(url: str, out: Path, retries: int = 3) -> bool:
    """GET with retries. A 404 is final; anything else (429 throttling,
    5xx, timeouts) is retried with a short backoff. The 2026-09-12 pack
    shipped only 1023/1240 dex renders because throttled fetches were
    treated like 404s and silently skipped."""
    for attempt in range(retries):
        r = subprocess.run(
            ['curl', '-sS', '-o', str(out), '-w', '%{http_code}',
             '--max-time', '30', url],
            capture_output=True, text=True,
        )
        code = (r.stdout or '').strip()[-3:]
        if code == '200' and out.exists() and out.stat().st_size >= 100:
            return True
        if out.exists():
            out.unlink()
        if code == '404':
            return False
        time.sleep(1.5 * (attempt + 1))
    return False


# ── champout fallback ────────────────────────────────────────────────
# Showdown's main CDN (`play.pokemonshowdown.com/sprites/...`) doesn't
# carry Pokémon Champions' Mega forms (Mega Raichu X/Y, Mega Garchomp Z,
# the M-B/M-C re-enabled megas, …). The smogon/sprites repo DOES, under
# `src/champions/s<species>[-o<form>][-s].png`: `sabsol-omega.png`,
# `sabsol-omega_z.png`, `scharizard-omega_x.png`, `sraichu-oalola.png`,
# `srotom-oheat.png`, `stauros-opaldea_aqua.png`, `stoxtricity-olow_key.png`,
# `sindeedee-of.png`, `-s` for shiny. (An earlier `s<id>` scheme is gone —
# every fallback 404'd for a while without anyone noticing.) The
# directory listing is fetched once so we only request files that exist.

_CHAMPOUT_BASE = ('https://raw.githubusercontent.com/smogon/sprites/'
                  'master/src/champions')
_CHAMPOUT_LIST = 'https://api.github.com/repos/smogon/sprites/contents/src/champions'
_champout_files: Optional[set[str]] = None


def _load_champout_files() -> set[str]:
    global _champout_files
    if _champout_files is not None:
        return _champout_files
    try:
        req = urllib.request.Request(_CHAMPOUT_LIST, headers={'User-Agent': 'damage-calc-sprite-pack'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            _champout_files = {e['name'] for e in json.load(resp)}
    except Exception as e:  # listing is an optimisation; fall back to blind GETs
        print(f'  WARN champout listing unavailable ({e}); trying blind fetches')
        _champout_files = set()
    return _champout_files


def champout_key(name: str) -> Optional[str]:
    """Map damage-calc's display name to champout's file stem (without
    the leading 's' and the extension), or None when the name is a form
    champout can't have."""
    n = name.strip()
    sp = lambda x: _strip_alnum(x)
    m = re.fullmatch(r'Mega (\w+) ([XYZ])', n)
    if m:
        return f'{sp(m.group(1))}-omega_{m.group(2).lower()}'
    m = re.fullmatch(r'Mega (\w+)', n)
    if m:
        # Meowstic's Mega is per-gender in champout; our dex has one entry.
        if m.group(1) == 'Meowstic':
            return 'meowstic-om_mega'
        return f'{sp(m.group(1))}-omega'
    if n.startswith('Primal '):
        return None
    m = re.fullmatch(r'(Heat|Wash|Frost|Fan|Mow) Rotom', n)
    if m:
        return f'rotom-o{m.group(1).lower()}'
    for pre, suf in REGIONAL.items():
        if n.startswith(pre + ' '):
            rest = n[len(pre) + 1:]
            nested = re.fullmatch(r'(\w+) \(([^)]+)\)', rest)
            if nested:
                words = [w for w in nested.group(2).split() if w not in NOISE_FORM_WORDS and w != 'Breed']
                return f'{sp(nested.group(1))}-o{suf}_' + '_'.join(w.lower() for w in words)
            return f'{sp(rest)}-o{suf}'
    m = re.fullmatch(r"(.+?) \(([^)]+)\)", n)
    if m:
        species = sp(m.group(1))
        inner = m.group(2)
        if inner == 'Female':
            return f'{species}-of'
        if inner == 'Male':
            return species
        words = [w for w in inner.split() if w not in NOISE_FORM_WORDS]
        return f'{species}-o' + '_'.join(w.lower() for w in (words or inner.split()))
    return sp(n)


def download_champout(name: str, out: Path, shiny: bool = False) -> bool:
    """Try champout for [name]; no-op when it has no such file."""
    key = champout_key(name)
    if key is None:
        return False
    fname = f's{key}{"-s" if shiny else ""}.png'
    files = _load_champout_files()
    if files and fname not in files:
        return False
    return download(f'{_CHAMPOUT_BASE}/{fname}', out)


# ── PokeAPI HOME fallback (dex style only) ───────────────────────────
# Showdown's dex/ CDN stops short of a lot of Gen 8/9 content (Zacian,
# Urshifu, Terapagos, the Galar/Hisui regionals, Crowned/Origin forms,
# ...). PokeAPI/sprites mirrors the official HOME renders for all of
# them by PokeAPI id at sprites/pokemon/other/home/<id>.png (shiny under
# home/shiny/). Base species map by dex number; forms go through
# PokeAPI's pokemon list, which mostly shares Showdown's slugs.

_POKEAPI_LIST = 'https://pokeapi.co/api/v2/pokemon?limit=2000'
_POKEAPI_HOME = ('https://raw.githubusercontent.com/PokeAPI/sprites/master/'
                 'sprites/pokemon/other/home')
_pokeapi_ids: Optional[dict[str, int]] = None
# our sprite key → PokeAPI slug where they differ
POKEAPI_KEY_FIX = {
    'mrmime-galar': 'mr-mime-galar', 'mrmime': 'mr-mime', 'mrrime': 'mr-rime',
    'darmanitan-galar': 'darmanitan-galar-standard',
    'darmanitan-galarzen': 'darmanitan-galar-zen',
    'urshifu': 'urshifu-single-strike', 'urshifu-rapidstrike': 'urshifu-rapid-strike',
    'zygarde': 'zygarde-50', 'toxtricity': 'toxtricity-amped',
    'tapukoko': 'tapu-koko', 'tapulele': 'tapu-lele', 'tapubulu': 'tapu-bulu', 'tapufini': 'tapu-fini',
    'slitherwing': 'slither-wing', 'tinglu': 'ting-lu', 'chienpao': 'chien-pao',
    'wochien': 'wo-chien', 'chiyu': 'chi-yu', 'gougingfire': 'gouging-fire',
    'ragingbolt': 'raging-bolt', 'ironboulder': 'iron-boulder', 'ironcrown': 'iron-crown',
    'ironvaliant': 'iron-valiant', 'ironhands': 'iron-hands', 'ironleaves': 'iron-leaves',
    'ironmoth': 'iron-moth', 'ironjugulis': 'iron-jugulis', 'ironthorns': 'iron-thorns',
    'ironbundle': 'iron-bundle', 'irontreads': 'iron-treads', 'walkingwake': 'walking-wake',
    'greattusk': 'great-tusk', 'screamtail': 'scream-tail', 'brutebonnet': 'brute-bonnet',
    'fluttermane': 'flutter-mane', 'slitherwing': 'slither-wing', 'sandyshocks': 'sandy-shocks',
    'roaringmoon': 'roaring-moon', 'typenull': 'type-null', 'jangmoo': 'jangmo-o',
    'hakamoo': 'hakamo-o', 'kommoo': 'kommo-o', 'porygonz': 'porygon-z', 'hooh': 'ho-oh',
    'nidoranf': 'nidoran-f', 'nidoranm': 'nidoran-m', 'flabebe': 'flabebe',
}


def _load_pokeapi_ids() -> dict[str, int]:
    global _pokeapi_ids
    if _pokeapi_ids is not None:
        return _pokeapi_ids
    try:
        req = urllib.request.Request(_POKEAPI_LIST, headers={'User-Agent': 'damage-calc-sprite-pack'})
        with urllib.request.urlopen(req, timeout=30) as resp:
            results = json.load(resp)['results']
        _pokeapi_ids = {r['name']: int(r['url'].rstrip('/').rsplit('/', 1)[1]) for r in results}
    except Exception as e:
        print(f'  WARN PokeAPI list unavailable ({e}); HOME fallback limited to base species')
        _pokeapi_ids = {}
    return _pokeapi_ids


_dex_numbers: Optional[dict[str, int]] = None


def _dex_number(name: str) -> Optional[int]:
    """National dex number for a *base* species name (forms return None:
    their HOME art lives under a PokeAPI form id, not the dex number)."""
    global _dex_numbers
    if _dex_numbers is None:
        _dex_numbers = {}
        for p in sorted(DATA_DIR.glob('*.json')):
            for e in json.loads(p.read_text(encoding='utf-8')):
                if e.get('name') and e.get('dexNumber'):
                    _dex_numbers[e['name']] = int(e['dexNumber'])
    if base_species_name(name) is not None:
        return None
    return _dex_numbers.get(name)


def pokeapi_id(name: str) -> Optional[int]:
    key = sprite_key(name)
    ids = _load_pokeapi_ids()
    for cand in (POKEAPI_KEY_FIX.get(key), key):
        if cand and cand in ids:
            return ids[cand]
    return _dex_number(name)


def download_pokeapi_home(name: str, out: Path, shiny: bool = False) -> bool:
    pid = pokeapi_id(name)
    if pid is None:
        return False
    sub = 'shiny/' if shiny else ''
    return download(f'{_POKEAPI_HOME}/{sub}{pid}.png', out)


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

    # Shiny calls pass `style_key='bw/shiny'` (work dir nesting trick
    # — see main()), so split off the base for the per-style decisions
    # below. Without this split, the BW pixel gate silently skipped
    # the shiny pass and let auto-downscaled HOME renders through
    # for every key the X/Y Sprite Project hasn't shipped a shiny
    # palette PNG for (RetroNC's ZA Megas were the visible symptom —
    # regular pixel sprite shown, shiny rendered as a smooth HOME
    # downscale).
    base_style = style_key.split('/')[0]
    bw_pixel_gate = (base_style == 'bw')

    # manual_sprites/<base_style>/ (and /<base_style>-shiny/ for shiny
    # passes) holds artist-contributed overrides. Files here pre-empt
    # the CDN entirely and skip the pixel-art gate — they were curated
    # by hand, so we trust them.
    manual_subdir = f'{base_style}-shiny' if fetching_shiny else base_style
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
        # Fallbacks (PNG only — animated GIFs aren't there). Both are
        # HOME-render art, so the BW pack never accepts them:
        #   1. champout (Champions Megas and Champions-roster forms)
        #   2. PokeAPI's HOME mirror (Gen 8/9 species, regionals, …)
        if ext.lower() == 'png' and not bw_pixel_gate:
            if download_champout(n, dst, shiny=fetching_shiny):
                return True
            return download_pokeapi_home(n, dst, shiny=fetching_shiny)
        return False

    # 6 workers: Showdown's CDN throttles heavier bursts, and download()
    # now retries instead of skipping — see its docstring.
    with ThreadPoolExecutor(max_workers=6) as ex:
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
    items_src = WORK_DIR / 'items'
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
        # Held-item icons (24×24). Shared across styles like the box
        # icons — see build_item_icons.py.
        if items_src.exists():
            for f in sorted(items_src.iterdir()):
                if f.is_file():
                    zf.write(f, arcname=f'items/{f.name}')
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
