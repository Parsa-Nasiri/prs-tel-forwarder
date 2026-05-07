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
RUN_DURATION = 20400          # 5h 40m (ends 20 min before the 6‑hour limit)

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


# ---------- Rubika API ----------
def send_text_to_rubika(chat_id: str, text: str) -> tuple[bool, str | None]:
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        data = resp.json()
        # Rubika can return "status": "OK" or "ok" : true
        if data.get("status") == "OK" or data.get("ok"):
            # Try multiple possible paths for message_id
            msg_id = (
                data.get("message_id") or
                data.get("result", {}).get("message_id") or
                data.get("data", {}).get("message_id")
            )
            if msg_id:
                return True, str(msg_id)
            else:
                logger.error(f"Could not find message_id in response: {data}")
                return False, None
        else:
            logger.error(f"Rubika API error: {resp.text}")
            return False, None
    except Exception as e:
        logger.error(f"sendMessage exception: {e}")
        return False, None

def edit_text_in_rubika(chat_id: str, message_id: str, new_text: str) -> bool:
    """Edit a text message."""
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


# ---------- Delayed reaction update ----------
pending_edits: dict[tuple[str, int], list[dict]] = {}
"""
Key: (channel_name, telegram_msg_id)
Value: list of dicts with keys: chat_id, rubika_msg_id, full_original_text
"""

async def delayed_reaction_update(client: TelegramClient, channel_name: str, tg_msg_id: int):
    """Wait 5 minutes, then fetch reactions and edit the Rubika messages."""
    await asyncio.sleep(300)   # 5 minutes
    key = (channel_name, tg_msg_id)
    entries = pending_edits.pop(key, [])
    if not entries:
        return

    try:
        # Re‑fetch the message to get current reactions
        msg = await client.get_messages(channel_name, ids=tg_msg_id)
        if not msg:
            logger.warning(f"Delayed update: TG message {tg_msg_id} not found.")
            return
        reaction_str = get_top_reactions(msg)
        if not reaction_str:
            logger.info(f"No reactions for {tg_msg_id} after 5 min, skipping edit.")
            return
        reaction_line = f"\n{reaction_str}"

        for entry in entries:
            new_text = entry["full_original_text"] + reaction_line
            if edit_text_in_rubika(entry["chat_id"], entry["rubika_msg_id"], new_text):
                logger.info(f"✅ Edited message {entry['rubika_msg_id']} with reactions.")
            else:
                logger.error(f"❌ Failed to edit {entry['rubika_msg_id']}")
    except Exception as e:
        logger.error(f"Delayed reaction update error for {tg_msg_id}: {e}")


# ---------- Core forwarding logic ----------
async def forward_message(client, message, channel_name, state, skip_duplicate_check=False):
    """Forward a text message only; schedule reaction edit after 5 min."""
    msg_date = message.date

    if not skip_duplicate_check:
        last_id = state.get(channel_name, 0)
        if message.id <= last_id:
            logger.debug(f"Skipping duplicate message {message.id}")
            return

    # IGNORE ALL MEDIA MESSAGES
    if message.media:
        logger.info(f"Skipping media message {message.id} from {channel_name}")
        # still mark as processed so we don't loop on it
        state[channel_name] = message.id
        save_state(state)
        return

    # Only text messages without media
    if not message.text:
        return

    # Build the header
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel_name}\n{date_str}\n=============\n\n"
    full_text = header + message.text.replace('`', '')

    # Prepare tracking
    track_key = (channel_name, message.id)
    pending_edits[track_key] = []

    all_ok = True
    for chat_id in RUBIKA_CHAT_IDS:
        success, rubika_msg_id = send_text_to_rubika(chat_id, full_text)
        if success and rubika_msg_id:
            pending_edits[track_key].append({
                "chat_id": chat_id,
                "rubika_msg_id": rubika_msg_id,
                "full_original_text": full_text
            })
            logger.info(f"✅ Text forwarded to {chat_id} from {channel_name}")
        else:
            all_ok = False

    if all_ok:
        state[channel_name] = message.id
        save_state(state)
        # Schedule the delayed edit
        asyncio.ensure_future(delayed_reaction_update(client, channel_name, message.id))


# ---------- Startup ----------
async def catch_up(client, channels, state):
    """First run: only mark latest ID. Subsequent runs: forward missed texts."""
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
                # Only text messages (no media)
                if not msg.text or msg.media:
                    continue
                logger.info(f"Missed message {msg.id} from {channel}")
                await forward_message(client, msg, channel, state)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


async def debug_resend_last3(client, channels, state):
    """First run: resend the 3 most recent TEXT messages for verification."""
    logger.info("DEBUG: Resending the 3 most recent TEXT messages from each channel.")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=3)
            if not messages:
                continue
            for msg in reversed(messages):
                # Skip media or empty
                if not msg.text or msg.media:
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
