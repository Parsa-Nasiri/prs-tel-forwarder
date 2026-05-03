import os
import json
import time
import asyncio
import logging
import sys
from pathlib import Path

import requests
from telethon import TelegramClient, events
from telethon.sessions import StringSession

# ---------- Configuration (all from environment) ----------
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
STRING_SESSION = os.environ["STRING_SESSION"]  # Telethon string session
RUBIKA_BOT_TOKEN = os.environ["RUBIKA_BOT_TOKEN"]
RUBIKA_CHAT_ID = os.environ["RUBIKA_CHAT_ID"]

CHANNELS_FILE = Path("channels.json")
STATE_FILE = Path("state.json")
# Run exactly 5 hours 55 minutes (21300 seconds) – leaves a 5‑min gap before next run
RUN_DURATION = 21300

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
    return channels


def load_state() -> dict:
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_to_rubika(channel_name: str, text: str) -> bool:
    formatted = f"=============\n{channel_name}\n=============\n\n{text}"
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": RUBIKA_CHAT_ID, "text": formatted}
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"✅ Forwarded from {channel_name}")
            return True
        logger.error(f"Rubika error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Request failed: {e}")
    return False


async def catch_up(client: TelegramClient, channels: list[str], state: dict):
    """Fetch the last 10 messages of each channel, forward any missed ones."""
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
                    if send_to_rubika(channel, msg.text):
                        state[channel] = msg.id
                        save_state(state)
        except Exception as e:
            logger.error(f"Error catching up {channel}: {e}")


async def main():
    if not all([API_ID, API_HASH, STRING_SESSION, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_ID]):
        logger.error("Missing required environment variables!")
        sys.exit(1)

    channels = load_channels()
    logger.info(f"Monitoring: {channels}")

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
            logger.info(f"New message from {channel_name}")
            last_id = state.get(channel_name, 0)
            if event.message.id <= last_id:
                logger.debug("Duplicate message, skipping")
                return
            if send_to_rubika(channel_name, text):
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
