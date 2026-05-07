import os
import json
import time
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime

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
RUN_DURATION = 20400          # 5h 40m (ends 20 min before GitHub 6h limit)

MAX_FILE_SIZE_MB = {
    "Image": 10,
    "Video": 50,
    "File": 50,
    "Music": 50,
    "Voice": 10,
    "Gif": 50,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


# ---------- Reaction helpers ----------
def get_top_reactions(message) -> str:
    """Return '❤️33 🍌12 👍3' from a Telethon message."""
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
        logger.error(f"{CHANNELS_FILE} not found!")
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


# ---------- Rubika API helpers ----------
def _rubika_post(endpoint: str, payload: dict) -> dict | None:
    """Generic POST to Rubika Bot API, returns parsed JSON or None."""
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/{endpoint}"
    try:
        resp = requests.post(url, json=payload, timeout=15)
        if resp.status_code == 200:
            return resp.json()
        else:
            logger.error(f"Rubika {endpoint} HTTP {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Rubika {endpoint} exception: {e}")
        return None

def _extract_field(data: dict, *paths: str) -> str | None:
    """Walk nested dict keys ('data','message_id') to find a value."""
    for path in paths:
        parts = path.split(".")
        cur = data
        for part in parts:
            if isinstance(cur, dict):
                cur = cur.get(part)
            else:
                cur = None
                break
        if cur is not None:
            return str(cur)
    return None


# ---------- Rubika upload flow ----------
def _rubika_file_type(telegram_media) -> str:
    """Map Telethon media to Rubika FileTypeEnum."""
    if hasattr(telegram_media, 'photo') and telegram_media.photo:
        return "Image"
    if hasattr(telegram_media, 'video') and telegram_media.video:
        return "Video"
    if hasattr(telegram_media, 'voice') and telegram_media.voice:
        return "Voice"
    if hasattr(telegram_media, 'audio') and telegram_media.audio:
        return "Music"
    if hasattr(telegram_media, 'document') and telegram_media.document:
        # Check if GIF by mime type
        mime = getattr(telegram_media.document, 'mime_type', '')
        if mime == "video/mp4" and getattr(telegram_media, 'gif', False):
            return "Gif"
        return "File"
    return "File"

def upload_to_rubika(file_bytes: bytes, filename: str, file_type: str) -> str | None:
    """
    Full Rubika upload flow:
    1. requestSendFile → upload_url
    2. POST file to upload_url → file_id
    Returns file_id or None.
    """
    # Step 1 – get upload URL
    req = _rubika_post("requestSendFile", {"type": file_type})
    if not req:
        return None
    upload_url = _extract_field(req, "data.upload_url", "upload_url", "result.upload_url")
    if not upload_url:
        logger.error(f"requestSendFile returned no upload_url: {req}")
        return None

    logger.info(f"Got upload_url for {file_type}")

    # Step 2 – upload file to the given URL
    try:
        resp = requests.post(upload_url, files={"file": (filename, file_bytes)}, timeout=60)
        if resp.status_code != 200:
            logger.error(f"Upload to Rubika storage failed: {resp.status_code} {resp.text}")
            return None
        data = resp.json()
        file_id = _extract_field(data, "data.file_id", "file_id", "result.file_id")
        if file_id:
            logger.info(f"Uploaded {filename}, got file_id={file_id}")
            return file_id
        else:
            logger.error(f"Upload response missing file_id: {data}")
            return None
    except Exception as e:
        logger.error(f"Upload exception: {e}")
        return None


# ---------- Sending ----------
def _build_header(channel_name: str, msg_date: datetime) -> str:
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    return f"=============\n{channel_name}\n{date_str}\n============="

def send_text_to_rubika(chat_id: str, text: str) -> tuple[bool, str | None]:
    data = _rubika_post("sendMessage", {"chat_id": chat_id, "text": text})
    if not data:
        return False, None
    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data, "data.message_id", "message_id", "result.message_id")
        return True, msg_id
    logger.error(f"sendMessage failed: {data}")
    return False, None

def send_file_to_rubika(chat_id: str, file_id: str, caption: str) -> tuple[bool, str | None]:
    data = _rubika_post("sendFile", {
        "chat_id": chat_id,
        "file_id": file_id,
        "text": caption,
    })
    if not data:
        return False, None
    if data.get("status") == "OK" or data.get("ok"):
        msg_id = _extract_field(data, "data.message_id", "message_id", "result.message_id")
        return True, msg_id
    logger.error(f"sendFile failed: {data}")
    return False, None

def edit_text_in_rubika(chat_id: str, message_id: str, new_text: str) -> bool:
    data = _rubika_post("editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
    })
    return data is not None and (data.get("status") == "OK" or data.get("ok"))


# ---------- Delayed reaction edits ----------
pending_edits: dict[tuple[str, int], list[dict]] = {}

async def delayed_reaction_updates(client: TelegramClient, channel_name: str, tg_msg_id: int):
    """Edit the Rubika message after 5 min and again after 15 min."""
    key = (channel_name, tg_msg_id)
    entries = pending_edits.get(key)
    if not entries:
        return

    # First edit at +5 min
    await asyncio.sleep(300)
    await _apply_reaction_edit(client, channel_name, tg_msg_id, entries, "5 min")

    # Second edit at +15 min (10 more minutes)
    await asyncio.sleep(600)
    await _apply_reaction_edit(client, channel_name, tg_msg_id, entries, "15 min")

    pending_edits.pop(key, None)

async def _apply_reaction_edit(client, channel_name, tg_msg_id, entries, label):
    try:
        msg = await client.get_messages(channel_name, ids=tg_msg_id)
        if not msg:
            logger.warning(f"{label} edit: TG msg {tg_msg_id} gone")
            return
        reaction_str = get_top_reactions(msg)
        if not reaction_str:
            logger.info(f"{label} edit: no reactions yet for {tg_msg_id}")
            return
        reaction_line = f"\n{reaction_str}"
        for entry in entries:
            new_text = entry["full_original_text"] + reaction_line
            if edit_text_in_rubika(entry["chat_id"], entry["rubika_msg_id"], new_text):
                logger.info(f"✅ {label} edit: msg {entry['rubika_msg_id']} updated")
            else:
                logger.error(f"❌ {label} edit failed for {entry['rubika_msg_id']}")
    except Exception as e:
        logger.error(f"Error during {label} edit for {tg_msg_id}: {e}")


# ---------- Core forwarding ----------
async def forward_message(client, message, channel_name, state, skip_dup=False):
    msg_date = message.date

    if not skip_dup:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            return

    # ---------- TEXT ONLY ----------
    if message.text and not message.media:
        header = _build_header(channel_name, msg_date)
        full_text = header + "\n\n" + message.text.replace('`', '')

        key = (channel_name, message.id)
        pending_edits[key] = []
        all_ok = True
        for chat_id in RUBIKA_CHAT_IDS:
            ok, rubika_id = send_text_to_rubika(chat_id, full_text)
            if ok and rubika_id:
                pending_edits[key].append({
                    "chat_id": chat_id,
                    "rubika_msg_id": rubika_id,
                    "full_original_text": full_text,
                })
                logger.info(f"✅ Text forwarded to {chat_id} from {channel_name}")
            else:
                all_ok = False
        if all_ok:
            state[channel_name] = message.id
            save_state(state)
            asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))
        return

    # ---------- MEDIA ----------
    if not message.media:
        return  # nothing to forward

    if not message.file or not message.file.size:
        logger.warning(f"Msg {message.id} has no file size, skipping")
        state[channel_name] = message.id
        save_state(state)
        return

    file_type = _rubika_file_type(message.media)
    max_mb = MAX_FILE_SIZE_MB.get(file_type, 50)
    if message.file.size > max_mb * 1024 * 1024:
        size_mb = message.file.size / (1024 * 1024)
        skip_msg = f"⚠️ Large {file_type} ({size_mb:.1f} MB) skipped"
        for chat_id in RUBIKA_CHAT_IDS:
            send_text_to_rubika(chat_id, skip_msg)
        state[channel_name] = message.id
        save_state(state)
        return

    # Determine filename
    if file_type == "Image":
        filename = "photo.jpg"
    elif file_type == "Voice":
        filename = "voice.ogg"
    elif file_type == "Music":
        filename = message.file.name or "audio.mp3"
    elif file_type == "Video":
        filename = message.file.name or "video.mp4"
    else:
        filename = message.file.name or "file"

    # Download from Telegram
    try:
        file_bytes = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {file_type} ({len(file_bytes)} B) from {channel_name}")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return

    # Upload to Rubika
    file_id = upload_to_rubika(file_bytes, filename, file_type)
    if not file_id:
        logger.error("Failed to get Rubika file_id, skipping media")
        return

    # Build caption
    header = _build_header(channel_name, msg_date)
    caption_text = message.text or ""
    full_caption = f"{header}\n\n{caption_text.replace('`', '')}" if caption_text else header

    key = (channel_name, message.id)
    pending_edits[key] = []
    all_ok = True
    for chat_id in RUBIKA_CHAT_IDS:
        ok, rubika_id = send_file_to_rubika(chat_id, file_id, full_caption)
        if ok and rubika_id:
            pending_edits[key].append({
                "chat_id": chat_id,
                "rubika_msg_id": rubika_id,
                "full_original_text": full_caption,
            })
            logger.info(f"✅ {file_type} sent to {chat_id} from {channel_name}")
        else:
            all_ok = False

    if all_ok:
        state[channel_name] = message.id
        save_state(state)
        asyncio.ensure_future(delayed_reaction_updates(client, channel_name, message.id))


# ---------- Startup ----------
async def catch_up(client, channels, state):
    if not state:
        logger.info("First run – initialising state without forwarding old messages")
        for channel in channels:
            try:
                msgs = await client.get_messages(channel, limit=1)
                state[channel] = msgs[0].id if msgs and msgs[0] else 0
                logger.info(f"Start marker for {channel} at msg {state[channel]}")
            except Exception as e:
                logger.error(f"Failed to init {channel}: {e}")
        save_state(state)
        return

    logger.info("Checking for missed messages…")
    for channel in channels:
        try:
            msgs = await client.get_messages(channel, limit=10)
            if not msgs:
                continue
            for msg in reversed(msgs):
                if msg.id <= state.get(channel, 0):
                    continue
                if not msg.text and not msg.media:
                    continue
                logger.info(f"Missed msg {msg.id} from {channel}")
                await forward_message(client, msg, channel, state)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


# ---------- Main ----------
async def main():
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_IDS]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    channels = load_channels()
    logger.info(f"Monitoring: {channels}")
    logger.info(f"Rubika chats: {RUBIKA_CHAT_IDS}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready")

    state = load_state()
    await catch_up(client, channels, state)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            await forward_message(client, event.message, chat.title, state)
        except Exception as e:
            logger.error(f"Handler error: {e}")

    logger.info("Now forwarding messages in real‑time…")
    start = time.time()

    while True:
        if time.time() - start >= RUN_DURATION:
            logger.info(f"Time limit ({RUN_DURATION/3600:.2f}h) reached, exiting")
            break
        await asyncio.sleep(30)

    await client.disconnect()
    logger.info("Session closed.")


if __name__ == "__main__":
    asyncio.run(main())
