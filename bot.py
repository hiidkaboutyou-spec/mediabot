import asyncio
import html
import logging
import os
import re
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yt_dlp
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from health_server import start_health_server

load_dotenv()

TOKEN = os.getenv("BOT_TOKEN", "").strip()
ALLOWED_USER_IDS = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip().isdigit()}
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "49"))
MAX_PLAYLIST_ITEMS = int(os.getenv("MAX_PLAYLIST_ITEMS", "30"))
SPOTIFY_AUDIO_FORMAT = os.getenv("SPOTIFY_AUDIO_FORMAT", "mp3")
SPOTIFY_BITRATE = os.getenv("SPOTIFY_BITRATE", "320k")
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip() or None

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
log = logging.getLogger("universal-media-bot")

URL_RE = re.compile(r"https?://\S+", re.I)
SPOTIFY_RE = re.compile(r"https?://open\.spotify\.com/(track|album|playlist)/", re.I)

@dataclass
class Job:
    url: str
    title: str
    caption: str
    formats: list[dict[str, Any]]

JOBS: dict[str, Job] = {}


def allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and (not ALLOWED_USER_IDS or user.id in ALLOWED_USER_IDS))


def ydl_base(download_dir: str | None = None) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "restrictfilenames": True,
        "socket_timeout": 30,
    }
    if download_dir:
        opts["outtmpl"] = str(Path(download_dir) / "%(title).120s-%(id)s.%(ext)s")
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def make_caption(info: dict[str, Any]) -> str:
    title = info.get("title") or "بدون عنوان"
    uploader = info.get("uploader") or info.get("channel") or info.get("artist") or ""
    description = (info.get("description") or "").strip()
    original = (info.get("webpage_url") or info.get("original_url") or "").strip()
    parts = [f"{title}"]
    if uploader:
        parts.append(f"از {uploader}")
    if description:
        short = description[:700].strip()
        parts.extend(["", short + ("…" if len(description) > 700 else "")])
    if original:
        parts.extend(["", original])
    return "\n".join(parts)[:1024]


def list_formats(url: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    with yt_dlp.YoutubeDL(ydl_base()) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("entries"):
        info = next((e for e in info["entries"] if e), info)
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for f in info.get("formats") or []:
        if f.get("vcodec") == "none" or not f.get("format_id"):
            continue
        height = f.get("height")
        ext = f.get("ext") or "mp4"
        if not height:
            continue
        label = f"{height}p · {ext}"
        key = str(height)
        if key in seen:
            continue
        seen.add(key)
        candidates.append({"id": str(f["format_id"]), "height": int(height), "label": label})
    candidates.sort(key=lambda x: x["height"], reverse=True)
    # Keep a compact menu plus universal best/audio options.
    compact = candidates[:6]
    compact.append({"id": "best", "height": 0, "label": "بهترین کیفیت ویدیو"})
    compact.append({"id": "audio", "height": 0, "label": "فقط صدا (MP3)"})
    return info, compact


def download_media(url: str, fmt: str, target: str) -> tuple[Path, dict[str, Any]]:
    opts = ydl_base(target)
    opts.update({"noplaylist": True, "merge_output_format": "mp4"})
    if fmt == "audio":
        opts.update({
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            }],
        })
    elif fmt == "best":
        opts["format"] = "bestvideo*+bestaudio/best"
    else:
        opts["format"] = f"{fmt}+bestaudio/best/{fmt}/best"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        prepared = Path(ydl.prepare_filename(info))

    files = [p for p in Path(target).iterdir() if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
    if not files:
        raise RuntimeError("فایل خروجی پیدا نشد")
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    # Prefer final merged/audio file.
    final = next((p for p in files if p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mp3", ".m4a"}), files[0])
    return final, info


def download_spotify(url: str, target: str) -> list[Path]:
    cmd = [
        "spotdl", "download", url,
        "--output", str(Path(target) / "{artists} - {title}.{output-ext}"),
        "--format", SPOTIFY_AUDIO_FORMAT,
        "--bitrate", SPOTIFY_BITRATE,
        "--threads", "4",
        "--max-retries", "3",
    ]
    result = __import__("subprocess").run(cmd, capture_output=True, text=True, timeout=3600)
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "خطای spotDL")[-1500:])
    files = sorted(Path(target).glob(f"*.{SPOTIFY_AUDIO_FORMAT}"))
    return files[:MAX_PLAYLIST_ITEMS]


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    text = (
        "سلام! لینک عمومی YouTube، Instagram، X یا Spotify را بفرست.\n\n"
        "• ویدیو: کیفیت‌ها را انتخاب می‌کنی.\n"
        "• کپشن: همراه پیش‌نمایش برایت می‌فرستم.\n"
        "• Spotify: ترک، آلبوم یا پلی‌لیست را با بهترین تنظیم صوتی دانلود می‌کند.\n\n"
        "فقط محتوایی را دانلود کن که مالک آن هستی یا اجازهٔ دانلودش را داری."
    )
    await update.message.reply_text(text)


async def show_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_user and update.message:
        await update.message.reply_text(f"آیدی عددی تلگرام شما: `{update.effective_user.id}`", parse_mode=ParseMode.MARKDOWN)


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.message or not update.message.text:
        return
    match = URL_RE.search(update.message.text)
    if not match:
        await update.message.reply_text("یک لینک معتبر بفرست.")
        return
    url = match.group(0).rstrip(".,)")
    status = await update.message.reply_text("دارم لینک را بررسی می‌کنم…")

    if SPOTIFY_RE.search(url):
        await status.edit_text("لینک اسپاتیفای شناسایی شد؛ دانلود با بهترین تنظیم شروع شد…")
        tmp = tempfile.mkdtemp(prefix="spotify_")
        try:
            files = await asyncio.to_thread(download_spotify, url, tmp)
            if not files:
                raise RuntimeError("هیچ آهنگی دانلود نشد")
            await status.edit_text(f"{len(files)} فایل آماده شد؛ در حال ارسال…")
            for p in files:
                if p.stat().st_size > MAX_UPLOAD_MB * 1024 * 1024:
                    await update.message.reply_text(f"فایل {p.name} از محدودیت تنظیم‌شده بزرگ‌تر است.")
                    continue
                with p.open("rb") as fh:
                    await update.message.reply_audio(audio=fh, title=p.stem[:64], read_timeout=180, write_timeout=180)
            await status.delete()
        except Exception as exc:
            log.exception("Spotify failure")
            await status.edit_text(f"دانلود اسپاتیفای ناموفق بود:\n{html.escape(str(exc))[:3000]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        return

    try:
        info, formats = await asyncio.to_thread(list_formats, url)
        job_id = uuid.uuid4().hex[:10]
        JOBS[job_id] = Job(url=url, title=info.get("title") or "Media", caption=make_caption(info), formats=formats)
        keyboard = [[InlineKeyboardButton(f["label"], callback_data=f"dl|{job_id}|{f['id']}")] for f in formats]
        keyboard.append([InlineKeyboardButton("فقط کپشن", callback_data=f"cap|{job_id}")])
        preview = html.escape(make_caption(info)[:900])
        await status.edit_text(
            f"<b>{html.escape(info.get('title') or 'Media')}</b>\n\n<blockquote>{preview}</blockquote>\n\nکیفیت را انتخاب کن:",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.exception("Probe failure")
        await status.edit_text(f"نتوانستم این لینک را بررسی کنم:\n{html.escape(str(exc))[:3000]}")


async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update) or not update.callback_query:
        return
    q = update.callback_query
    await q.answer()
    parts = (q.data or "").split("|")
    action, job_id = parts[0], parts[1]
    job = JOBS.get(job_id)
    if not job:
        await q.edit_message_text("این انتخاب منقضی شده؛ لینک را دوباره بفرست.")
        return
    if action == "cap":
        await q.message.reply_text(job.caption, disable_web_page_preview=True)
        return

    fmt = parts[2]
    await q.edit_message_text("دانلود شروع شد…")
    tmp = tempfile.mkdtemp(prefix="media_")
    try:
        path, info = await asyncio.to_thread(download_media, job.url, fmt, tmp)
        size_mb = path.stat().st_size / 1024 / 1024
        if size_mb > MAX_UPLOAD_MB:
            await q.edit_message_text(
                f"فایل {size_mb:.1f} مگابایت است و از سقف {MAX_UPLOAD_MB} مگابایت بیشتر شده. کیفیت پایین‌تر را انتخاب کن."
            )
            return
        caption = make_caption(info)
        with path.open("rb") as fh:
            if path.suffix.lower() == ".mp3":
                await q.message.reply_audio(audio=fh, caption=caption, read_timeout=180, write_timeout=180)
            else:
                await q.message.reply_document(document=fh, caption=caption, filename=path.name, read_timeout=180, write_timeout=180)
        await q.delete_message()
    except Exception as exc:
        log.exception("Download failure")
        await q.edit_message_text(f"دانلود ناموفق بود:\n{html.escape(str(exc))[:3000]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> None:
    if not TOKEN:
        raise SystemExit("BOT_TOKEN در فایل .env تنظیم نشده است")
    start_health_server()
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("id", show_id))
    app.add_handler(CallbackQueryHandler(callback, pattern=r"^(dl|cap)\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
