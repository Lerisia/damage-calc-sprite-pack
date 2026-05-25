# damage-calc-sprite-pack

Pre-built Pokémon sprite packs for [damage-calc.com](https://damage-calc.com)'s mobile app to import. Published as GitHub Release assets under the `latest` tag:

- **bw.zip** — Black/White pixel sprites, scoped to **gen 1–5 base species and gen1-5-era formes only**. Excludes all Megas (a gen6+ mechanic), all post-gen5 regional variants (Alolan/Galarian/Hisuian/Paldean), and all gen6+ species. This boundary keeps the pack to sprites derived from the original Game Freak BW games until the [X/Y Sprite Project](https://www.smogon.com/forums/threads/x-y-sprite-project.3486712/) (which extended BW-style art to post-gen5 Pokémon) explicitly OKs redistribution.
- **dex.zip** — HOME 3D PNG sprites, full Pokémon coverage.
- ~~ani.zip~~ — Animated GIF pack is intentionally not published yet, same community-attribution reason as bw's exclusions.

A scheduled GitHub Actions workflow mirrors the latest sprites from `play.pokemonshowdown.com/sprites/` and re-publishes them as a single release. Pack sizes: bw ~1 MB, dex ~3 MB.

## How the mobile app uses these

The damage-calc app does not bundle Pokémon sprites in its binary — that's a deliberate IP-hygiene choice (Apple App Store review scans static assets). Instead, the user can optionally download a pack from this repo's releases and import it into the app; the app extracts to local cache, and the sprite slots that previously showed a pokéball placeholder start rendering the imported sprites.

The web version of damage-calc.com fetches sprites directly from `play.pokemonshowdown.com/sprites/` at runtime and never reads from this repo.

## Credits and license

- BW pixel sprites are produced by the [Smogon Sprite Project](https://www.smogon.com/forums/threads/smogon-sprite-project.3647722/) and [X/Y Sprite Project](https://www.smogon.com/forums/threads/x-y-sprite-project.3486712/) community. Used here under their stated non-profit-use clause, with credit.
- Animated and HOME 3D sprite styles are derived works of official Game Freak / The Pokémon Company artwork.
- Pokémon, Pokémon character names, and related imagery are trademarks of Nintendo / Game Freak / The Pokémon Company. This repo redistributes those community-extended sprite assets for the sole purpose of supporting the unofficial damage-calc.com fan calculator's offline-first mobile mode. No commercial use.

## Updating

The workflow runs nightly via cron and can also be triggered manually from the Actions tab. Each run rebuilds all three packs from Showdown's CDN and replaces the `latest` release's assets.
