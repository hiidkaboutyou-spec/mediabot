# Universal Media Downloader Bot

A Persian-first Telegram bot called **دانلودچی (Downloadchi)** for downloading media from many public websites with [yt-dlp](https://github.com/yt-dlp/yt-dlp), plus Spotify track/album/playlist matching through [spotDL](https://github.com/spotDL/spotify-downloader).

yt-dlp supports thousands of extractors, but website support can break as services change. Private, region-restricted, or login-required media may not work. Spotify audio is DRM-protected and is not downloaded from Spotify; spotDL matches audio from third-party providers and adds Spotify metadata. Use this bot only for content you are authorized to download.

## Features

- Available video qualities, best quality, MP3 audio, and metadata captions
- Full yt-dlp playlist downloads, sent as separate files in playlist order (up to `MAX_PLAYLIST_ITEMS`)
- Spotify metadata lookup with a configurable per-link track cap
- Telegram profile icon at [`assets/bot-avatar.png`](assets/bot-avatar.png), with ready-to-copy BotFather profile text in [`BRANDING.md`](BRANDING.md)
- Configurable file size, media duration, and concurrent download limits
- Optional Telegram user allowlist and checks against private/local URL hosts
- Docker image and Render/Railway deployment manifests

## Run with Docker Compose

1. Create a Telegram bot with BotFather and copy its token.
2. Copy `.env.example` to `.env`, set `BOT_TOKEN`, and optionally set your Telegram numeric ID in `ALLOWED_USER_IDS`.
3. Run `docker compose up --build -d`.
4. Open the bot and send `/start`. Send `/id` to see your numeric Telegram ID.

See [README_FA.md](README_FA.md) for the full Persian setup guide and configuration table.

## Tests

Run the offline unit suite with `python -m unittest discover -s tests -v`. To also download a public sample MP4 and a two-item PeerTube playlist end to end, run `RUN_LIVE_MEDIA_TESTS=1 python -m unittest tests.test_live_media -v`. GitHub Actions runs the unit suite and builds the Docker image on pushes and pull requests.
