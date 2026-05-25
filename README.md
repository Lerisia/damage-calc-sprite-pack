# damage-calc-sprite-pack

Pre-built Pokémon sprite packs for [damage-calc.com](https://damage-calc.com)'s mobile app to import. Published as GitHub Release assets under the `latest` tag:

- **bw.zip** — Black/White pixel sprites at the top level (scoped to **gen 1–5 base species and gen1-5-era formes only**; excludes all Megas, all post-gen5 regional variants, and all gen6+ species — that work is the [X/Y Sprite Project](https://www.smogon.com/forums/threads/x-y-sprite-project.3486712/)'s community extension and we don't redistribute it without explicit permission) plus an `icons/` subdirectory with 40×30 box icons.
- **dex.zip** — HOME 3D PNG sprites at the top level (full Pokémon coverage, derived from official HOME assets) plus the same `icons/` subdirectory.
- ~~ani.zip~~ — Animated GIF pack intentionally not published yet, same community-attribution reason as bw's exclusions.

Box icons are bundled inside each style pack rather than published separately, so the user only manages one download per style — the app's import flow extracts both groups in one go. Icon scope is **gen 1–7 base species only** (num 1–809). 40×30 icons are the official Sun/Moon-era Game Freak style; gen8 onwards the game switched to 68×56 and Showdown's 40×30 icons for gen8+ Pokémon are drawn by [msikma/pokesprite](https://github.com/msikma/pokesprite) and similar community projects, so we exclude them.

A scheduled GitHub Actions workflow mirrors the latest sprites from `play.pokemonshowdown.com/sprites/` and re-publishes them as a single release. Pack sizes: ~2 MB each.

## How the mobile app uses these

The damage-calc app does not bundle Pokémon sprites in its binary — that's a deliberate IP-hygiene choice (Apple App Store review scans static assets). Instead, the user can optionally download a pack from this repo's releases and import it into the app; the app extracts to local cache, and the sprite slots that previously showed a pokéball placeholder start rendering the imported sprites.

The web version of damage-calc.com fetches sprites directly from `play.pokemonshowdown.com/sprites/` at runtime and never reads from this repo.

## Credits and license

- BW pixel sprites are produced by the [Smogon Sprite Project](https://www.smogon.com/forums/threads/smogon-sprite-project.3647722/) and [X/Y Sprite Project](https://www.smogon.com/forums/threads/x-y-sprite-project.3486712/) community. Used here under their stated non-profit-use clause, with credit.
- Animated and HOME 3D sprite styles are derived works of official Game Freak / The Pokémon Company artwork.
- Pokémon, Pokémon character names, and related imagery are trademarks of Nintendo / Game Freak / The Pokémon Company. This repo redistributes those community-extended sprite assets for the sole purpose of supporting the unofficial damage-calc.com fan calculator's offline-first mobile mode. No commercial use.

## Updating

The workflow runs nightly via cron and can also be triggered manually from the Actions tab. Each run rebuilds all three packs from Showdown's CDN and replaces the `latest` release's assets.
