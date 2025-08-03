import os
import time
import tempfile
import threading
import requests
import json
import pymongo
from flask import Flask, request
from pyrogram import Client, filters
import pyromod.listen
from pyrogram.types import Message
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from google.oauth2.credentials import Credentials
from dotenv import load_dotenv

# === Load ENV ===
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
BASE_URL = os.getenv("BASE_URL")  # e.g., https://drive-uploader-bot.onrender.com

# === MongoDB ===
mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client['gdrive_bot']
tokens_collection = db['tokens']

# === Google Scopes ===
SCOPES = ['https://www.googleapis.com/auth/drive']

# === Flask App ===
app = Flask(__name__)

@app.route("/")
def index():
    return "Drive Uploader Bot is running!"

@app.route("/oauth2callback")
def oauth2callback():
    state = request.args.get("state")
    code = request.args.get("code")
    if not state or not code:
        return "Missing state or code", 400
    user_id = int(state)
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{BASE_URL}/oauth2callback"]
            }
        },
        SCOPES
    )
    flow.redirect_uri = f"{BASE_URL}/oauth2callback"
    flow.fetch_token(code=code)
    creds = flow.credentials
    tokens_collection.update_one(
        {"user_id": user_id},
        {"$set": {"token": creds.to_json()}},
        upsert=True
    )
    bot.send_message(user_id, "✅ **Google Drive linked successfully!**\nYou can now use /driveit.")
    return "Google Drive linked successfully! You can close this tab."

# === Pyrogram Bot ===
bot = Client("gdrive_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
upload_count = {}

# === Helpers ===
def get_user_creds(user_id):
    token_data = tokens_collection.find_one({"user_id": user_id})
    if not token_data:
        return None
    return Credentials.from_authorized_user_info(json.loads(token_data['token']))

def build_drive_service(creds):
    return build('drive', 'v3', credentials=creds)

def human_readable(size):
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

# === Commands ===
@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    await message.reply_text(
        "👋 **Welcome to Google Drive Uploader Bot!**\n\n"
        "Commands:\n"
        "/login - Connect your Google Drive\n"
        "/logout - Disconnect your Google Drive\n"
        "/driveit - Upload files or links\n"
        "/storage - View storage info"
    )

@bot.on_message(filters.command("login") & filters.private)
async def login(_, message: Message):
    flow = Flow.from_client_config(
        {
            "web": {
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [f"{BASE_URL}/oauth2callback"]
            }
        },
        SCOPES
    )
    flow.redirect_uri = f"{BASE_URL}/oauth2callback"
    auth_url, _ = flow.authorization_url(prompt='consent', access_type='offline', state=str(message.from_user.id))
    await message.reply_text(
        f"🔗 **Login to Google Drive**\n[Click here to log in]({auth_url})",
        disable_web_page_preview=True
    )

@bot.on_message(filters.command("logout") & filters.private)
async def logout(_, message: Message):
    tokens_collection.delete_one({"user_id": message.from_user.id})
    await message.reply_text("✅ **Logged out successfully!**")

@bot.on_message(filters.command("storage") & filters.private)
async def storage(_, message: Message):
    creds = get_user_creds(message.from_user.id)
    if not creds:
        return await message.reply_text("❌ **You are not logged in. Use /login first.**")

    drive_service = build_drive_service(creds)
    about = drive_service.about().get(fields="storageQuota").execute()
    total = int(about['storageQuota']['limit'])
    used = int(about['storageQuota']['usage'])
    free = total - used
    count = upload_count.get(message.from_user.id, 0)

    await message.reply_text(
        f"📊 **Google Drive Storage:**\n"
        f"Total: `{human_readable(total)}`\n"
        f"Used: `{human_readable(used)}`\n"
        f"Free: `{human_readable(free)}`\n"
        f"Files Uploaded: `{count}`"
    )

@bot.on_message(filters.command("driveit") & filters.private)
async def ask_file(_, message: Message):
    await message.reply_text("📤 **Send me a file or a direct download link to upload.**")

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo | filters.text))
async def handle_upload(_, message: Message):
    creds = get_user_creds(message.from_user.id)
    if not creds:
        return await message.reply_text("❌ **You are not logged in. Use /login first.**")

    if message.text and not message.text.startswith("http") and not message.text.startswith("/"):
        return

    status = await message.reply_text("⏳ **Downloading...**")
    start_time = time.time()

    if message.document or message.video or message.audio or message.photo:
        temp = await message.download()
        orig_filename = message.document.file_name if message.document else "file"
    else:
        url = message.text.strip()
        local_file = tempfile.NamedTemporaryFile(delete=False)
        orig_filename = url.split("/")[-1] or "downloaded_file"
        with requests.get(url, stream=True) as r:
            total = int(r.headers.get('content-length', 0))
            downloaded = 0
            with open(local_file.name, 'wb') as f:
                for chunk in r.iter_content(1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        percent = int(downloaded * 100 / total) if total else 0
                        await status.edit_text(f"⬇️ **Downloading:** {percent}%")
        temp = local_file.name

    await status.edit_text("✏️ **Send me a new file name (without extension).**\nReply `no` to keep original.")
    reply = await message.chat.ask("Send the new filename (or type 'no' to keep original):")
    if reply.text.lower() != "no":
        name, ext = os.path.splitext(orig_filename)
        new_filename = reply.text + ext
    else:
        new_filename = orig_filename

    await status.edit_text("📤 **Uploading to Google Drive...**")
    drive_service = build_drive_service(creds)
    media = MediaFileUpload(temp, resumable=True)
    file_metadata = {'name': new_filename}
    drive_file = drive_service.files().create(body=file_metadata, media_body=media, fields='id').execute()
    file_id = drive_file.get('id')
    drive_service.permissions().create(fileId=file_id, body={'role': 'reader', 'type': 'anyone'}).execute()
    link = f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"

    os.remove(temp)
    elapsed = round(time.time() - start_time, 2)
    upload_count[message.from_user.id] = upload_count.get(message.from_user.id, 0) + 1

    await status.edit_text(
        f"✅ **Uploaded Successfully!**\n\n"
        f"📁 File: `{new_filename}`\n"
        f"🔗 [View in Drive]({link})\n"
        f"⏱ Time Taken: `{elapsed}s`"
    )

# === Run Flask + Bot ===
def run_flask():
    app.run(host="0.0.0.0", port=8080)

threading.Thread(target=run_flask).start()
bot.run()
