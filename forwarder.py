import os, json, time, asyncio, logging, sys, base64
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

# Admin chat IDs (can be comma‑separated)
ADMIN_CHAT_IDS = [cid.strip() for cid in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if cid.strip()]

# Private data repo details (set these as secrets)
DATA_REPO_OWNER = os.environ["DATA_REPO_OWNER"]        # your GitHub username
DATA_REPO_NAME = os.environ["DATA_REPO_NAME"]          # e.g., "my-bot-data"
DATA_REPO_TOKEN = os.environ["DATA_REPO_TOKEN"]        # PAT with repo scope

BASE_URL = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}"
CHANNELS_FILE = Path("channels.json")
STATE_FILE = Path("state.json")

RUN_DURATION = 20900          # 5h 48min (gap ~12 min)
MAX_FILE_SIZE = 50 * 1024 * 1024   # 50 MB

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# Global whitelist dictionary: chat_id -> name
whitelist = {}

# ---------- Private Repo API helpers ----------
def get_whitelist_from_repo():
    """Download whitelist.json from the private data repo, return dict."""
    url = f"https://api.github.com/repos/{DATA_REPO_OWNER}/{DATA_REPO_NAME}/contents/whitelist.json"
    headers = {"Authorization": f"token {DATA_REPO_TOKEN}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        content_b64 = resp.json().get("content", "")
        if content_b64:
            return json.loads(base64.b64decode(content_b64).decode('utf-8'))
    logger.error(f"Failed to fetch whitelist: {resp.status_code} {resp.text}")
    return {}

def push_whitelist_to_repo(wl_dict):
    """
    Update whitelist.json in the private repo.
    Uses the GitHub API to update or create the file.
    """
    url = f"https://api.github.com/repos/{DATA_REPO_OWNER}/{DATA_REPO_NAME}/contents/whitelist.json"
    headers = {"Authorization": f"token {DATA_REPO_TOKEN}"}
    content_b64 = base64.b64encode(json.dumps(wl_dict, indent=2).encode()).decode()

    # First get current file SHA (if exists) to avoid conflicts
    resp = requests.get(url, headers=headers)
    sha = resp.json().get("sha") if resp.status_code == 200 else None

    payload = {
        "message": "Update whitelist",
        "content": content_b64,
    }
    if sha:
        payload["sha"] = sha

    resp = requests.put(url, json=payload, headers=headers)
    if resp.status_code in (200, 201):
        logger.info("Whitelist updated in private repo.")
    else:
        logger.error(f"Failed to push whitelist: {resp.status_code} {resp.text}")

# ---------- Whitelist management (background polling) ----------
async def poll_rubika_commands():
    global whitelist
    offset = 0
    logger.info("Started Rubika command listener")
    while True:
        try:
            url = f"{BASE_URL}/getUpdates?offset={offset}&timeout=15"
            r = requests.get(url, timeout=20)
            if r.status_code != 200:
                await asyncio.sleep(5)
                continue
            updates = r.json()
            for upd in updates:
                offset = upd.get("update_id", 0) + 1
                msg = upd.get("message")
                if not msg:
                    continue
                text = msg.get("text", "").strip()
                cid = str(msg["chat"]["id"])

                # Non‑admins: reply with their ID
                if cid not in ADMIN_CHAT_IDS:
                    if text.strip():
                        requests.post(f"{BASE_URL}/sendMessage", json={
                            "chat_id": cid,
                            "text": f"Your chat ID is `{cid}`. Send this to an admin to be whitelisted."
                        })
                    continue

                # ---- Admin commands ----
                if text.startswith("whitelist "):
                    parts = text[len("whitelist "):].strip().split(maxsplit=1)  # at most 2 parts: id, name
                    target = parts[0].lstrip('@')
                    name = parts[1] if len(parts) > 1 else ""
                    whitelist[target] = name
                    push_whitelist_to_repo(whitelist)
                    requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"✅ Whitelisted {target} ({name})"})
                elif text.startswith("remove "):
                    target = text[len("remove "):].strip().lstrip('@')
                    if target in whitelist:
                        del whitelist[target]
                        push_whitelist_to_repo(whitelist)
                        requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"❌ Removed {target}"})
                    else:
                        requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": "Not in whitelist."})
                elif text == "/list":
                    if not whitelist:
                        lst = "(empty)"
                    else:
                        lst = "\n".join(f"{cid} → {name}" for cid, name in whitelist.items())
                    requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"Whitelist:\n{lst}"})
        except Exception as e:
            logger.error(f"Poll error: {e}")
        await asyncio.sleep(1)

# ---------- Other functions (unchanged, but ensure whitelist usage) ----------
def clean_text(text: str) -> str:
    return text.replace('`', '')

def send_text_to_rubika(channel: str, text: str, date: datetime) -> bool:
    text = clean_text(text)
    date_str = date.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"=============\n{channel}\n{date_str}\n=============\n\n{text}"
    url = f"{BASE_URL}/sendMessage"
    ok = True
    for cid in whitelist.keys():
        try:
            r = requests.post(url, json={"chat_id": cid, "text": msg}, timeout=10)
            if r.status_code != 200:
                logger.error(f"❌ Text to {cid}: {r.status_code} {r.text}")
                ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {cid}: {e}")
            ok = False
    return ok

def upload_file(byte_data: bytes, filename: str, file_type: str, chat_id: str) -> str | None:
    url = f"{BASE_URL}/uploadFile"
    try:
        files = {"file": (filename, BytesIO(byte_data))}
        data = {"chat_id": chat_id, "file_name": filename, "file_type": file_type}
        r = requests.post(url, data=data, files=files, timeout=30)
        logger.info(f"uploadFile response [{r.status_code}]: {r.text}")
        if r.status_code == 200:
            j = r.json()
            if j.get("status") == "OK" and "file_id" in j:
                return j["file_id"]
            else:
                logger.error(f"uploadFile returned: {r.text}")
        else:
            logger.error(f"uploadFile HTTP {r.status_code}")
    except Exception as e:
        logger.error(f"uploadFile exception: {e}")
    return None

def send_media_by_id(channel: str, date: datetime, file_id: str, media_type: str, caption: str = "") -> bool:
    caption = clean_text(caption)
    date_str = date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel}\n{date_str}\n============="
    full_cap = f"{header}\n\n{caption}" if caption else header

    method = {"photo":"sendPhoto","video":"sendVideo","audio":"sendAudio","voice":"sendVoice","document":"sendDocument"}.get(media_type, "sendDocument")
    url = f"{BASE_URL}/{method}"
    ok = True
    for cid in whitelist.keys():
        payload = {"chat_id": cid, "file": file_id, "caption": full_cap}
        try:
            r = requests.post(url, json=payload, timeout=10)
            logger.info(f"{method} response [{r.status_code}]: {r.text}")
            if r.status_code == 200:
                j = r.json()
                if j.get("status") == "OK" or j.get("ok"):
                    logger.info(f"✅ {media_type} sent to {cid}")
                else:
                    logger.error(f"❌ Media error to {cid}: {r.text}")
                    ok = False
            else:
                logger.error(f"❌ Media HTTP error to {cid}: {r.status_code}")
                ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {cid}: {e}")
            ok = False
    return ok

def format_reactions(message) -> str:
    if not hasattr(message, 'reactions') or not message.reactions:
        return ""
    results = message.reactions.results
    if not results:
        return ""
    sorted_reacts = sorted(results, key=lambda r: r.count, reverse=True)[:3]
    parts = []
    for r in sorted_reacts:
        emoji = r.reaction.emoticon if hasattr(r.reaction, 'emoticon') else ''
        parts.append(f"{emoji}{r.count}")
    return "  " + "  ".join(parts) if parts else ""

async def forward_message(client, message, channel_name, state, skip_dup=False):
    msg_date = message.date
    if not skip_dup and message.id <= state.get(channel_name, 0):
        return

    reactions_str = format_reactions(message)

    # Text only
    if message.text and not message.media:
        full_text = message.text + reactions_str
        if send_text_to_rubika(channel_name, full_text, msg_date):
            state[channel_name] = message.id
            save_state(state)
        return

    # Media
    if not message.file or not message.file.size:
        return
    file_size = message.file.size
    if file_size > MAX_FILE_SIZE:
        size_mb = file_size/(1024*1024)
        send_text_to_rubika(channel_name, f"⚠️ Large file skipped ({size_mb:.1f} MB)\nOriginal: {message.file.name or 'unknown'}", msg_date)
        state[channel_name] = message.id
        save_state(state)
        return

    if message.photo:
        media_type, filename = "photo", "photo.jpg"
        file_type = "image/jpeg"
    elif message.video:
        media_type, filename = "video", message.file.name or "video.mp4"
        file_type = "video/mp4"
    elif message.audio:
        media_type, filename = "audio", message.file.name or "audio.mp3"
        file_type = "audio/mpeg"
    elif message.voice:
        media_type, filename = "voice", message.file.name or "voice.ogg"
        file_type = "audio/ogg"
    else:
        media_type, filename = "document", message.file.name or "unknown_file"
        file_type = "application/octet-stream"

    caption = (message.text or "") + reactions_str

    try:
        data = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(data)} bytes)")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return

    # Use first whitelisted user for upload (file_id is bound to that chat)
    upload_chat = next(iter(whitelist), None)
    if not upload_chat:
        logger.error("Whitelist empty, cannot upload.")
        return
    file_id = upload_file(data, filename, file_type, upload_chat)
    if not file_id:
        return
    if send_media_by_id(channel_name, msg_date, file_id, media_type, caption):
        state[channel_name] = message.id
        save_state(state)

# ---------- State handling ----------
def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def load_state():
    if STATE_FILE.exists():
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

async def catch_up(client, channels, state):
    if not state:
        logger.info("First run – initializing markers (no old messages forwarded).")
        for ch in channels:
            try:
                msgs = await client.get_messages(ch, limit=1)
                state[ch] = msgs[0].id if msgs else 0
                logger.info(f"Marker {ch}: {state[ch]}")
            except Exception as e:
                logger.error(f"Init {ch}: {e}")
        save_state(state)
        return
    logger.info("Checking missed messages…")
    for ch in channels:
        try:
            msgs = await client.get_messages(ch, limit=10)
            if not msgs: continue
            for m in reversed(msgs):
                if m.id <= state.get(ch, 0): continue
                if not m.text and not m.media: continue
                logger.info(f"Missed {m.id} from {ch}")
                await forward_message(client, m, ch, state)
        except Exception as e:
            logger.error(f"Catchup {ch}: {e}")

async def debug_resend_last3(client, channels, state):
    logger.info("DEBUG: Resending last 3 messages from each channel (first run only).")
    for ch in channels:
        try:
            msgs = await client.get_messages(ch, limit=3)
            for m in reversed(msgs):
                if not m.text and not m.media: continue
                logger.info(f"DEBUG resend {m.id} from {ch}")
                await forward_message(client, m, ch, state, skip_dup=True)
        except Exception as e:
            logger.error(f"DEBUG {ch}: {e}")

# ---------- Main ----------
async def main():
    global whitelist
    if not all([API_ID, API_HASH, STRING_SESSION, DATA_REPO_OWNER, DATA_REPO_NAME, DATA_REPO_TOKEN]):
        logger.error("Missing essential environment variables!")
        sys.exit(1)

    # Load whitelist from private repo
    whitelist = get_whitelist_from_repo()
    logger.info(f"Whitelist loaded: {len(whitelist)} users")

    channels = load_channels()
    logger.info(f"Channels: {channels}")

    client = TelegramClient(StringSession(STRING_SESSION), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram client ready")

    state = load_state()
    first_run = (state == {})
    await catch_up(client, channels, state)
    if first_run:
        await debug_resend_last3(client, channels, state)

    # Start background poller
    poll_task = asyncio.create_task(poll_rubika_commands())

    @client.on(events.NewMessage(chats=channels))
    async def handler(event):
        try:
            chat = await event.get_chat()
            await forward_message(client, event.message, chat.title, state)
        except Exception as e:
            logger.error(f"Handler error: {e}")

    logger.info("Forwarding live…")
    start = time.time()
    try:
        while (time.time() - start) < RUN_DURATION:
            await asyncio.sleep(30)
    finally:
        poll_task.cancel()
        await client.disconnect()
        logger.info("Disconnected cleanly")

if __name__ == "__main__":
    asyncio.run(main())
