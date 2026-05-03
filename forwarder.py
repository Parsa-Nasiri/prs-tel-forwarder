import os
import json
import time
import asyncio
import logging
import sys
from pathlib import Path

import requests
from telethon import TelegramClient, events

# ---------- Configuration (via environment variables) ----------
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
RUBIKA_BOT_TOKEN = os.environ.get("RUBIKA_BOT_TOKEN", "")
RUBIKA_CHAT_ID = os.environ.get("RUBIKA_CHAT_ID", "")

# Channels list (editable JSON file)
CHANNELS_FILE = Path("channels.json")
# Persistent state (tracks last forwarded message ID per channel)
STATE_FILE = Path("state.json")
# Run duration: 5 hours 40 minutes (20400 seconds). This exits 20 minutes before
# the GitHub Actions 6‑hour timeout, allowing the next run to start seamlessly.
RUN_DURATION = 20400  # seconds

# ---------- Logging ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


def load_channels() -> list[str]:
    """Load channel list from channels.json."""
    if not CHANNELS_FILE.exists():
        logger.error(f"Channels file {CHANNELS_FILE} not found!")
        sys.exit(1)
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        channels = json.load(f)
    if not isinstance(channels, list):
        logger.error("channels.json must be a JSON array.")
        sys.exit(1)
    return channels


def load_state() -> dict:
    """Load persistent state (channel_name -> last_message_id)."""
    if STATE_FILE.exists():
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict):
    """Save state to file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def send_to_rubika(channel_name: str, text: str) -> bool:
    """Format the message and send it to the Rubika bot."""
    formatted = f"=============\n{channel_name}\n=============\n\n{text}"
    url = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": RUBIKA_CHAT_ID,
        "text": formatted,
    }
    try:
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            logger.info(f"✅ Forwarded message from {channel_name}")
            return True
        else:
            logger.error(f"❌ Rubika API error: {resp.status_code} {resp.text}")
            return False
    except Exception as e:
        logger.error(f"❌ Failed to send to Rubika: {e}")
        return False


async def catch_up_missed_messages(client: TelegramClient, channels: list[str], state: dict):
    """Fetch recent messages for each channel and forward any that were missed."""
    logger.info("Checking for missed messages...")
    for channel in channels:
        try:
            messages = await client.get_messages(channel, limit=10)
            if not messages:
                continue
            # Process oldest first to maintain order
            for msg in reversed(messages):
                if not msg.text:
                    continue
                last_id = state.get(channel, 0)
                if msg.id > last_id:
                    logger.info(f"Caught up missed message {msg.id} from {channel}")
                    success = send_to_rubika(channel, msg.text)
                    if success:
                        state[channel] = msg.id
                        save_state(state)
        except Exception as e:
            logger.error(f"Error fetching messages from {channel}: {e}")


async def main():
    if not all([API_ID, API_HASH, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_ID]):
        logger.error("Missing required environment variables: API_ID, API_HASH, RUBIKA_BOT_TOKEN, RUBIKA_CHAT_ID")
        sys.exit(1)

    channels = load_channels()
    logger.info(f"Monitoring channels: {channels}")

    state = load_state()

    client = TelegramClient("telegram_session", API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client started")

    # Catch up on any messages missed between runs
    await catch_up_missed_messages(client, channels, state)

    # Register handler for new messages
    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            channel_name = chat.title
            text = event.message.text
            if not text:
                return
            logger.info(f"New message from {channel_name}")
            success = send_to_rubika(channel_name, text)
            if success:
                state[channel_name] = event.message.id
                save_state(state)
        except Exception as e:
            logger.error(f"Error handling message: {e}")

    logger.info("Listening for new messages...")
    start_time = time.time()

    # Keep running until just before the GitHub Actions timeout
    while True:
        elapsed = time.time() - start_time
        if elapsed >= RUN_DURATION:
            logger.info(f"Runtime reached {elapsed/3600:.2f} hours, exiting cleanly.")
            break
        await asyncio.sleep(60)

    await client.disconnect()
    logger.info("Client disconnected. Script finished.")


if __name__ == "__main__":
    asyncio.run(main())
