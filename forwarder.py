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
from telethon.tl.types import MessageReactions

# ---------- Configuration (environment variables) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

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

# ---------- Reaction helpers ----------
def get_top_reactions(message) -> str:
    """Return a string like '❤️33 🍌12 👍3' from a Telethon message."""
    if not message.reactions or not message.reactions.results:
        return ""
    counts = []
    for r in message.reactions.results:
        emoji = r.reaction.emoticon if hasattr(r.reaction, 'emoticon') else str(r.reaction)
        counts.append((emoji, r.count))
    counts.sort(key=lambda x: x[1], reverse=True)
    top = counts[:3]
    return " ".join(f"{emoji}{count}" for emoji, count in top)


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


# ---------- Rubika API (extended) ----------
def _rubika_send_text(chat_id: str, text: str) -> tuple[bool, str | None]:
    """Send a text message and return (success, message_id)."""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            msg_id = data.get("result", {}).get("message_id")
            if msg_id:
                return True, str(msg_id)
            else:
                logger.error(f"sendMessage missing message_id: {resp.text}")
                return False, None
        else:
            logger.error(f"sendMessage HTTP {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        logger.error(f"sendMessage exception: {e}")
        return False, None

def _rubika_send_media(chat_id: str, method: str, file_id: str, caption: str) -> tuple[bool, str | None]:
    """Send media by file_id and return (success, message_id)."""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{method}"
    payload = {"chat_id": chat_id, "file": file_id, "caption": caption}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            msg_id = data.get("result", {}).get("message_id")
            if msg_id:
                return True, str(msg_id)
            else:
                logger.error(f"{method} missing message_id: {resp.text}")
                return False, None
        else:
            logger.error(f"{method} HTTP {resp.status_code}: {resp.text}")
            return False, None
    except Exception as e:
        logger.error(f"{method} exception: {e}")
        return False, None

def edit_rubika_text(chat_id: str, message_id: str, new_text: str) -> bool:
    """Edit a text message in Rubika."""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": new_text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            logger.error(f"editMessageText HTTP {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"editMessageText exception: {e}")
        return False

def edit_rubika_caption(chat_id: str, message_id: str, new_caption: str) -> bool:
    """Edit a media caption in Rubika."""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/editMessageCaption"
    payload = {"chat_id": chat_id, "message_id": message_id, "caption": new_caption}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return True
        else:
            logger.error(f"editMessageCaption HTTP {resp.status_code}: {resp.text}")
            return False
    except Exception as e:
        logger.error(f"editMessageCaption exception: {e}")
        return False


# ---------- Delayed reaction update ----------
pending_edits: dict[tuple[str, int], list[dict]] = {}
"""
Structure:
    (channel_name, telegram_msg_id) → [
        {"chat_id": str, "rubika_msg_id": str, "type": "text"|"media"}
    ]
"""

async def delayed_reaction_update(client: TelegramClient, channel_name: str, tg_msg_id: int):
    """Wait 2 minutes, then fetch reactions and edit the Rubika messages."""
    await asyncio.sleep(120)
    key = (channel_name, tg_msg_id)
    entries = pending_edits.pop(key, [])
    if not entries:
        return

    try:
        # Fetch the latest version of the Telegram message
        msg = await client.get_messages(channel_name, ids=tg_msg_id)
        if not msg:
            logger.warning(f"Delayed update: TG message {tg_msg_id} not found.")
            return
        reaction_str = get_top_reactions(msg)
        if not reaction_str:
            logger.info(f"No reactions yet for {tg_msg_id} after 2 min, skipping edit.")
            return
        reaction_line = f"\n{reaction_str}"

        for entry in entries:
            chat_id = entry["chat_id"]
            rubika_id = entry["rubika_msg_id"]
            msg_type = entry["type"]

            if msg_type == "text":
                # Original text is unknown; we have to rebuild the whole message.
                # We'll store the original text in the entry when we send.
                # Let's add an "original_text" field to entries.
                original = entry.get("original_text", "")
                new_text = original + reaction_line
                edit_rubika_text(chat_id, rubika_id, new_text)
            else:  # media
                original_caption = entry.get("original_caption", "")
                new_caption = original_caption + reaction_line
                edit_rubika_caption(chat_id, rubika_id, new_caption)

    except Exception as e:
        logger.error(f"Delayed reaction update error for {tg_msg_id}: {e}")


# ---------- Core forwarding logic ----------
async def forward_message(client, message, channel_name, state, skip_duplicate_check=False):
    """Process a single message (text or media) and schedule a reaction update."""
    msg_date = message.date

    if not skip_duplicate_check:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            logger.debug(f"Skipping duplicate message {message.id}")
            return

    # Prepare tracking entry
    track_key = (channel_name, message.id)
    pending_edits[track_key] = []

    # 1. Pure text message
    if message.text and not message.media:
        date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
        header = f"=============\n{channel_name}\n{date_str}\n=============\n\n"
        full_text = header + message.text.replace('`', '')
        all_ok = True
        for chat_id in RUBIKA_CHAT_IDS:
            success, msg_id = _rubika_send_text(chat_id, full_text)
            if success and msg_id:
                pending_edits[track_key].append({
                    "chat_id": chat_id,
                    "rubika_msg_id": msg_id,
                    "type": "text",
                    "original_text": full_text
                })
                logger.info(f"✅ Text forwarded to {chat_id} from {channel_name}")
            else:
                all_ok = False

        if all_ok:
            state[channel_name] = message.id
            save_state(state)
            # Schedule reaction update
            asyncio.ensure_future(delayed_reaction_update(client, channel_name, message.id))
        return

    # 2. Media message
    if not message.file or not message.file.size:
        logger.warning(f"Message {message.id} has no file size info, skipping.")
        return

    file_size = message.file.size
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size / (1024 * 1024)
        skip_text = f"⚠️ Large file skipped ({size_mb:.1f} MB)\nOriginal filename: {message.file.name or 'unknown'}"
        logger.info(f"Skipping large file ({size_mb:.1f} MB) from {channel_name}")
        for chat_id in RUBIKA_CHAT_IDS:
            _rubika_send_text(chat_id, skip_text)
        state[channel_name] = message.id
        save_state(state)
        return

    # Determine media type & filename
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

    caption_text = message.text or ""

    # Download file
    try:
        media_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(media_bytes)} bytes) from {channel_name}")
    except Exception as e:
        logger.error(f"Failed to download media: {e}")
        return

    # Upload to Rubika (use first chat to get file_id)
    upload_chat = RUBIKA_CHAT_IDS[0]
    file_id = upload_file_to_rubika(media_bytes, filename, media_type, upload_chat)  # existing function
    if not file_id:
        logger.error("Failed to obtain file_id from Rubika, skipping.")
        return

    # Send to all chats
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel_name}\n{date_str}\n============="
    full_caption = f"{header}\n\n{caption_text.replace('`', '')}" if caption_text else header

    method_map = {"photo": "sendPhoto", "video": "sendVideo", "audio": "sendAudio",
                  "voice": "sendVoice", "document": "sendDocument"}
    method = method_map.get(media_type, "sendDocument")

    all_ok = True
    for chat_id in RUBIKA_CHAT_IDS:
        success, msg_id = _rubika_send_media(chat_id, method, file_id, full_caption)
        if success and msg_id:
            pending_edits[track_key].append({
                "chat_id": chat_id,
                "rubika_msg_id": msg_id,
                "type": "media",
                "original_caption": full_caption
            })
            logger.info(f"✅ {media_type} sent to {chat_id} from {channel_name}")
        else:
            all_ok = False

    if all_ok:
        state[channel_name] = message.id
        save_state(state)
        asyncio.ensure_future(delayed_reaction_update(client, channel_name, message.id))


# ---------- Startup procedures ----------
async def catch_up(client, channels, state):
    """First run: only mark latest ID. Subsequent runs: forward missed messages."""
    if not state:
        logger.info("First run – initialising state without forwarding old messages.")
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
    """Only on the very first run: resend the 3 most recent messages for verification."""
    logger.info("DEBUG: Resending the 3 most recent messages from each channel.")
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
    logger.info(f"Monitoring channels: {channels}")
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
