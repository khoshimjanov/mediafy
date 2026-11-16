# Mediafy

Telegram bot MVP for downloading accessible social-media media you have permission to save.

## Features

- Paste a URL: automatic Instagram, TikTok, YouTube, Facebook, X and other allowlisted host detection.
- Instagram/TikTok: video or MP3 buttons; no video quality picker.
- YouTube/Shorts: detected video quality buttons and MP3.
- Public single photos: automatic delivery where supported by the extractor.
- Carousels: delivery of extracted entries (up to 10), when supported by yt-dlp.
- Temporary storage deleted after upload; simultaneous-job and upload-size limits.

**Limitations:** platform extractors can break, public photo/carousel extraction is not guaranteed, some video formats won't be available, and private/DRM-protected material is not bypassed. Long polling means only one active bot instance should consume updates.

## Local run

Install Python 3.12+ and FFmpeg (`ffmpeg -version` must work), then:

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env       # Windows: copy .env.example .env
```

Create a Telegram bot with [@BotFather](https://t.me/BotFather); place its token in `.env` (never commit your token). Then run:

```bash
python main.py
```

Open your Telegram bot and send `/start`, then paste a supported public URL.

## Docker

```bash
docker build -t mediafy .
docker run --rm --env-file .env mediafy
```

## Hosting and limits

By default `MAX_UPLOAD_MB=48` stays below the standard Bot API's 50 MB upload limit. The bot processes at most two downloads at a time; adjust `DOWNLOAD_PARALLELISM` to match your server. Large YouTube files may need a self-hosted Telegram Bot API server in a future version. A public launch also needs abuse prevention, job timeouts, per-user daily quotas, secure session storage and monitoring.

## Notes

- Download only content you own or are permitted to save; observe platform terms and applicable rights.
- Never commit `.env` or authentication cookies.
- Development version; validate live platform samples before production use.
