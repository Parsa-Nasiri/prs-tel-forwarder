"""
Telegram → Rubika Forwarder
────────────────────────────────────────────────────────────────────────────
Features
  • Forwards text, photos, videos, audio, voice, documents
  • Top-3 message reactions appended to every forward
  • Whitelist-based delivery (up to N users)
  • Admin commands via Rubika bot polling:
        whitelist {chat_id} [Name]   – add user
        remove {chat_id}             – remove user
        /list                        – show all whitelisted users
  • State AND whitelist persisted in a private GitHub repo
    (so the bot doesn't re-forward everything on every 6-hour restart)
  • Session-safe: GitHub Actions concurrency prevents duplicate sessions

Required secrets
  API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN,
  ADMIN_CHAT_IDS          (comma-separated Rubika chat IDs of admins)
  DATA_REPO_TOKEN         (GitHub PAT with repo scope)
  DATA_REPO_OWNER         (your GitHub username)
  DATA_REPO_NAME          (private repo name, e.g. "my-bot-data")

Optional fallback (if no whitelist is set up yet)
  RUBIKA_CHAT_IDS / RUBIKA_CHAT_ID   (comma-separated)
"""

import asyncio
import base64
import json
import logging
import os
import sys
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Optional

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ─── Configuration ────────────────────────────────────────────────────────────

API_ID          = int(os.environ["API_ID"])
API_HASH        = os.environ["API_HASH"]
STRING_SESSION  = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

ADMIN_CHAT_IDS  = [c.strip() for c in (os.environ.get("ADMIN_CHAT_IDS") or "").split(",") if c.strip()]

DATA_REPO_TOKEN = os.environ.get("DATA_REPO_TOKEN", "")
DATA_REPO_OWNER = os.environ.get("DATA_REPO_OWNER", "")
DATA_REPO_NAME  = os.environ.get("DATA_REPO_NAME", "")

CHANNELS_FILE   = Path("channels.json")
RUN_DURATION    = 20_880          # 5 h 48 min  → 12-min gap before next 6-h cron
MAX_FILE_SIZE   = 50 * 1024 * 1024  # 50 MB

RUBIKA_BASE     = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

# ─── Private-repo helpers ──────────────────────────────────────────────────────

def _gh_headers() -> dict:
    return {
        "Authorization": f"token {DATA_REPO_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

def _gh_get(filename: str) -> tuple[dict, Optional[str]]:
    """Fetch a JSON file from the private data repo. Returns (data, sha)."""
    if not DATA_REPO_TOKEN:
        return {}, None
    url = f"https://api.github.com/repos/{DATA_REPO_OWNER}/{DATA_REPO_NAME}/contents/{filename}"
    try:
        resp = requests.get(url, headers=_gh_headers(), timeout=10)
        if resp.status_code == 200:
            payload = resp.json()
            data = json.loads(base64.b64decode(payload["content"]).decode())
            return data, payload["sha"]
        if resp.status_code == 404:
            return {}, None
        logger.error(f"GH GET {filename}: {resp.status_code}")
    except Exception as exc:
        logger.error(f"GH GET {filename} error: {exc}")
    return {}, None

def _gh_put(filename: str, data: dict, sha: Optional[str]) -> Optional[str]:
    """Write a JSON file to the private data repo. Returns new sha or None."""
    if not DATA_REPO_TOKEN:
        return None
    url = f"https://api.github.com/repos/{DATA_REPO_OWNER}/{DATA_REPO_NAME}/contents/{filename}"
    content = base64.b64encode(json.dumps(data, indent=2, ensure_ascii=False).encode()).decode()
    payload: dict = {"message": f"Update {filename}", "content": content}
    if sha:
        payload["sha"] = sha
    try:
        resp = requests.put(url, headers=_gh_headers(), json=payload, timeout=15)
        if resp.status_code in (200, 201):
            return resp.json()["content"]["sha"]
        logger.error(f"GH PUT {filename}: {resp.status_code} {resp.text[:200]}")
    except Exception as exc:
        logger.error(f"GH PUT {filename} error: {exc}")
    return None

# ─── Whitelist ─────────────────────────────────────────────────────────────────

_whitelist: dict   = {}   # { "rubika_chat_id": "Display Name" }
_whitelist_sha: Optional[str] = None

def load_whitelist() -> None:
    global _whitelist, _whitelist_sha
    data, sha = _gh_get("whitelist.json")
    _whitelist = data
    _whitelist_sha = sha
    logger.info(f"Whitelist loaded: {len(_whitelist)} user(s)")

def save_whitelist() -> bool:
    global _whitelist_sha
    new_sha = _gh_put("whitelist.json", _whitelist, _whitelist_sha)
    if new_sha:
        _whitelist_sha = new_sha
        return True
    return False

def add_to_whitelist(chat_id: str, name: str) -> bool:
    _whitelist[chat_id] = name
    return save_whitelist()

def remove_from_whitelist(chat_id: str) -> bool:
    _whitelist.pop(chat_id, None)
    return save_whitelist()

def get_recipients() -> list[str]:
    """Return whitelisted IDs, falling back to env-var IDs if whitelist is empty."""
    if _whitelist:
        return list(_whitelist.keys())
    raw = os.environ.get("RUBIKA_CHAT_IDS") or os.environ.get("RUBIKA_CHAT_ID", "")
    return [c.strip() for c in raw.split(",") if c.strip()]

# ─── State ─────────────────────────────────────────────────────────────────────

_state: dict = {}         # { "@channel": last_forwarded_message_id }
_state_sha: Optional[str] = None

def load_state() -> None:
    global _state, _state_sha
    data, sha = _gh_get("state.json")
    _state = data
    _state_sha = sha
    logger.info(f"State loaded for {len(_state)} channel(s)")

def save_state() -> None:
    global _state_sha
    new_sha = _gh_put("state.json", _state, _state_sha)
    if new_sha:
        _state_sha = new_sha

# ─── Rubika send helpers ────────────────────────────────────────────────────────

def _clean(text: str) -> str:
    return text.replace("`", "")

def _header(channel_name: str, msg_date: datetime) -> str:
    return f"=============\n{channel_name}\n{msg_date.strftime('%Y-%m-%d %H:%M:%S')}\n============="

def rubika_send_text(recipients: list[str], channel_name: str, text: str,
                     msg_date: datetime, reaction_line: str = "") -> bool:
    body = f"{_header(channel_name, msg_date)}\n\n{_clean(text)}"
    if reaction_line:
        body += f"\n\n{reaction_line}"
    url = f"{RUBIKA_BASE}/sendMessage"
    ok = True
    for cid in recipients:
        try:
            resp = requests.post(url, json={"chat_id": cid, "text": body}, timeout=10)
            if resp.status_code == 200:
                logger.info(f"✅ Text → {cid}")
            else:
                logger.error(f"❌ Text {cid}: {resp.status_code} {resp.text[:150]}")
                ok = False
        except Exception as exc:
            logger.error(f"❌ Text {cid}: {exc}")
            ok = False
    return ok

# Field name & MIME per media type (must match Rubika endpoint expectations)
_MEDIA_META = {
    "photo":    ("sendPhoto",    "photo",    "image/jpeg"),
    "video":    ("sendVideo",    "video",    "video/mp4"),
    "audio":    ("sendAudio",    "audio",    "audio/mpeg"),
    "voice":    ("sendVoice",    "voice",    "audio/ogg"),
    "document": ("sendDocument", "document", "application/octet-stream"),
}

def rubika_send_media(
    recipients: list[str],
    channel_name: str,
    msg_date: datetime,
    file_bytes: bytes,
    filename: str,
    media_type: str,
    caption: str = "",
    reaction_line: str = "",
) -> bool:
    method, field, mime = _MEDIA_META.get(media_type, _MEDIA_META["document"])
    header_text = _header(channel_name, msg_date)
    cap = f"{header_text}\n\n{_clean(caption)}" if caption else header_text
    if reaction_line:
        cap += f"\n\n{reaction_line}"
    url = f"{RUBIKA_BASE}/{method}"
    ok = True
    for cid in recipients:
        try:
            # Key fix: field name must match the endpoint (photo/video/audio/…)
            files = {field: (filename, BytesIO(file_bytes), mime)}
            data  = {"chat_id": cid, "caption": cap}
            resp  = requests.post(url, data=data, files=files, timeout=60)
            logger.info(f"{method} [{resp.status_code}] → {cid}: {resp.text[:200]}")
            if resp.status_code == 200:
                result = resp.json()
                if result.get("status") == "OK" or result.get("ok"):
                    logger.info(f"✅ {media_type} → {cid}")
                else:
                    logger.error(f"❌ Rubika media error {cid}: {result}")
                    ok = False
            else:
                ok = False
        except Exception as exc:
            logger.error(f"❌ Media {cid}: {exc}")
            ok = False
    return ok

def rubika_reply(chat_id: str, text: str) -> None:
    try:
        requests.post(f"{RUBIKA_BASE}/sendMessage",
                      json={"chat_id": chat_id, "text": text}, timeout=10)
    except Exception as exc:
        logger.warning(f"rubika_reply error: {exc}")

# ─── Admin command polling ──────────────────────────────────────────────────────
# Rubika's official API is webhook-based.  We attempt getUpdates (which some
# library wrappers expose).  If the endpoint doesn't exist the call silently
# returns [] and no harm is done.  Admins can always manage whitelist.json
# directly in the private repo as a fallback.

_rubika_offset = 0

def poll_rubika_updates() -> list:
    global _rubika_offset
    try:
        resp = requests.get(
            f"{RUBIKA_BASE}/getUpdates",
            params={"offset": _rubika_offset, "timeout": 1},
            timeout=6,
        )
        if resp.status_code == 200:
            updates = resp.json().get("result", [])
            if updates:
                _rubika_offset = updates[-1]["update_id"] + 1
            return updates
    except Exception:
        pass
    return []

def handle_admin_update(update: dict) -> None:
    """Parse one Rubika bot update and act on admin commands."""
    msg       = update.get("message") or update.get("inline_message") or {}
    sender_id = msg.get("sender_id", "")
    chat_id   = msg.get("chat_id", "")
    text      = (msg.get("text") or "").strip()

    if not text or not chat_id:
        return

    is_admin = sender_id in ADMIN_CHAT_IDS or chat_id in ADMIN_CHAT_IDS

    if not is_admin:
        # Greet unknowns and tell them their chat_id
        if text.lower() in ("/start", "start", "hello", "سلام", "hi"):
            rubika_reply(
                chat_id,
                f"👋 سلام!\nشناسه شما:\n`{chat_id}`\n\nبرای دسترسی با ادمین تماس بگیرید."
            )
        return

    parts = text.split(maxsplit=2)
    cmd   = parts[0].lower()

    # whitelist {chat_id} [Name]
    if cmd == "whitelist" and len(parts) >= 2:
        uid  = parts[1].lstrip("@")
        name = parts[2] if len(parts) > 2 else uid
        if add_to_whitelist(uid, name):
            rubika_reply(chat_id, f"✅ Whitelisted: {uid} ({name})\n👥 Total: {len(_whitelist)}")
        else:
            rubika_reply(chat_id, "❌ Failed to update whitelist (check DATA_REPO_* secrets)")

    # remove {chat_id}
    elif cmd == "remove" and len(parts) >= 2:
        uid = parts[1].lstrip("@")
        if uid in _whitelist:
            remove_from_whitelist(uid)
            rubika_reply(chat_id, f"✅ Removed: {uid}\n👥 Total: {len(_whitelist)}")
        else:
            rubika_reply(chat_id, f"⚠️ {uid} not found in whitelist")

    # /list
    elif cmd in ("/list", "list"):
        if not _whitelist:
            rubika_reply(chat_id, "📋 Whitelist is empty")
        else:
            lines = [f"📋 Whitelist ({len(_whitelist)} user(s)):"]
            for uid, name in _whitelist.items():
                lines.append(f"• {name}  |  {uid}")
            rubika_reply(chat_id, "\n".join(lines))

    else:
        rubika_reply(
            chat_id,
            "❓ Available commands:\n"
            "  whitelist {id} [Name]\n"
            "  remove {id}\n"
            "  /list"
        )

# ─── Reactions ─────────────────────────────────────────────────────────────────

def build_reaction_line(message) -> str:
    """Return top-3 reactions as '❤️33  🍌12  👍3', or empty string."""
    try:
        if not message.reactions or not message.reactions.results:
            return ""
        top3 = sorted(message.reactions.results, key=lambda r: r.count, reverse=True)[:3]
        parts = []
        for rc in top3:
            emoticon = getattr(rc.reaction, "emoticon", "❓")
            parts.append(f"{emoticon}{rc.count}")
        return "  ".join(parts)
    except Exception:
        return ""

# ─── Core forwarding ───────────────────────────────────────────────────────────

async def forward_message(client, message, channel_name: str, skip_dup: bool = False) -> None:
    recipients = get_recipients()
    if not recipients:
        logger.warning("No recipients – add users via 'whitelist' command or set RUBIKA_CHAT_IDS")
        return

    if not skip_dup and message.id <= _state.get(channel_name, 0):
        logger.debug(f"Skipping already-forwarded {message.id}")
        return

    msg_date      = message.date
    reaction_line = build_reaction_line(message)

    # ── Text only ──────────────────────────────────────────────────────────────
    if message.text and not message.media:
        if rubika_send_text(recipients, channel_name, message.text, msg_date, reaction_line):
            _state[channel_name] = message.id
            save_state()
        return

    # ── Media ──────────────────────────────────────────────────────────────────
    if not message.file or not message.file.size:
        logger.warning(f"Message {message.id} has no downloadable file – skipping")
        return

    size = message.file.size
    if size > MAX_FILE_SIZE:
        mb = size / (1024 ** 2)
        rubika_send_text(
            recipients, channel_name,
            f"⚠️ Large file skipped ({mb:.1f} MB)\nFilename: {message.file.name or 'unknown'}",
            msg_date,
        )
        _state[channel_name] = message.id
        save_state()
        return

    if message.photo:
        media_type, filename = "photo",    "photo.jpg"
    elif message.video:
        media_type, filename = "video",    message.file.name or "video.mp4"
    elif message.audio:
        media_type, filename = "audio",    message.file.name or "audio.mp3"
    elif message.voice:
        media_type, filename = "voice",    message.file.name or "voice.ogg"
    else:
        media_type, filename = "document", message.file.name or "file"

    caption = message.text or ""

    try:
        media_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(media_bytes):,} bytes) from {channel_name}")
    except Exception as exc:
        logger.error(f"Download failed: {exc}")
        return

    if rubika_send_media(recipients, channel_name, msg_date,
                         media_bytes, filename, media_type, caption, reaction_line):
        _state[channel_name] = message.id
        save_state()

# ─── Start-up catch-up ─────────────────────────────────────────────────────────

async def catch_up(client, channels: list[str]) -> None:
    if not _state:
        logger.info("First run – recording start markers (no back-fill)")
        for ch in channels:
            try:
                msgs = await client.get_messages(ch, limit=1)
                _state[ch] = msgs[0].id if msgs else 0
                logger.info(f"  {ch} → start at {_state[ch]}")
            except Exception as exc:
                logger.error(f"Init {ch}: {exc}")
        save_state()
        return

    logger.info("Catching up missed messages…")
    for ch in channels:
        try:
            msgs = await client.get_messages(ch, limit=20)
            for msg in reversed(msgs):
                if msg.id <= _state.get(ch, 0):
                    continue
                if not msg.text and not msg.media:
                    continue
                logger.info(f"  Missed: {ch} #{msg.id}")
                await forward_message(client, msg, ch)
                await asyncio.sleep(1)   # be gentle with Rubika rate limits
        except Exception as exc:
            logger.error(f"Catch-up {ch}: {exc}")

# ─── Main ──────────────────────────────────────────────────────────────────────

async def main() -> None:
    for var in ("API_ID", "API_HASH", "STRING_SESSION", "RUBIKA_BOT_TOKEN"):
        if not os.environ.get(var):
            logger.error(f"Missing required secret: {var}")
            sys.exit(1)

    channels = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    logger.info(f"Channels: {channels}")

    load_whitelist()
    load_state()
    logger.info(f"Recipients: {len(get_recipients())}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready ✅")

    await catch_up(client, channels)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            name = getattr(chat, "title", str(chat.id))
            logger.info(f"New message from {name}")
            await forward_message(client, event.message, name)
        except Exception as exc:
            logger.error(f"Handler error: {exc}")

    logger.info("Forwarding in real-time… (polling Rubika for admin commands every 30 s)")
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed >= RUN_DURATION:
            logger.info(f"⏱ Time limit reached ({elapsed / 3600:.2f} h) – shutting down cleanly")
            break

        # Poll Rubika bot for admin commands (silently no-ops if getUpdates unsupported)
        try:
            for upd in poll_rubika_updates():
                handle_admin_update(upd)
        except Exception as exc:
            logger.debug(f"Admin poll error: {exc}")

        await asyncio.sleep(30)

    await client.disconnect()
    logger.info("Session closed. See you in 12 minutes 👋")


if __name__ == "__main__":
    asyncio.run(main())
