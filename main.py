"""Mediafy: Telegram bot for accessible social-media media."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import secrets
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

load_dotenv()
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("mediafy")

# Avoid letting users request arbitrary local/network URLs.
SUPPORTED_HOSTS = (
    "instagram.com", "tiktok.com", "youtube.com", "youtu.be",
    "facebook.com", "fb.watch", "twitter.com", "x.com",
    "vimeo.com", "threads.net", "pinterest.com", "pin.it",
    "reddit.com", "redd.it",
)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
SESSION_SECONDS = 20 * 60
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_MB", "48")) * 1024 * 1024
MAX_CAROUSEL_ITEMS = 10
DOWNLOAD_GATE = asyncio.Semaphore(max(1, int(os.getenv("DOWNLOAD_PARALLELISM", "2"))))


@dataclass
class MediaSession:
    owner_id: int
    url: str
    platform: str
    title: str
    kind: str
    qualities: list[int]
    created_at: float


SESSIONS: dict[str, MediaSession] = {}
BUSY_USERS: set[int] = set()


def host_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def platform_for(url: str) -> str | None:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return None
        host = parsed.hostname.lower().rstrip(".")
        if not any(host_matches(host, d) for d in SUPPORTED_HOSTS):
            return None
        for name, hosts in (
            ("Instagram", ("instagram.com",)),
            ("TikTok", ("tiktok.com",)),
            ("YouTube", ("youtube.com", "youtu.be")),
            ("Facebook", ("facebook.com", "fb.watch")),
            ("X", ("x.com", "twitter.com")),
        ):
            if any(host_matches(host, d) for d in hosts):
                return name
        return "Social media"
    except ValueError:
        return None


def find_link(text: str) -> tuple[str, str] | None:
    for match in URL_RE.finditer(text):
        url = match.group(0).rstrip(".,!?:;)]}>")
        platform = platform_for(url)
        if platform:
            return url, platform
    return None


def ydl_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
        "retries": 2,
        "extractor_retries": 2,
    }


def entries_for(info: dict) -> list[dict]:
    entries = info.get("entries")
    if entries is None:
        return [info]
    output = []
    for entry in entries:
        if entry:
            output.extend(entries_for(entry))
        if len(output) >= MAX_CAROUSEL_ITEMS:
            break
    return output[:MAX_CAROUSEL_ITEMS]


def is_photo(info: dict) -> bool:
    return str(info.get("ext", "")).lower() in {"jpg", "jpeg", "png", "webp"}


def inspect_media(url: str) -> tuple[str, str, list[int]]:
    with yt_dlp.YoutubeDL(ydl_options()) as ydl:
        info = ydl.extract_info(url, download=False)
    if not info:
        raise ValueError("No accessible media found")
    entries = entries_for(info)
    if not entries:
        raise ValueError("No accessible media found")
    title = str(info.get("title") or entries[0].get("title") or "Media")[:180]
    kind = "carousel" if len(entries) > 1 else "photos" if is_photo(entries[0]) else "media"
    heights = set()
    for fmt in entries[0].get("formats") or []:
        if fmt.get("vcodec") == "none":
            continue
        height = fmt.get("height")
        if isinstance(height, (int, float)) and 144 <= height <= 4320:
            heights.add(int(height))
    qualities = [h for h in (360, 480, 720, 1080, 1440, 2160) if any(v >= h for v in heights)]
    return title, kind, qualities


def keyboard(token: str, session: MediaSession) -> InlineKeyboardMarkup | None:
    if session.kind == "photos":
        return None
    if session.kind == "carousel":
        return InlineKeyboardMarkup([[
            InlineKeyboardButton("📥 Download carousel", callback_data=f"d:{token}:all")
        ]])
    if session.platform == "YouTube":
        buttons = [InlineKeyboardButton(f"🎬 {h}p", callback_data=f"d:{token}:v{h}") for h in session.qualities]
        if not buttons:
            buttons = [InlineKeyboardButton("🎬 Video", callback_data=f"d:{token}:video")]
        rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
        rows.append([InlineKeyboardButton("🎵 MP3", callback_data=f"d:{token}:audio")])
        return InlineKeyboardMarkup(rows)
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🎬 Video", callback_data=f"d:{token}:video"),
        InlineKeyboardButton("🎵 Audio only", callback_data=f"d:{token}:audio"),
    ]])


def download_media(url: str, choice: str, folder: Path) -> list[Path]:
    options = ydl_options()
    options.update({
        "skip_download": False,
        "outtmpl": str(folder / "%(playlist_index|0)s_%(id)s.%(ext)s"),
        "max_filesize": MAX_UPLOAD_BYTES,
        "merge_output_format": "mp4",
        "windowsfilenames": True,
        "playlistend": MAX_CAROUSEL_ITEMS,
    })
    if choice == "audio":
        options["format"] = "bestaudio/best"
        options["postprocessors"] = [{
            "key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"
        }]
    elif choice.startswith("v") and choice[1:].isdigit():
        height = int(choice[1:])
        options["format"] = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]"
    else:
        options["format"] = "bestvideo+bestaudio/best"
    with yt_dlp.YoutubeDL(options) as ydl:
        ydl.download([url])
    files = sorted(
        (p for p in folder.iterdir() if p.is_file() and p.suffix.lower() not in (".part", ".ytdl", ".temp")),
        key=lambda p: p.name,
    )
    if not files:
        raise ValueError("No downloaded files")
    return files[:MAX_CAROUSEL_ITEMS]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "👋 Welcome to Mediafy! Paste a supported public Instagram, TikTok, YouTube, "
            "Facebook or X link. Only download media you have permission to save."
        )


def purge_sessions() -> None:
    now = time.monotonic()
    for token, session in list(SESSIONS.items()):
        if now - session.created_at > SESSION_SECONDS:
            del SESSIONS[token]


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not user:
        return
    text = msg.text or msg.caption or ""
    found = find_link(text)
    if not found:
        await msg.reply_text("Paste a supported social-media link to begin.")
        return
    url, platform = found
    if user.id in BUSY_USERS:
        await msg.reply_text("Another download is running. Please retry after it finishes.")
        return
    status = await msg.reply_text(f"🔎 Checking {platform} link…")
    try:
        title, kind, qualities = await asyncio.to_thread(inspect_media, url)
        purge_sessions()
        token = secrets.token_urlsafe(9)
        session = MediaSession(user.id, url, platform, title, kind, qualities, time.monotonic())
        SESSIONS[token] = session
        if kind == "photos":
            context.application.create_task(send_media(context, status.chat_id, user.id, token, "all", status.message_id))
            return
        await status.edit_text(f"✅ {platform} detected\n📄 {title}\n\nChoose a download option:", reply_markup=keyboard(token, session))
    except Exception as exc:
        log.warning("Inspection failed: %s", exc)
        await status.edit_text("Couldn't access this post. Check that it's public and supported.")


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    user = update.effective_user
    if not query or not user:
        return
    parts = (query.data or "").split(":")
    if len(parts) != 3 or parts[0] != "d":
        await query.answer("Invalid option", show_alert=True)
        return
    _, token, choice = parts
    purge_sessions()
    session = SESSIONS.get(token)
    if not session or session.owner_id != user.id:
        await query.answer("Link expired. Paste it again.", show_alert=True)
        return
    choices = {"all", "video", "audio"} | {f"v{h}" for h in session.qualities}
    if choice not in choices:
        await query.answer("Unsupported option", show_alert=True)
        return
    if user.id in BUSY_USERS:
        await query.answer("You already have a download running.", show_alert=True)
        return
    await query.answer("Preparing download")
    if query.message:
        context.application.create_task(send_media(context, query.message.chat_id, user.id, token, choice, query.message.message_id))


async def send_media(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, token: str, choice: str, message_id: int) -> None:
    session = SESSIONS.get(token)
    if not session or user_id in BUSY_USERS:
        return
    BUSY_USERS.add(user_id)
    try:
        async with DOWNLOAD_GATE:
            await context.bot.edit_message_text(
                text="⏳ Downloading and processing…",
                chat_id=chat_id,
                message_id=message_id,
            )
            await context.bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
            with tempfile.TemporaryDirectory(prefix="mediafy_") as tmp:
                paths = await asyncio.to_thread(download_media, session.url, choice, Path(tmp))
                sent = 0
                for path in paths:
                    if path.stat().st_size > MAX_UPLOAD_BYTES:
                        await context.bot.send_message(chat_id, f"⚠️ {path.name} is too large. Try lower YouTube quality.")
                        continue
                    with path.open("rb") as media:
                        if path.suffix.lower() in (".jpg", ".jpeg", ".png"):
                            await context.bot.send_photo(chat_id, media)
                        elif path.suffix.lower() == ".mp3":
                            await context.bot.send_audio(chat_id, media, title=session.title[:64])
                        elif path.suffix.lower() == ".mp4":
                            await context.bot.send_video(chat_id, media, supports_streaming=True)
                        else:
                            await context.bot.send_document(chat_id, media)
                    sent += 1
                await context.bot.edit_message_text(
                    text=f"✅ Sent {sent} file(s)." if sent else "No files fit the upload limit.",
                    chat_id=chat_id,
                    message_id=message_id,
                )
    except Exception as exc:
        log.exception("Download failed: %s", exc)
        try:
            await context.bot.edit_message_text(
                text="❌ Download failed. The post might be restricted, unavailable or too large.",
                chat_id=chat_id,
                message_id=message_id,
            )
        except TelegramError:
            pass
    finally:
        BUSY_USERS.discard(user_id)


def build_application() -> Application:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN in .env or your environment")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_button, pattern=r"^d:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    return app


if __name__ == "__main__":
    build_application().run_polling(drop_pending_updates=True)
