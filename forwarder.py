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

# ---------- Configuration (from environment) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]

# Support one or multiple chat IDs (comma-separated)
raw_chat_ids = os.environ.get("RUBIKA_CHAT_IDS") or os.environ["RUBIKA_CHAT_ID"]
RUBIKA_CHAT_IDS = [cid.strip() for cid in raw_chat_ids.split(",") if cid.strip()]

CHANNELS_FILE = Path("channels.json")
STATE_FILE = Path("state.json")
RUN_DURATION = 21300  # 5h55m

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def load_channels() -> list[str]:
    if not CHANNELS_FILE.exists():
        logger.error(f"File {CHANNELS_FILE} not found!")
        sys.exit(1)
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        channels = json.load(f)
    if not isinstance(channels, list):
        logger.error("channels.json must be a JSON array.")
        sys.exit(1)
    return channels


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_to_rubika(channel_name: str, text: str, msg_date: datetime) -> bool:
    """
    Send the formatted message to ALL configured Rubika chat IDs.
    Returns True if all sends were successful, False otherwise.
    """
    date_str = msg_date.strftime("%Y-%m-%d %H:%M:%S")
    formatted = f"=============\n{channel_name}\n{date_str}\n=============\n\n{text}"
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    all_ok = True

    for chat_id in RUBIKA_CHAT_IDS:
        payload = {"chat_id": chat_id, "text": formatted}
        try:
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                logger.info(f"✅ Forwarded to {chat_id} from {channel_name}")
            else:
                logger.error(f"❌ Rubika error for {chat_id}: {resp.status_code} {resp.text}")
                all_ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {chat_id}: {e}")
            all_ok = False

    return all_ok


async def catch_up(client: TelegramClient, channels: list[str], state: dict):
    """
    If state is empty (first run), just record the latest message ID
    without forwarding anything. Otherwise, forward only missed messages.
    """
    if not state:
        logger.info("First run detected – initialising state without forwarding old messages.")
        for channel in channels:
            try:
                messages = await client.get_messages(channel, limit=1)
                if messages and messages[0]:
                    last_id = messages[0].id
                    state[channel] = last_id
                    logger.info(f"Start marker for {channel} at message {last_id}")
                else:
                    state[channel] = 0
            except Exception as e:
                logger.error(f"Failed to initialise state for {channel}: {e}")
        save_state(state)
        return

    logger.info("Checking for missed messages…")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=10)
            if not messages:
                continue
            for msg in reversed(messages):  # oldest first
                if not msg.text:
                    continue
                last_id = state.get(channel, 0)
                if msg.id > last_id:
                    logger.info(f"Missed message {msg.id} from {channel}")
                    if send_to_rubika(channel, msg.text, msg.date):
                        state[channel] = msg.id
                        save_state(state)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


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
    await catch_up(client, channels, state)

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            channel_name = chat.title
            text = event.message.text
            if not text:
                return
            last_id = state.get(channel_name, 0)
            if event.message.id <= last_id:
                logger.debug("Duplicate message, skipping")
                return
            logger.info(f"New message from {channel_name}")
            if send_to_rubika(channel_name, text, event.message.date):
                state[channel_name] = event.message.id
                save_state(state)
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
