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

# ---------- Configuration (environment variables) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

# Support both single and multiple chat IDs (comma‑separated)
raw_chat_ids = os.environ.get("RUBIKA_CHAT_IDS") or os.environ["RUBIKA_CHAT_ID"]
RUBIKA_CHAT_IDS = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]

CHANNELS_FILE = Path("channels.json")
STATE_FILE = Path("state.json")
RUN_DURATION = 21300          # 5 hours 55 minutes
MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------- Helper ----------
def clean_text(text: str) -> str:
    """Remove backticks from a string."""
    return text.replace('`', '')


# ---------- File I/O ----------
def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        logger.error(f"File {CHANNELS_FILE} not found!")
        sys.exit(1)
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


# ---------- Rubika API communication ----------
def send_text_to_rubika(channel_name: str, text: str, msg_date: datetime) -> bool:
    """Send a plain text message (backticks removed) to all Rubika chats."""
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


def upload_file_to_rubika(file_bytes: bytes, filename: str, file_type: str, chat_id: str) -> str | None:
    """
    Upload a file to Rubika and return its file_id.
    Required parameters: file, chat_id, file_name, file_type.
    """
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/uploadFile"
    try:
        files = {"file": (filename, BytesIO(file_bytes))}
        data = {
            "chat_id": chat_id,
            "file_name": filename,
            "file_type": file_type,
        }
        resp = requests.post(url, files=files, data=data, timeout=30)
        logger.debug(f"uploadFile response [{resp.status_code}]: {resp.text}")

        if resp.status_code == 200:
            result = resp.json()
            if isinstance(result, dict) and "file_id" in result:
                return result["file_id"]
            else:
                logger.error(f"uploadFile missing file_id: {resp.text}")
        else:
            logger.error(f"uploadFile HTTP error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.error(f"uploadFile exception: {e}")
    return None


def send_media_by_id(
    channel_name: str,
    msg_date: datetime,
    file_id: str,
    media_type: str,
    caption: str = "",
) -> bool:
    """
    Send a media file using its file_id.
    media_type: 'photo', 'video', 'audio', 'voice', 'document'
    """
    caption = clean_text(caption)
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel_name}\n{date_str}\n============="
    full_caption = f"{header}\n\n{caption}" if caption else header

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
        payload = {
            "chat_id": chat_id,
            "file": file_id,            # string file_id, not bytes
            "caption": full_caption,
        }
        try:
            resp = requests.post(url, json=payload, timeout=10)
            logger.debug(f"{method} response [{resp.status_code}]: {resp.text}")

            if resp.status_code == 200:
                # Some Rubika methods may return a JSON with status field
                data = resp.json()
                if data.get("status") == "OK" or data.get("ok"):
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


# ---------- Core forwarding logic ----------
async def forward_message(client, message, channel_name, state, skip_duplicate_check=False):
    """
    Process a single Telegram message: text or media (≤50 MB).
    If skip_duplicate_check is True (debug mode), the ID check is bypassed.
    """
    msg_date = message.date

    if not skip_duplicate_check:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            logger.debug(f"Skipping duplicate message {message.id}")
            return

    # 1. Pure text message (no media)
    if message.text and not message.media:
        if send_text_to_rubika(channel_name, message.text, msg_date):
            state[channel_name] = message.id
            save_state(state)
        return

    # 2. Media message
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
        logger.info(f"Skipping large file ({size_mb:.1f} MB) from {channel_name}")
        send_text_to_rubika(channel_name, skip_text, msg_date)
        # Still mark as processed so we don't try again
        state[channel_name] = message.id
        save_state(state)
        return

    # Determine media type and filename
    if message.photo:
        media_type, filename = "photo", "photo.jpg"
    elif message.video:
        media_type, filename = "video", message.file.name or "video.mp4"
    elif message.audio:
        media_type, filename = "audio", message.file.name or "audio.mp3"
    elif message.voice:
        media_type, filename = "voice", message.file.name or "voice.ogg"
    else:
        media_type, filename = "document", message.file.name or "file"

    caption = message.text or ""

    # Download the file into memory
    try:
        media_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(media_bytes)} bytes) from {channel_name}")
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return

    # Upload to Rubika using the first chat ID (file_id is bound to that chat)
    # For multiple users, you may want to upload once per chat or reuse the ID.
    upload_chat = RUBIKA_CHAT_IDS[0]
    file_id = upload_file_to_rubika(media_bytes, filename, media_type, upload_chat)
    if not file_id:
        logger.error("Failed to obtain file_id from Rubika, skipping.")
        return

    # Send the media using the obtained file_id
    if send_media_by_id(channel_name, msg_date, file_id, media_type, caption):
        state[channel_name] = message.id
        save_state(state)


# ---------- Startup procedures ----------
async def catch_up(client, channels, state):
    """
    On first run (state is empty): just mark the latest message as processed.
    On subsequent runs: forward any messages missed during the gap.
    """
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
            for msg in reversed(messages):               # oldest first
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
    """
    Only on the very first run: resend the 3 most recent messages of each channel
    so you can immediately verify that everything is working.
    """
    logger.info("DEBUG: Resending the 3 most recent messages from each channel (first run only).")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=3)
            if not messages:
                continue
            for msg in reversed(messages):               # oldest first
                if not msg.text and not msg.media:
                    continue
                logger.info(f"DEBUG: resending {msg.id} from {channel}")
                # Bypass duplicate check for these
                await forward_message(client, msg, channel, state, skip_duplicate_check=True)
        except Exception as e:
            logger.error(f"DEBUG resend error for {channel}: {e}")


# ---------- Main loop ----------
async def main():
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_IDS]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    channels = load_channels()
    logger.info(f"Monitoring channels: {channels}")
    logger.info(f"Forwarding to Rubika chat(s): {RUBIKA_CHAT_IDS}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready")

    state = load_state()
    first_run = (state == {})          # True only if state.json was empty

    await catch_up(client, channels, state)

    # On the very first run, resend the last 3 messages for debugging
    if first_run:
        await debug_resend_last3(client, channels, state)

    # Live event handler
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
