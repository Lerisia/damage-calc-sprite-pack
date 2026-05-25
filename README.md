# damage-calc-sprite-pack

Pre-built Pokémon sprite packs for [damage-calc.com](https://damage-calc.com)'s mobile app to import. Three style packs published as GitHub Release assets:

- **bw.zip** — Black/White pixel sprites (gen5 style, community-extended to all generations by the Smogon Sprite Project)
- **ani.zip** — Animated GIF sprites
- **dex.zip** — HOME 3D PNG sprites

A scheduled GitHub Actions workflow mirrors the latest sprites from `play.pokemonshowdown.com/sprites/` and re-publishes them as a single release. Pack sizes are ~3 MB for bw/dex, ~25 MB for ani.

## How the mobile app uses these

The damage-calc app does not bundle Pokémon sprites in its binary — that's a deliberate IP-hygiene choice (Apple App Store review scans static assets). Instead, the user can optionally download a pack from this repo's releases and import it into the app; the app extracts to local cache, and the sprite slots that previously showed a pokéball placeholder start rendering the imported sprites.

The web version of damage-calc.com fetches sprites directly from `play.pokemonshowdown.com/sprites/` at runtime and never reads from this repo.

## Credits and license

- BW pixel sprites are produced by the [Smogon Sprite Project](https://www.smogon.com/forums/threads/smogon-sprite-project.3647722/) and [X/Y Sprite Project](https://www.smogon.com/forums/threads/x-y-sprite-project.3486712/) community. Used here under their stated non-profit-use clause, with credit.
- Animated and HOME 3D sprite styles are derived works of official Game Freak / The Pokémon Company artwork.
- Pokémon, Pokémon character names, and related imagery are trademarks of Nintendo / Game Freak / The Pokémon Company. This repo redistributes those community-extended sprite assets for the sole purpose of supporting the unofficial damage-calc.com fan calculator's offline-first mobile mode. No commercial use.

## Updating

The workflow runs nightly via cron and can also be triggered manually from the Actions tab. Each run rebuilds all three packs from Showdown's CDN and replaces the `latest` release's assets.
