import os
import re
import json
import time
import uuid
import logging
import threading
from datetime import datetime
import requests
from flask import Flask, jsonify, request, send_from_directory

BOT_TOKEN        = "8598271310:AAHH30udI-5-uqAgDl6S7aQUwokKNz_NmqA"
BOT_USERNAME     = "@LaborGallerybot"
DATA_FILE        = "data.json"
TELEGRAM_CONFIG_FILE = "telegram_config.json"
DASHBOARD_DIR    = os.path.dirname(os.path.abspath(__file__))
POLLING_TIMEOUT  = 30
VALIDATE_EVERY   = 600

CONTROLLED_BRANDS = {
    "crickex": 7, "mostplay": 64, "darazplay": 50, "kv8": 42,
    "sbj66": 66, "superbaji": 68, "jeetway": 58, "heybaji": 54,
    "betjili": 9, "betvisa": 123, "jeetwin": 60, "betjdb": 48,
    "jeetbangla": 56, "heyvip": 46, "luckyworld": 62, "123ga": 44,"bn88":162
}

TELEGRAM_CONFIG = {
    "sending":   {"group_id": -1003892418830, "topics": CONTROLLED_BRANDS},
    "receiving": {"group_id": -1001234567890, "topics": {"tracker": 1}}
}

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm", ".flv", ".m4v"}
AUDIO_EXTS = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}
DOC_EXTS   = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt", ".zip", ".rar"}

TELEGRAM_KEY_TO_TYPE = {
    "photo": "image", "video": "video", "animation": "video",
    "audio": "audio", "voice": "audio", "document": "document",
    "sticker": "image", "video_note": "video",
}

def detect_file_type(file_name):
    ext = os.path.splitext(file_name.lower())[1]
    if ext in IMAGE_EXTS: return "image"
    if ext in VIDEO_EXTS: return "video"
    if ext in AUDIO_EXTS: return "audio"
    if ext in DOC_EXTS:   return "document"
    return "other"

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.ERROR)

offset = 0
pending_uploads = {}
_last_validated = 0
_tg_config_cache = None
_tg_config_mtime = 0

def load_data():
    if not os.path.exists(DATA_FILE):
        _save_raw({"entries": []})
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Load data error: {e}")
        return {"entries": []}

def _save_raw(data):
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Save data error: {e}")

def save_entry(brand, promo, link, version, username,
               is_image=False, file_type="other", file_id=None, preview_url=None):
    data = load_data()
    entry = {
        "id": str(uuid.uuid4())[:8], "brand": brand, "promo": promo,
        "version": version, "status": "", "link": link,
        "is_image": is_image, "file_type": file_type,
        "file_id": file_id or "", "preview_url": preview_url or "",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "user": username,
    }
    data["entries"].append(entry)
    _save_raw(data)
    logger.info(f"Saved: {brand} | {promo} v{version} [{file_type}]")
    return entry

def check_duplicate(brand, promo):
    data = load_data()
    for e in data["entries"]:
        if e["brand"] == brand and e["promo"] == promo and e.get("status") not in ("replaced", "deleted"):
            return True
    return False

def get_current_version(brand, promo):
    data = load_data()
    max_v = 0
    for e in data["entries"]:
        if e["brand"] == brand and e["promo"] == promo:
            try: max_v = max(max_v, int(e.get("version", 1)))
            except (ValueError, TypeError): pass
    return max_v

def mark_replaced(brand, promo):
    data = load_data()
    for e in data["entries"]:
        if e["brand"] == brand and e["promo"] == promo and e.get("status") not in ("replaced", "deleted"):
            e["status"] = "replaced"
    _save_raw(data)

def mark_deleted(entry_id):
    data = load_data()
    for e in data["entries"]:
        if e["id"] == entry_id:
            e["status"] = "deleted"
            break
    _save_raw(data)

def get_versions(brand, promo, limit=5):
    data = load_data()
    matches = [e for e in data["entries"] if e["brand"] == brand and e["promo"] == promo]
    matches.sort(key=lambda e: e.get("version", 1))
    return matches[-limit:]

def _default_tg_config():
    return {
        "sending_groups": [{
            "id": "default", "name": "Main Archive",
            "group_id": TELEGRAM_CONFIG["sending"]["group_id"],
            "topics": TELEGRAM_CONFIG["sending"]["topics"], "active": True,
        }],
        "receiving_groups": [{
            "id": "tracker", "name": "Tracker",
            "group_id": TELEGRAM_CONFIG["receiving"]["group_id"],
            "topic_id": TELEGRAM_CONFIG["receiving"]["topics"].get("tracker"), "active": True,
        }],
    }

def load_tg_config():
    global _tg_config_cache, _tg_config_mtime
    if os.path.exists(TELEGRAM_CONFIG_FILE):
        try:
            mtime = os.path.getmtime(TELEGRAM_CONFIG_FILE)
            if mtime != _tg_config_mtime or _tg_config_cache is None:
                with open(TELEGRAM_CONFIG_FILE, "r", encoding="utf-8") as f:
                    _tg_config_cache = json.load(f)
                _tg_config_mtime = mtime
            return _tg_config_cache
        except Exception as e:
            logger.error(f"Telegram config load error: {e}")
    cfg = _default_tg_config()
    save_tg_config(cfg)
    _tg_config_cache = cfg
    return cfg

def save_tg_config(config):
    global _tg_config_cache, _tg_config_mtime
    try:
        with open(TELEGRAM_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        _tg_config_cache = config
        _tg_config_mtime = os.path.getmtime(TELEGRAM_CONFIG_FILE)
        logger.info("Telegram config saved.")
    except Exception as e:
        logger.error(f"Telegram config save error: {e}")

def get_active_sending_target(brand):
    cfg = load_tg_config()
    for sg in cfg.get("sending_groups", []):
        if not sg.get("active", True): continue
        topic_id = sg.get("topics", {}).get(brand)
        if topic_id is not None:   # PATCH: was: if topic_id:
            return sg["group_id"], topic_id
    topic_id = TELEGRAM_CONFIG["sending"]["topics"].get(brand)
    return TELEGRAM_CONFIG["sending"]["group_id"], topic_id

def get_active_receiving_group():
    cfg = load_tg_config()
    for rg in cfg.get("receiving_groups", []):
        if not rg.get("active", True): continue
        return rg["group_id"], rg.get("topic_id")
    return TELEGRAM_CONFIG["receiving"]["group_id"], None

def _api(method, payload):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json()
    except Exception as e:
        logger.error(f"API {method} error: {e}")
        return {}

def get_file_url(file_id):
    if not file_id: return None
    result = _api("getFile", {"file_id": file_id})
    if result.get("ok"):
        fp = result["result"].get("file_path", "")
        if fp: return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{fp}"
    return None

def send_message(chat_id, text, reply_markup=None, topic_id=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    if topic_id is not None: payload["message_thread_id"] = topic_id
    result = _api("sendMessage", payload)
    if result.get("ok"): return result["result"].get("message_id")
    return None

def edit_message(chat_id, message_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML"}
    if reply_markup: payload["reply_markup"] = reply_markup
    return _api("editMessageText", payload).get("ok", False)

def answer_callback(callback_id, text=""):
    _api("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

def copy_message(from_chat_id, message_id, to_chat_id, topic_id=None):
    payload = {"from_chat_id": from_chat_id, "message_id": message_id, "chat_id": to_chat_id}
    if topic_id is not None: payload["message_thread_id"] = topic_id
    result = _api("copyMessage", payload)
    if result.get("ok"): return result["result"].get("message_id")
    return None

def get_username(user_id):
    result = _api("getChat", {"chat_id": user_id})
    if result.get("ok"): return result["result"].get("username") or "user"
    return "user"

def validate_entries():
    global _last_validated
    now = time.time()
    if now - _last_validated < VALIDATE_EVERY: return
    _last_validated = now
    data = load_data()
    changed = False
    for e in data["entries"]:
        if e.get("status") in ("replaced", "deleted"): continue
        fid = e.get("file_id")
        if not fid: continue
        result = _api("getFile", {"file_id": fid})
        if not result.get("ok"):
            e["status"] = "deleted"
            changed = True
            logger.info(f"Auto-deleted stale entry: {e['brand']} | {e['promo']} v{e['version']}")
    if changed: _save_raw(data)

def fmt_tracker(brand, promo, version, username, is_image=False, action_label="SAVED"):
    version_tag  = f"  v{version}" if version > 1 else ""
    preview_line = "\nPreview available" if is_image else ""
    return (f"<b>{action_label}</b>\n\n<b>{brand.upper()}</b>  |  {promo}{version_tag}\n"
            f"<a href='https://t.me'>View in archive</a>{preview_line}\n\n@{username}")

def fmt_duplicate(brand, promo):
    versions = get_versions(brand, promo)
    history = ""
    if versions:
        lines = [f"  v{e['version']}  {e['timestamp'][:10]}  @{e['user']}" for e in versions]
        history = "\n<code>" + "\n".join(lines) + "</code>"
    return f"<b>DUPLICATE</b>\n\n<b>{brand.upper()}</b>  |  {promo}{history}\n\nChoose action below."

def duplicate_buttons(short_id):
    return {"inline_keyboard": [
        [{"text": "Replace", "callback_data": f"r|{short_id}"},
         {"text": "Save New", "callback_data": f"n|{short_id}"}],
        [{"text": "Cancel", "callback_data": f"c|{short_id}"}],
    ]}

def parse_caption(text):
    if not text: return None, None
    text = re.sub(re.escape(BOT_USERNAME), "", text, flags=re.IGNORECASE).strip()
    parts = re.split(r"\s*\|\s*", text, maxsplit=1)
    if len(parts) < 2: return None, None
    return parts[0].lower().strip(), parts[1].strip()

def build_archive_link(group_id, message_id):
    return f"https://t.me/c/{str(group_id).replace('-100', '')}/{message_id}"

def _is_triggered(message):
    text = (message.get("text") or message.get("caption") or "").lower()
    return BOT_USERNAME.lower() in text and "reply_to_message" in message

def process_message(message, from_user_id):
    if not _is_triggered(message):
        return

    import random
    import time

    def send_typing(chat_id):
        _api("sendChatAction", {
            "chat_id": chat_id,
            "action": "typing"
        })

    # =========================
    # CONTEXT
    # =========================
    chat_id = message.get("chat", {}).get("id")
    topic_id = message.get("message_thread_id")
    original = message.get("reply_to_message", {})
    original_msg_id = original.get("message_id")

    # =========================
    # 🔥 PREMIUM UX START
    # =========================

    intros = [
        "👀 Checking boy",
        "🧠 Analyzing request",
        "📂 Opening file",
        "🚀 Initiating process",
        "🔍 Running scan"
    ]

    phases = [
        ["Checking", "Validating", "Finalizing"],
        ["Reading", "Processing", "Completing"],
        ["Scanning", "Analyzing", "Saving"],
        ["Inspecting", "Working", "Finishing"]
    ]

    success_msgs = [
        "✅ Done",
        "🎯 Completed",
        "🚀 Success",
        "🔥 All good"
    ]

    intro = random.choice(intros)
    flow = random.choice(phases)
    done = random.choice(success_msgs)

    def dots(text, n):
        return text + "." * n

    # Step 1
    send_typing(chat_id)
    time.sleep(0.5)

    msg_id = send_message(chat_id, intro + "...", topic_id=topic_id)

    # Step 2
    for i in range(1, 3):
        send_typing(chat_id)
        time.sleep(0.4)
        edit_message(chat_id, msg_id, intro + "...\n\n1️⃣ " + dots(flow[0], i))

    # Step 3
    for i in range(1, 3):
        send_typing(chat_id)
        time.sleep(0.4)
        edit_message(chat_id, msg_id,
            intro + "...\n\n1️⃣ " + flow[0] + "...\n2️⃣ " + dots(flow[1], i)
        )

    # Step 4
    for i in range(1, 2):
        send_typing(chat_id)
        time.sleep(0.4)
        edit_message(chat_id, msg_id,
            intro + "...\n\n1️⃣ " + flow[0] + "...\n2️⃣ " + flow[1] + "...\n3️⃣ " + dots(flow[2], i)
        )

    # Final
    send_typing(chat_id)
    time.sleep(0.4)

    edit_message(chat_id, msg_id,
        intro + "...\n\n"
        "1️⃣ " + flow[0] + "...\n"
        "2️⃣ " + flow[1] + "...\n"
        "3️⃣ " + flow[2] + "...\n\n"
        + done + " 🚀"
    )

    # =========================
    # FILE DETECTION
    # =========================
    file_obj = None
    file_name = "file"
    is_image = False
    file_type = "other"
    file_id = ""

    if "document" in original:
        doc = original["document"]
        file_name = doc.get("file_name", "file")
        file_type = detect_file_type(file_name)
        if file_type == "other":
            file_type = "document"
        is_image = file_type == "image"
        file_obj = doc
        file_id = doc.get("file_id", "")

    elif "photo" in original:
        is_image = True
        file_type = "image"
        file_obj = original["photo"][-1]
        file_name = "photo.jpg"
        file_id = file_obj.get("file_id", "")

    else:
        for key in ("video", "animation", "video_note", "audio", "voice", "sticker"):
            if key in original:
                file_obj = original[key]
                file_name = file_obj.get("file_name", f"{key}.file")
                file_id = file_obj.get("file_id", "")
                detected = detect_file_type(file_name)
                file_type = detected if detected != "other" else TELEGRAM_KEY_TO_TYPE.get(key, "other")
                is_image = file_type == "image"
                break

    # =========================
    # VALIDATION
    # =========================
    if not file_obj:
        send_message(chat_id, "<b>No file found</b>\n\nReply to a message that contains a file.", topic_id=topic_id)
        return

    trigger_text = (message.get("text") or message.get("caption") or "").strip()
    brand, promo = parse_caption(trigger_text)

    if not brand or not promo:
        send_message(
            chat_id,
            f"<b>Missing caption</b>\n\nReply format:  <code>{BOT_USERNAME} brand | promo name</code>",
            topic_id=topic_id
        )
        return

    archive_group_id, target_topic_id = get_active_sending_target(brand)

    if target_topic_id is None:
        send_message(
            chat_id,
            f"<b>Unknown brand</b>  —  <code>{brand}</code>\n\nSupported: {', '.join(sorted(CONTROLLED_BRANDS.keys()))}",
            topic_id=topic_id
        )
        return

    orig_chat_id = chat_id
    orig_msg_id = original_msg_id

    # =========================
    # DUPLICATE CHECK
    # =========================
    if check_duplicate(brand, promo):
        short_id = str(uuid.uuid4())[:6]

        pending_uploads[short_id] = {
            "original_chat_id": orig_chat_id,
            "original_msg_id": orig_msg_id,
            "archive_group_id": archive_group_id,
            "topic_id": target_topic_id,
            "from_user_id": from_user_id,
            "brand": brand,
            "promo": promo,
            "is_image": is_image,
            "file_type": file_type,
            "file_id": file_id,
        }

        send_message(
            chat_id,
            fmt_duplicate(brand, promo),
            duplicate_buttons(short_id),
            topic_id=topic_id
        )

        return

    # =========================
    # FORWARD
    # =========================
    _forward(
        orig_chat_id,
        orig_msg_id,
        brand,
        promo,
        archive_group_id,
        target_topic_id,
        from_user_id,
        version=1,
        is_image=is_image,
        file_type=file_type,
        file_id=file_id,
        action_label="SAVED"
    )
def _forward(original_chat_id, original_msg_id, brand, promo,
             archive_group_id, topic_id, from_user_id,
             version, is_image=False, file_type="other", file_id="", action_label="SAVED"):
    username = get_username(from_user_id)
    archive_msg_id = copy_message(original_chat_id, original_msg_id, archive_group_id, topic_id)
    if not archive_msg_id:
        send_message(original_chat_id, "Failed to save to archive. Please try again.", topic_id=topic_id)
        return
    link = build_archive_link(archive_group_id, archive_msg_id)
    preview_url = get_file_url(file_id) if file_id else None
    recv_group_id, recv_topic_id = get_active_receiving_group()
    send_message(recv_group_id, fmt_tracker(brand, promo, version, username, is_image, action_label), topic_id=recv_topic_id)
    save_entry(brand, promo, link, version, username, is_image=is_image, file_type=file_type, file_id=file_id, preview_url=preview_url)
    logger.info(f"Done: {brand} | {promo} v{version} [{action_label}] [{file_type}]")

def process_callback(callback_data, from_user_id, callback_id, cb_message):
    parts = callback_data.split("|")
    if len(parts) < 2: answer_callback(callback_id); return
    action, short_id = parts[0], parts[1]
    upload = pending_uploads.get(short_id)
    if not upload: answer_callback(callback_id, "Session expired — please re-upload."); return

    brand = upload["brand"]; promo = upload["promo"]
    archive_group_id = upload.get("archive_group_id", TELEGRAM_CONFIG["sending"]["group_id"])
    topic_id = upload["topic_id"]; is_image = upload.get("is_image", False)
    file_type = upload.get("file_type", "other"); file_id = upload.get("file_id", "")
    orig_chat_id = upload["original_chat_id"]; orig_msg_id = upload["original_msg_id"]
    chat_id = cb_message.get("chat", {}).get("id"); msg_id = cb_message.get("message_id")

    if action == "r":
        mark_replaced(brand, promo)
        new_v = get_current_version(brand, promo) + 1
        _forward(orig_chat_id, orig_msg_id, brand, promo, archive_group_id, topic_id,
                 from_user_id, version=new_v, is_image=is_image, file_type=file_type, file_id=file_id, action_label="REPLACED")
        edit_message(chat_id, msg_id, f"<b>Replaced</b>  —  {brand.upper()} | {promo}  v{new_v}")
        logger.info(f"Replaced: {brand} | {promo} → v{new_v}")
    elif action == "n":
        new_v = get_current_version(brand, promo) + 1
        _forward(orig_chat_id, orig_msg_id, brand, promo, archive_group_id, topic_id,
                 from_user_id, version=new_v, is_image=is_image, file_type=file_type, file_id=file_id, action_label="SAVED")
        edit_message(chat_id, msg_id, f"<b>Saved</b>  —  {brand.upper()} | {promo}  v{new_v}")
        logger.info(f"New version: {brand} | {promo} → v{new_v}")
    elif action == "c":
        edit_message(chat_id, msg_id, "<b>Cancelled</b>")
        logger.info(f"Cancelled: {brand} | {promo}")

    pending_uploads.pop(short_id, None)
    answer_callback(callback_id)

app = Flask(__name__, static_folder=DASHBOARD_DIR)

@app.route("/")
def index():
    key = request.args.get("key")
    if key != "98419841":
        return "Forbidden", 403
    return send_from_directory(DASHBOARD_DIR, "BannerTracker.html")

@app.route("/data")
def api_data():
    return jsonify(load_data())

@app.route("/api/entries/<entry_id>", methods=["DELETE"])
def api_delete_entry(entry_id):
    mark_deleted(entry_id)
    logger.info(f"Dashboard deleted entry: {entry_id}")
    return jsonify({"ok": True})

@app.route("/api/telegram-config", methods=["GET"])
def api_get_tg_config():
    return jsonify(load_tg_config())

@app.route("/api/telegram-config", methods=["POST"])
def api_save_tg_config():
    data = request.get_json(force=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Request body must be a JSON object"}), 400
    if not isinstance(data.get("sending_groups"), list):
        return jsonify({"ok": False, "error": "sending_groups must be a list"}), 400
    if not isinstance(data.get("receiving_groups"), list):
        return jsonify({"ok": False, "error": "receiving_groups must be a list"}), 400
    for j, sg in enumerate(data["sending_groups"]):
        if not isinstance(sg, dict):
            return jsonify({"ok": False, "error": f"sending_groups[{j}] must be an object"}), 400
        if not sg.get("id"):
            return jsonify({"ok": False, "error": f"sending_groups[{j}].id is required"}), 400
        if not isinstance(sg.get("name"), str) or not sg["name"].strip():
            return jsonify({"ok": False, "error": f"sending_groups[{j}].name must be a non-empty string"}), 400
        if not isinstance(sg.get("group_id"), int):
            return jsonify({"ok": False, "error": f"sending_groups[{j}].group_id must be an integer"}), 400
        if "active" in sg and not isinstance(sg["active"], bool):
            return jsonify({"ok": False, "error": f"sending_groups[{j}].active must be boolean"}), 400
        topics = sg.get("topics", {})
        if not isinstance(topics, dict):
            return jsonify({"ok": False, "error": f"sending_groups[{j}].topics must be an object"}), 400
        for brand, tid in topics.items():
            if not isinstance(tid, int):
                return jsonify({"ok": False, "error": f"sending_groups[{j}].topics.{brand} must be an integer"}), 400
    for j, rg in enumerate(data["receiving_groups"]):
        if not isinstance(rg, dict):
            return jsonify({"ok": False, "error": f"receiving_groups[{j}] must be an object"}), 400
        if not rg.get("id"):
            return jsonify({"ok": False, "error": f"receiving_groups[{j}].id is required"}), 400
        if not isinstance(rg.get("name"), str) or not rg["name"].strip():
            return jsonify({"ok": False, "error": f"receiving_groups[{j}].name must be a non-empty string"}), 400
        if not isinstance(rg.get("group_id"), int):
            return jsonify({"ok": False, "error": f"receiving_groups[{j}].group_id must be an integer"}), 400
        if "active" in rg and not isinstance(rg["active"], bool):
            return jsonify({"ok": False, "error": f"receiving_groups[{j}].active must be boolean"}), 400
        tid = rg.get("topic_id")
        if tid is not None and not isinstance(tid, int):
            return jsonify({"ok": False, "error": f"receiving_groups[{j}].topic_id must be an integer or null"}), 400
    save_tg_config(data)
    return jsonify({"ok": True})

@app.route("/webhook/<secret>", methods=["POST"])
def webhook(secret):
    if secret != "x9KpppLmQ7z":
        return "Forbidden", 403

    data = request.get_json()

    if "message" in data:
        msg = data["message"]
        from_id = msg.get("from", {}).get("id")
        if from_id:
            process_message(msg, from_id)

    if "callback_query" in data:
        cb = data["callback_query"]
        from_id = cb.get("from", {}).get("id")
        if from_id:
            process_callback(cb.get("data", ""), from_id, cb.get("id"), cb.get("message", {}))

    return "OK"
    
@app.route("/api/telegram-config/test", methods=["POST"])
def api_test_tg_send():
    data     = request.get_json(force=True)
    group_id = data.get("group_id")
    topic_id = data.get("topic_id")
    if group_id is None:
        return jsonify({"ok": False, "error": "Missing group_id"}), 400
    if not isinstance(group_id, int):
        return jsonify({"ok": False, "error": "group_id must be an integer"}), 400
    if topic_id is not None and not isinstance(topic_id, int):
        return jsonify({"ok": False, "error": "topic_id must be an integer or null"}), 400
    payload = {"chat_id": group_id, "text": "🔔 <b>Test</b> — Banner Tracker Dashboard connection check.", "parse_mode": "HTML"}
    if topic_id is not None: payload["message_thread_id"] = topic_id
    result = _api("sendMessage", payload)
    if result.get("ok"): return jsonify({"ok": True})
    return jsonify({"ok": False, "error": result.get("description", "Unknown Telegram error")})

def get_local_ip():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)); ip = s.getsockname()[0]; s.close(); return ip
    except Exception: return "127.0.0.1"

import os

PORT = int(os.environ.get("PORT", 5050))

def run_flask():
    app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False)

def get_updates():
    global offset
    return _api("getUpdates", {"offset": offset, "timeout": POLLING_TIMEOUT})



if __name__ == "__main__":
    run_flask()
