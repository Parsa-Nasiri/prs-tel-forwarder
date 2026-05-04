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

ADMIN_CHAT_IDS = [cid.strip() for cid in os.environ.get("ADMIN_CHAT_IDS", "").split(",") if cid.strip()]

DATA_REPO_OWNER = os.environ["DATA_REPO_OWNER"]
DATA_REPO_NAME = os.environ["DATA_REPO_NAME"]
DATA_REPO_TOKEN = os.environ["DATA_REPO_TOKEN"]

BASE_URL = f"https://botapi.rubika.ir/v3/{RUBIKA_BOT_TOKEN}"
CHANNELS_FILE = Path("channels.json")
RUN_DURATION = 20900          # 5h 48m
MAX_FILE_SIZE = 50 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", stream=sys.stdout)
logger = logging.getLogger(__name__)

# ---------- Private repo helpers ----------
def github_request(method, path, json_data=None, extra_headers=None):
    url = f"https://api.github.com/repos/{DATA_REPO_OWNER}/{DATA_REPO_NAME}/contents/{path}"
    headers = {"Authorization": f"token {DATA_REPO_TOKEN}"}
    if extra_headers:
        headers.update(extra_headers)
    resp = requests.request(method, url, headers=headers, json=json_data)
    return resp

def get_file_from_repo(path):
    resp = github_request("GET", path)
    if resp.status_code == 200:
        content = resp.json().get("content", "")
        if content:
            return base64.b64decode(content).decode('utf-8')
    return ""

def push_file_to_repo(path, content_str):
    resp = github_request("GET", path)
    sha = resp.json().get("sha") if resp.status_code == 200 else None
    payload = {
        "message": f"Update {path}",
        "content": base64.b64encode(content_str.encode()).decode(),
    }
    if sha:
        payload["sha"] = sha
    resp = github_request("PUT", path, json_data=payload)
    if resp.status_code in (200, 201):
        logger.info(f"✅ {path} updated in private repo.")
    else:
        logger.error(f"❌ Failed to push {path}: {resp.status_code} {resp.text}")

# ---------- Whitelist & state ----------
def load_whitelist():
    raw = get_file_from_repo("whitelist.json")
    try:
        wl = json.loads(raw) if raw else {}
    except:
        wl = {}
    if not wl and ADMIN_CHAT_IDS:
        # Auto-populate with admins on first run
        for cid in ADMIN_CHAT_IDS:
            wl[cid] = "Admin"
        push_file_to_repo("whitelist.json", json.dumps(wl, indent=2))
        logger.info(f"Initialised whitelist with admins: {list(wl.keys())}")
    return wl

def load_state():
    raw = get_file_from_repo("state.json")
    try:
        return json.loads(raw) if raw else {}
    except:
        return {}

def save_state(state):
    push_file_to_repo("state.json", json.dumps(state, indent=2))

whitelist = load_whitelist()
logger.info(f"Whitelist loaded: {len(whitelist)} users")

# ---------- Rubika API ----------
def clean_text(text: str) -> str:
    return text.replace('`', '')

def send_text_to_rubika(channel: str, text: str, date: datetime) -> bool:
    text = clean_text(text)
    date_str = date.strftime("%Y-%m-%d %H:%M:%S")
    msg = f"=============\n{channel}\n{date_str}\n=============\n\n{text}"
    url = f"{BASE_URL}/sendMessage"
    ok = True
    for cid in whitelist:
        try:
            r = requests.post(url, json={"chat_id": cid, "text": msg}, timeout=10)
            if r.status_code != 200:
                logger.error(f"❌ Text to {cid}: {r.status_code} {r.text}")
                ok = False
        except Exception as e:
            logger.error(f"❌ Network error to {cid}: {e}")
            ok = False
    return ok

def upload_file_to_rubika(file_bytes: bytes, file_name: str, file_type: str) -> str | None:
    """
    Uploads a file to Rubika using the two-step process:
    1. requestSendFile to get an upload URL
    2. Upload the file to that URL
    Returns the file_id to use with sendFile
    """
    # Step 1: requestSendFile
    file_type_map = {
        "photo": "Image",
        "video": "Video",
        "audio": "Music",
        "voice": "Voice",
        "document": "File"
    }
    rubika_type = file_type_map.get(file_type, "File")
    
    try:
        # Request upload URL
        req_url = f"{BASE_URL}/requestSendFile"
        resp = requests.post(req_url, json={"type": rubika_type})
        logger.info(f"requestSendFile response [{resp.status_code}]: {resp.text}")
        if resp.status_code != 200:
            logger.error(f"requestSendFile failed: {resp.status_code} {resp.text}")
            return None
        
        data = resp.json()
        upload_url = data.get("upload_url")
        if not upload_url:
            logger.error(f"No upload_url in response: {resp.text}")
            return None
        
        # Step 2: Upload file to the provided URL
        files = {"file": (file_name, BytesIO(file_bytes))}
        upload_resp = requests.post(upload_url, files=files)
        logger.info(f"File upload response [{upload_resp.status_code}]: {upload_resp.text}")
        if upload_resp.status_code != 200:
            logger.error(f"File upload failed: {upload_resp.status_code} {upload_resp.text}")
            return None
        
        upload_data = upload_resp.json()
        file_id = upload_data.get("file_id") or upload_data.get("hash")
        if not file_id:
            logger.error(f"No file_id in upload response: {upload_resp.text}")
            return None
        
        return file_id
    except Exception as e:
        logger.error(f"upload_file_to_rubika exception: {e}")
        return None

def send_file_to_rubika(channel: str, date: datetime, file_id: str, file_type: str, file_name: str, file_size: int, caption: str = "") -> bool:
    """
    Sends a file to Rubika using the sendFile method
    """
    caption = clean_text(caption)
    date_str = date.strftime("%Y-%m-%d %H:%M:%S")
    header = f"=============\n{channel}\n{date_str}\n============="
    full_cap = f"{header}\n\n{caption}" if caption else header

    file_type_map = {
        "photo": "Image",
        "video": "Video",
        "audio": "Music",
        "voice": "Voice",
        "document": "File"
    }
    rubika_type = file_type_map.get(file_type, "File")

    url = f"{BASE_URL}/sendFile"
    ok = True
    for cid in whitelist:
        payload = {
            "chat_id": cid,
            "file_id": file_id,
            "type": rubika_type,
            "caption": full_cap,
            "file_name": file_name,
            "size": str(file_size)
        }
        try:
            r = requests.post(url, json=payload, timeout=10)
            logger.info(f"sendFile response [{r.status_code}]: {r.text}")
            if r.status_code == 200:
                j = r.json()
                if j.get("status") == "OK" or j.get("ok"):
                    logger.info(f"✅ File sent to {cid}")
                else:
                    logger.error(f"❌ File send error to {cid}: {r.text}")
                    ok = False
            else:
                logger.error(f"❌ File send HTTP error to {cid}: {r.status_code}")
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

# ---------- Forwarding ----------
async def forward_message(client, message, channel_name, state, skip_dup=False):
    msg_date = message.date
    if not skip_dup and message.id <= state.get(channel_name, 0):
        return

    reactions_str = format_reactions(message)

    if message.text and not message.media:
        full_text = message.text + reactions_str
        if send_text_to_rubika(channel_name, full_text, msg_date):
            state[channel_name] = message.id
            save_state(state)
        return

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
    elif message.video:
        media_type, filename = "video", message.file.name or "video.mp4"
    elif message.audio:
        media_type, filename = "audio", message.file.name or "audio.mp3"
    elif message.voice:
        media_type, filename = "voice", message.file.name or "voice.ogg"
    else:
        media_type, filename = "document", message.file.name or "unknown_file"

    caption = (message.text or "") + reactions_str

    try:
        data = await client.download_media(message, file=bytes)
        logger.info(f"Downloaded {media_type} ({len(data)} bytes)")
    except Exception as e:
        logger.error(f"Download failed: {e}")
        return

    file_id = upload_file_to_rubika(data, filename, media_type)
    if not file_id:
        logger.error("Failed to upload file to Rubika")
        return

    if send_file_to_rubika(channel_name, msg_date, file_id, media_type, filename, len(data), caption):
        state[channel_name] = message.id
        save_state(state)

# ---------- Poller ----------
async def poll_rubika_commands():
    global whitelist
    offset_id = ""
    logger.info("Started Rubika command listener")
    while True:
        try:
            # Use POST with offset_id and limit according to the documentation
            payload = {"limit": 10}
            if offset_id:
                payload["offset_id"] = offset_id
            
            r = requests.post(f"{BASE_URL}/getUpdates", json=payload, timeout=20)
            if r.status_code != 200:
                logger.error(f"getUpdates HTTP error: {r.status_code}")
                await asyncio.sleep(5)
                continue
            
            data = r.json()
            updates = data.get("updates", [])
            next_offset_id = data.get("next_offset_id", "")
            
            for upd in updates:
                offset_id = next_offset_id
                msg = upd.get("message")
                if not msg:
                    continue
                text = msg.get("text", "").strip()
                cid = str(msg["chat"]["id"])

                # Non-admin: tell them their chat ID
                if cid not in ADMIN_CHAT_IDS:
                    if text:
                        requests.post(f"{BASE_URL}/sendMessage", json={
                            "chat_id": cid,
                            "text": f"Your chat ID is `{cid}`. Send this to an admin to be whitelisted."
                        })
                    continue

                # Admin commands
                if text.startswith("whitelist "):
                    parts = text[len("whitelist "):].strip().split(maxsplit=1)
                    target = parts[0].lstrip('@')
                    name = parts[1] if len(parts) > 1 else ""
                    whitelist[target] = name
                    push_file_to_repo("whitelist.json", json.dumps(whitelist, indent=2))
                    requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"✅ Whitelisted {target} ({name})"})
                elif text.startswith("remove "):
                    target = text[len("remove "):].strip().lstrip('@')
                    if target in whitelist:
                        del whitelist[target]
                        push_file_to_repo("whitelist.json", json.dumps(whitelist, indent=2))
                        requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"❌ Removed {target}"})
                    else:
                        requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": "Not in whitelist."})
                elif text == "/list":
                    lst = "\n".join(f"{cid} → {name}" for cid, name in whitelist.items()) if whitelist else "(empty)"
                    requests.post(f"{BASE_URL}/sendMessage", json={"chat_id": cid, "text": f"Whitelist:\n{lst}"})
        except Exception as e:
            logger.error(f"Poll error: {e}")
        await asyncio.sleep(1)

# ---------- Channel loading ----------
def load_channels():
    with open(CHANNELS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

# ---------- Startup catch‑up & debug ----------
async def catch_up(client, channels, state):
    if not state:
        logger.info("First run – initialising markers (no old messages forwarded).")
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
            if not msgs:
                continue
            for m in reversed(msgs):
                if m.id <= state.get(ch, 0):
                    continue
                if not m.text and not m.media:
                    continue
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
                if not m.text and not m.media:
                    continue
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
        save_state(state)
        logger.info("Disconnected cleanly")

if __name__ == "__main__":
    asyncio.run(main())
