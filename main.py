import os
import time
import math
import tempfile
import requests
import pymongo
import json
from pyrogram import Client, filters
import pyromod.listen
from pyrogram.types import Message
from google_auth_oauthlib.flow import InstalledAppFlow
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

# === Mongo Setup ===
mongo_client = pymongo.MongoClient(MONGO_URI)
db = mongo_client['gdrive_bot']
tokens_collection = db['tokens']
folders_collection = db['folders']

# === Pyrogram Bot ===
bot = Client("gdrive_bot", bot_token=BOT_TOKEN, api_id=API_ID, api_hash=API_HASH)
SCOPES = ['https://www.googleapis.com/auth/drive']
upload_count = {}

# === Helpers ===
def human_readable(size):
    for unit in ['B','KB','MB','GB','TB']:
        if size < 1024:
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"

def eta(start, done, total):
    if done == 0: return "Calculating..."
    speed = done / (time.time() - start)
    remaining = (total - done) / speed
    return f"{int(remaining)}s"

def get_user_creds(user_id):
    token_data = tokens_collection.find_one({"user_id": user_id})
    if not token_data: return None
    return Credentials.from_authorized_user_info(json.loads(token_data['token']))

def build_drive_service(creds):
    return build('drive', 'v3', credentials=creds)

def get_or_create_user_folder(service, user_id):
    folder = folders_collection.find_one({"user_id": user_id})
    if folder: return folder['folder_id']
    file_metadata = {
        'name': f"GDriveBot_{user_id}",
        'mimeType': 'application/vnd.google-apps.folder'
    }
    folder = service.files().create(body=file_metadata, fields='id').execute()
    folders_collection.update_one({"user_id": user_id}, {"$set": {"folder_id": folder['id']}}, upsert=True)
    return folder['id']

# === Commands ===
@bot.on_message(filters.command("start") & filters.private)
async def start(_, message: Message):
    await message.reply_text(
        "👋 **Welcome to Google Drive Uploader Bot!**\n\n"
        "**Commands:**\n"
        "/login - Connect Google Drive\n"
        "/logout - Disconnect Google Drive\n"
        "/driveit - Upload files or links\n"
        "/storage - View Drive storage"
    )

@bot.on_message(filters.command("login") & filters.private)
async def login(_, message: Message):
    flow = InstalledAppFlow.from_client_config(
        {
            "installed": {
                "client_id": GOOGLE_CLIENT_ID,
                "project_id": "drive-uploader-bot",
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uris": ["urn:ietf:wg:oauth:2.0:oob"]
            }
        },
        SCOPES
    )
    auth_url, _ = flow.authorization_url(prompt='consent')
    await message.reply_text(
        f"🔗 **Login to Google Drive**\n1. [Click here]({auth_url})\n2. Allow access & copy the code.\n3. Send me the code."
    )
    code_msg = await message.chat.ask("Now send the code from Google:")
    flow.fetch_token(code=code_msg.text.strip())
    creds = flow.credentials
    tokens_collection.update_one({"user_id": message.from_user.id}, {"$set": {"token": creds.to_json()}}, upsert=True)
    await message.reply_text("✅ **Google Drive linked!** Now use /driveit.")

@bot.on_message(filters.command("logout") & filters.private)
async def logout(_, message: Message):
    tokens_collection.delete_one({"user_id": message.from_user.id})
    await message.reply_text("✅ **Logged out successfully!**")

@bot.on_message(filters.command("storage") & filters.private)
async def storage(_, message: Message):
    creds = get_user_creds(message.from_user.id)
    if not creds: return await message.reply_text("❌ **Login first using /login**")
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
    await message.reply_text("📤 **Send me a file or a direct download link.**")

@bot.on_message(filters.private & (filters.document | filters.video | filters.audio | filters.photo | filters.text))
async def handle_upload(_, message: Message):
    creds = get_user_creds(message.from_user.id)
    if not creds: return await message.reply_text("❌ **Login first using /login**")
    if message.text and not message.text.startswith("http") and not message.text.startswith("/"): return

    status = await message.reply_text("⏳ **Downloading...**")
    start_time = time.time()

    # === Download with progress ===
    if message.document or message.video or message.audio or message.photo:
        async def progress(current, total):
            percent = int(current * 100 / total)
            await status.edit_text(f"⬇️ **Downloading:** {percent}% | ETA: {eta(start_time, current, total)}")
        temp = await message.download(progress=progress)
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
                        await status.edit_text(f"⬇️ **Downloading:** {percent}% | ETA: {eta(start_time, downloaded, total)}")
        temp = local_file.name

    # === Ask rename ===
    await status.edit_text("✏️ **Send a new file name (no extension).** Reply `no` to keep original.")
    reply = await message.chat.ask("Send new filename (or 'no'):")
    new_filename = (reply.text + os.path.splitext(orig_filename)[1]) if reply.text.lower() != "no" else orig_filename

    # === Upload with progress ===
    await status.edit_text("📤 **Uploading to Google Drive...**")
    drive_service = build_drive_service(creds)
    folder_id = get_or_create_user_folder(drive_service, message.from_user.id)
    media = MediaFileUpload(temp, resumable=True)
    file_metadata = {'name': new_filename, 'parents': [folder_id]}
    request = drive_service.files().create(body=file_metadata, media_body=media, fields='id')

    response = None
    while response is None:
        status_, response = request.next_chunk()
        if status_:
            percent = int(status_.progress() * 100)
            await status.edit_text(f"⬆️ **Uploading:** {percent}% | ETA: {eta(start_time, status_.resumable_progress, media.size)}")

    file_id = response.get('id')
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

bot.run()
