import logging
import httpx
import json
import os
import asyncio
from functools import wraps
import threading
from flask import Flask
import html
import sys

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from telegram.constants import ParseMode
from datetime import datetime, timedelta, timezone

# --- CẤU HÌNH LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# ------------------------------------

# --- CẤU HÌNH BOT TỪ BIẾN MÔI TRƯỜNG ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
ADMIN_USER_ID_STR = os.getenv('ADMIN_USER_ID')
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY') # BIẾN MỚI
# ------------------------------------

if not all([BOT_TOKEN, CHAT_ID, ADMIN_USER_ID_STR, YOUTUBE_API_KEY]):
    logger.critical("LỖI: Thiếu một trong các biến môi trường bắt buộc!")
    sys.exit(1)

ADMIN_USER_ID = int(ADMIN_USER_ID_STR)
CHECK_INTERVAL = 120 # Đặt 2 phút một lần để không hết hạn ngạch API

# --- CẤU HÌNH JSONBIN.IO ---
JSONBIN_API_KEY = os.getenv('JSONBIN_API_KEY')
JSONBIN_BIN_ID = os.getenv('JSONBIN_BIN_ID')
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
# ------------------------------------

state_lock = asyncio.Lock()
app = Flask(__name__)
application: Application = None

# (Các hàm load_state, save_state, restricted, start, help giữ nguyên)
async def load_state(client: httpx.AsyncClient):
    headers = {'X-Master-Key': JSONBIN_API_KEY}
    try:
        response = await client.get(f"{JSONBIN_URL}/latest", headers=headers, timeout=15)
        response.raise_for_status()
        return response.json().get('record', {"channels": {}})
    except httpx.RequestError as e:
        logger.error(f"Lỗi khi đọc trạng thái: {e}")
        return None

async def save_state(client: httpx.AsyncClient, state):
    headers = {'Content-Type': 'application/json', 'X-Master-Key': JSONBIN_API_KEY}
    try:
        data_to_save = json.dumps(state, ensure_ascii=False).encode('utf-8')
        response = await client.put(JSONBIN_URL, headers=headers, content=data_to_save, timeout=15)
        response.raise_for_status()
        logger.info("Đã lưu trạng thái thành công lên JSONBin.io")
        return True
    except httpx.RequestError as e:
        logger.error(f"Lỗi khi lưu trạng thái lên JSONBin: {e}")
        return False

def restricted(func):
    @wraps(func)
    async def wrapped(update: Update, context: CallbackContext, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id != ADMIN_USER_ID:
            await update.message.reply_text("⛔ Bạn không có quyền sử dụng lệnh này.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

async def start(update: Update, context: CallbackContext):
    await update.message.reply_text("👋 Chào bạn! Tôi là bot thông báo video YouTube, phiên bản YouTube Data API.")

async def help_command(update: Update, context: CallbackContext):
    await update.message.reply_text("Các lệnh: /add, /remove, /list")

# --- HÀM KIỂM TRA MỚI SỬ DỤNG YOUTUBE DATA API ---
async def youtube_check_callback(context: CallbackContext):
    logger.info("Bắt đầu chu kỳ kiểm tra video mới bằng YouTube API...")
    async with httpx.AsyncClient() as client:
        async with state_lock:
            state = await load_state(client)
            if state is None or not state.get("channels"):
                logger.info("Danh sách kênh trống, bỏ qua.")
                return

            something_changed = False
            for channel_id, data in state["channels"].items():
                try:
                    # Gọi API của YouTube để tìm video mới nhất
                    api_url = "https://www.googleapis.com/youtube/v3/search"
                    params = {
                        'key': YOUTUBE_API_KEY,
                        'channelId': channel_id,
                        'part': 'snippet',
                        'order': 'date',
                        'maxResults': 1,
                        'type': 'video'
                    }
                    response = await client.get(api_url, params=params)
                    if response.status_code != 200:
                        logger.error(f"Lỗi API khi kiểm tra kênh {channel_id}: {response.text}")
                        continue

                    api_data = response.json()
                    if not api_data.get('items'):
                        continue
                    
                    latest_video = api_data['items'][0]
                    video_id = latest_video['id']['videoId']
                    last_known_id = data.get("last_video_id")
                    
                    # Chỉ thông báo video được đăng trong vòng 1 giờ qua để tránh thông báo video cũ
                    published_at_str = latest_video['snippet']['publishedAt']
                    published_at = datetime.fromisoformat(published_at_str.replace('Z', '+00:00'))
                    now = datetime.now(timezone.utc)
                    
                    if last_known_id != video_id and (now - published_at) < timedelta(hours=1):
                        channel_name = html.escape(data.get('name', latest_video['snippet']['channelTitle']))
                        video_title = html.escape(latest_video['snippet']['title'])
                        video_link = f"https://www.youtube.com/watch?v={video_id}"
                        
                        message = (f"📺 <b>{channel_name}</b> vừa ra video mới!\n\n"
                                   f"<b>{video_title}</b>\n\n"
                                   f'<a href="{video_link}">Xem ngay tại đây</a>')
                                   
                        await context.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.HTML)
                        
                        state["channels"][channel_id]["last_video_id"] = video_id
                        something_changed = True
                    elif last_known_id is None: # Lần đầu kiểm tra, chỉ cập nhật
                        state["channels"][channel_id]["last_video_id"] = video_id
                        something_changed = True

                except Exception as e:
                    logger.error(f"Lỗi khi xử lý kênh {data.get('name', 'N/A')}: {e}", exc_info=True)
                
                await asyncio.sleep(1)

            if something_changed:
                await save_state(client, state)
    logger.info("Kết thúc chu kỳ kiểm tra.")

# --- CÁC HÀM CŨ ĐÃ CẬP NHẬT ---
# (Các hàm add, remove, list sẽ không cần thay đổi nhiều)

# ... (Dán các hàm add_channel, remove_channel, list_channels từ phiên bản trước vào đây,
# chúng vẫn hoạt động tốt, chỉ cần xóa logic manage_subscription đi) ...

# Đây là các hàm đã chỉnh sửa:
@restricted
async def add_channel(update: Update, context: CallbackContext):
    if not context.args: await update.message.reply_text("Vui lòng nhập link kênh hoặc Channel ID."); return
    user_input = context.args[0]
    async with httpx.AsyncClient() as client:
        async with state_lock:
            state = await load_state(client)
            if state is None: await update.message.reply_text("⚠️ Lỗi: Không thể kết nối DB."); return
            channel_id, final_url = None, None
            # Tạm thời dùng API để lấy tên kênh thay vì feedparser
            if user_input.startswith("UC") and len(user_input) == 24: channel_id, final_url = user_input, f"https://www.youtube.com/channel/{user_input}"
            elif user_input.startswith("http"): 
                 await update.message.reply_text("Chức năng thêm bằng link chưa được hỗ trợ trong phiên bản này, vui lòng nhập Channel ID (bắt đầu bằng UC...)."); return
            else: await update.message.reply_text("❌ Định dạng không hợp lệ, vui lòng nhập Channel ID."); return
            if not channel_id: await update.message.reply_text("❌ Không tìm thấy Channel ID."); return
            if channel_id in state["channels"]: await update.message.reply_text("✅ Kênh đã có trong danh sách."); return
            
            # Lấy tên kênh từ API
            api_url = f"https://www.googleapis.com/youtube/v3/channels?part=snippet&id={channel_id}&key={YOUTUBE_API_KEY}"
            response = await client.get(api_url)
            channel_name = "Tên không xác định"
            if response.status_code == 200 and response.json().get('items'):
                channel_name = response.json()['items'][0]['snippet']['title']
            
            state["channels"][channel_id] = {"url": final_url, "name": channel_name, "last_video_id": None}
            if await save_state(client, state):
                safe_name = html.escape(channel_name)
                await update.message.reply_text(f"✅ Đã thêm kênh: <b>{safe_name}</b>.", parse_mode=ParseMode.HTML)
            else: await update.message.reply_text("⚠️ Lỗi: Không thể lưu thay đổi.")


@restricted
async def remove_channel(update: Update, context: CallbackContext):
    if not context.args: await update.message.reply_text("Vui lòng nhập Channel ID cần xóa."); return
    channel_id_to_remove = context.args[0]
    async with httpx.AsyncClient() as client:
        async with state_lock:
            state = await load_state(client);
            if state is None: await update.message.reply_text("⚠️ Lỗi: Không thể kết nối DB."); return
            if not channel_id_to_remove.startswith("UC"): await update.message.reply_text("❌ Vui lòng nhập Channel ID hợp lệ."); return
            
            if channel_id_to_remove in state["channels"]:
                channel_name = state["channels"][channel_id_to_remove].get('name', 'Kênh không rõ tên')
                del state["channels"][channel_id_to_remove]
                if await save_state(client, state):
                    safe_name = html.escape(channel_name)
                    await update.message.reply_text(f"🗑️ Đã xóa kênh: <b>{safe_name}</b>.", parse_mode=ParseMode.HTML)
                else: await update.message.reply_text("⚠️ Lỗi: Không thể lưu thay đổi.")
            else: await update.message.reply_text("Kênh này không có trong danh sách.")

@restricted
async def list_channels(update: Update, context: CallbackContext):
    async with httpx.AsyncClient() as client:
        state = await load_state(client)
        if state is None: await update.message.reply_text("⚠️ Lỗi: Không thể kết nối tới DB."); return
        if not state.get("channels"): await update.message.reply_text("Không có kênh nào."); return
        message_parts = ["📜 <b>Các kênh đang được theo dõi:</b>\n"]
        for i, (channel_id, data) in enumerate(state["channels"].items(), 1):
            name, url = html.escape(data.get('name', '')), html.escape(data.get('url', '#'))
            message_parts.append(f"<b>{i}. {name}</b>\n   - Link: {url}\n   - ID: <code>{channel_id}</code>\n")
        await update.message.reply_text("\n".join(message_parts), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def run_bot():
    global application
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_channel))
    application.add_handler(CommandHandler("remove", remove_channel))
    application.add_handler(CommandHandler("list", list_channels))
    
    # KÍCH HOẠT LẠI JOBQUEUE
    job_queue = application.job_queue
    job_queue.run_repeating(youtube_check_callback, interval=CHECK_INTERVAL, first=10)
    
    logger.info("Bot Telegram đã khởi động và đang chạy...")
    application.run_polling(stop_signals=None)

# --- PHẦN KHỞI ĐỘNG CHÍNH ---
@app.route('/')
def hello_world():
    return "Telegram bot (API Version) is running in the background!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

if __name__ == "__main__":
    if not all([JSONBIN_API_KEY, JSONBIN_BIN_ID]):
        logger.error("Thiếu biến môi trường JSONBIN_API_KEY hoặc JSONBIN_BIN_ID!")
    else:
        logger.info("Khởi động luồng cho bot Telegram...")
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        run_flask()