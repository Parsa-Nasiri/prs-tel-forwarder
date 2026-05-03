import os
import json
import time
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime
from io import BytesIO

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------- Configuration ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

raw_chat_ids = os.environ.get("RUBIKA_CHAT_IDS") or os.environ["RUBIKA_CHAT_ID"]
RUBIKA_CHAT_IDS = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]

CHANNELS_FILE = Path("channels.json")
STATE_FILE = Path("state.json")
RUN_DURATION = 21300          # 5h 55min
MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def clean_text(text: str) -> str:
    return text.replace('`', '')


def load_channels() -> list[str]:
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


# ---------- Rubika API (direct multipart) ----------
def send_text_to_rubika(channel_name: str, text: str, msg_date: datetime) -> bool:
    text = clean_text(text)
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"=============\n{channel_name}\n{date_str}\n=============\n\n{text}"
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    all_ok = True
    for chat_id in RUBIKA_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": formatted}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"✅ Text forwarded to {chat_id} from {channel_name}")
            else:
                logger.error(f"❌ Rubika error for {chat_id}: {resp.status_code} {resp.text}")
                all_ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {chat_id}: {e}")
            all_ok = False
    return all_ok


def send_media_direct(
    channel_name: str,
    msg_date: datetime,
    file_bytes: bytes,
    filename: str,
    media_type: str,
    caption: str = "",
) -> bool:
    """
    Send a media file directly to Rubika using multipart/form-data.
    media_type must be one of: 'photo', 'video', 'audio', 'voice', 'document'.
    """
    caption = clean_text(caption)
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel_name}\n{date_str}\n============="
    full_caption = f"{header}\n\n{caption}" if caption else header

    # Map media_type to Rubika method name
    method_map = {
        "photo": "sendPhoto",
        "video": "sendVideo",
        "audio": "sendAudio",
        "voice": "sendVoice",
        "document": "sendDocument",
    }
    method = method_map.get(media_type, "sendDocument")
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"

    all_ok = True
    for chat_id in RUBIKA_CHAT_IDS:
        try:
            # Multipart data as per Rubika docs
            files = {"file": (filename, BytesIO(file_bytes))}
            data = {
                "chat_id": chat_id,
                "caption": full_caption,
                "file_name": filename,
                "file_type": media_type,        # required
            }
            resp = requests.post(url, data=data, files=files, timeout=30)

            # Log full response for debugging
            logger.info(f"{method} response [{resp.status_code}]: {resp.text}")

            if resp.status_code == 200:
                result = resp.json()
                if result.get("status") == "OK" or result.get("ok"):
                    logger.info(f"✅ {media_type} sent to {chat_id} from {channel_name}")
                else:
                    logger.error(f"❌ Rubika media error for {chat_id}: {resp.text}")
                    all_ok = False
            else:
                logger.error(f"❌ Rubika media HTTP error for {chat_id}: {resp.status_code} {resp.text}")
                all_ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {chat_id}: {e}")
            all_ok = False
    return all_ok


# ---------- Core forwarding ----------
async def forward_message(client, message, channel_name, state, skip_duplicate_check=False):
    msg_date = message.date

    if not skip_duplicate_check:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            logger.debug(f"Skipping duplicate {message.id}")
            return

    # Text only
    if message.text and not message.media:
        if send_text_to_rubika(channel_name, message.text, msg_date):
            state[channel_name] = message.id
            save_state(state)
        return

    # Media message
    if not message.file or not message.file.size:
        logger.warning(f"Message {message.id} has no file size info, skipping.")
        return

    file_size = message.file.size
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        skip_text = (
            f"⚠️ Large file skipped ({size_mb:.1f} MB)\n"
            f"Original filename: {message.file.name or 'unknown'}"
        )
        send_text_to_rubika(channel_name, skip_text, msg_date)
        state[channel_name] = message.id
        save_state(state)
        return

    # Determine media type
    if message.photo:
        media_type, filename = "photo", "photo.jpg"
    elif message.video:
        media_type, filename = "video", message.file.name or "video.mp4"
    elif message.audio:
        media_type, filename = "audio", message.file.name or "audio.mp3"
    elif message.voice:
        media_type, filename = "voice", message.file.name or "voice.ogg"
    else:
        media_type, filename = "document", message.file.name or "unknown_file"

    caption = message.text or ""

    # Download file into memory
    try:
        media_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(media_bytes)} bytes) from {channel_name}")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return

    # Send directly with multipart
    if send_media_direct(channel_name, msg_date, media_bytes, filename, media_type, caption):
        state[channel_name] = message.id
        save_state(state)


# ---------- Startup procedures ----------
async def catch_up(client, channels, state):
    if not state:
        logger.info("First run – initialising state without forwarding old messages (debug will resend last 3).")
        for channel in channels:
            try:
                messages = await client.get_messages(channel, limit=1)
                if messages and messages[0]:
                    state[channel] = messages[0].id
                    logger.info(f"Start marker for {channel} at message {messages[0].id}")
                else:
                    state[channel] = 0
            except Exception as e:
                logger.error(f"Failed to initialise {channel}: {e}")
        save_state(state)
        return

    logger.info("Checking for missed messages…")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=10)
            if not messages:
                continue
            for msg in reversed(messages):
                last_id = state.get(channel, 0)
                if msg.id <= last_id:
                    continue
                if not msg.text and not msg.media:
                    continue
                logger.info(f"Missed message {msg.id} from {channel}")
                await forward_message(client, msg, channel, state)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


async def debug_resend_last3(client, channels, state):
    logger.info("DEBUG: Resending the 3 most recent messages from each channel (first run only).")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=3)
            if not messages:
                continue
            for msg in reversed(messages):
                if not msg.text and not msg.media:
                    continue
                logger.info(f"DEBUG: resending {msg.id} from {channel}")
                await forward_message(client, msg, channel, state, skip_duplicate_check=True)
        except Exception as e:
            logger.error(f"DEBUG resend error for {channel}: {e}")


# ---------- Main ----------
async def main():
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_IDS]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    channels = load_channels()
    logger.info(f"Monitoring: {channels}")
    logger.info(f"Forwarding to Rubika chat(s): {RUBIKA_CHAT_IDS}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready")

    state = load_state()
    first_run = (state == {})

    await catch_up(client, channels, state)

    if first_run:
        await debug_resend_last3(client, channels, state)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            channel_name = chat.title
            logger.info(f"New message from {channel_name}")
            await forward_message(client, event.message, channel_name, state)
        except Exception as e:
            logger.error(f"Handler error: {e}")

    logger.info("Now forwarding messages in real‑time…")
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed >= RUN_DURATION:
            logger.info(f"Time limit reached ({elapsed/3600:.2f}h), exiting.")
            break
        await asyncio.sleep(30)

    await client.disconnect()
    logger.info("Session closed.")


if __name__ == "__main__":
    asyncio.run(main())
