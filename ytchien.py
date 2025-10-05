import logging
import feedparser
import httpx
import re
import json
import os
import asyncio
from functools import wraps
import threading
from flask import Flask, request, Response
import html
import sys
import xmltodict

from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackContext
from telegram.constants import ParseMode

# --- CẤU HÌNH LOGGING ---
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)
# ------------------------------------

# --- CẤU HÌNH BOT TỪ BIẾN MÔI TRƯỜNG ---
BOT_TOKEN = os.getenv('BOT_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')
ADMIN_USER_ID_STR = os.getenv('ADMIN_USER_ID')
CALLBACK_URL = os.getenv('RENDER_EXTERNAL_URL', 'https://your-app-name.onrender.com') + "/youtube_webhook"
# ------------------------------------

if not all([BOT_TOKEN, CHAT_ID, ADMIN_USER_ID_STR]):
    logger.critical("LỖI: Thiếu biến môi trường bắt buộc: BOT_TOKEN, CHAT_ID, ADMIN_USER_ID")
    sys.exit(1)

ADMIN_USER_ID = int(ADMIN_USER_ID_STR)

# --- CẤU HÌNH JSONBIN.IO ---
JSONBIN_API_KEY = os.getenv('JSONBIN_API_KEY')
JSONBIN_BIN_ID = os.getenv('JSONBIN_BIN_ID')
JSONBIN_URL = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
# ------------------------------------

HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
state_lock = asyncio.Lock()
app = Flask(__name__)

# --- SỬA LỖI: BIẾN TOÀN CỤC ĐỂ GIAO TIẾP GIỮA CÁC LUỒNG ---
application: Application = None
bot_loop: asyncio.AbstractEventLoop = None
# -----------------------------------------------------------

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

async def get_channel_id_from_url(client: httpx.AsyncClient, channel_url):
    try:
        response = await client.get(channel_url, timeout=15, follow_redirects=True)
        response.raise_for_status()
        match = re.search(r'"channelId":"(UC[A-Za-z0-9_-]{22})"', response.text)
        if match: return match.group(1)
        else: return None
    except httpx.RequestError as e:
        logger.error(f"Lỗi khi lấy Channel ID từ {channel_url}: {e}")
        return None

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
    await update.message.reply_text("👋 Chào bạn! Tôi là bot thông báo video YouTube, phiên bản Webhook tốc độ cao.")

async def help_command(update: Update, context: CallbackContext):
    await update.message.reply_text("Các lệnh: /add, /remove, /list, /resubscribeall")

async def manage_subscription(channel_id: str, mode: str = "subscribe"):
    topic_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    data = {'hub.mode': mode, 'hub.topic': topic_url, 'hub.callback': CALLBACK_URL, 'hub.verify': 'async', 'hub.lease_seconds': 432000}
    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(HUB_URL, data=data)
            if 200 <= response.status_code < 300:
                logger.info(f"Yêu cầu '{mode}' cho kênh {channel_id} đã được gửi thành công.")
                return True
            else:
                logger.error(f"Lỗi khi gửi yêu cầu '{mode}' cho kênh {channel_id}. Status: {response.status_code}, Body: {response.text}")
                return False
        except httpx.RequestError as e:
            logger.error(f"Lỗi mạng khi gửi yêu cầu '{mode}': {e}")
            return False

async def process_notification(xml_data: bytes):
    logger.info("Đã nhận được thông báo từ Hub.")
    try:
        data = xmltodict.parse(xml_data)
        entry = data.get('feed', {}).get('entry')
        if not entry: return
        video_id, channel_id = entry.get('yt:videoId'), entry.get('yt:channelId')
        if not video_id or not channel_id: return
            
        async with httpx.AsyncClient() as client:
            async with state_lock:
                state = await load_state(client)
                if state is None or channel_id not in state.get("channels", {}): return
                last_known_id = state["channels"][channel_id].get("last_video_id")
                if last_known_id != video_id:
                    logger.info(f"Phát hiện video mới {video_id} cho kênh {channel_id}.")
                    channel_name = html.escape(state["channels"][channel_id].get('name', entry.get('author', {}).get('name')))
                    video_title, video_link = html.escape(entry.get('title')), entry.get('link', {}).get('@href')
                    message = (f"📺 <b>{channel_name}</b> vừa ra video mới!\n\n<b>{video_title}</b>\n\n<a href='{video_link}'>Xem ngay tại đây</a>")
                    await application.bot.send_message(chat_id=CHAT_ID, text=message, parse_mode=ParseMode.HTML)
                    state["channels"][channel_id]["last_video_id"] = video_id
                    await save_state(client, state)
    except Exception as e:
        logger.error(f"Lỗi nghiêm trọng khi xử lý thông báo: {e}", exc_info=True)

@app.route('/youtube_webhook', methods=['GET', 'POST'])
def webhook_endpoint():
    if request.method == 'GET':
        challenge = request.args.get('hub.challenge')
        if challenge:
            logger.info("Xác thực webhook thành công với Hub.")
            return Response(challenge, status=200, mimetype='text/plain')
        return Response("No challenge", status=400, mimetype='text/plain')
    elif request.method == 'POST':
        # --- SỬA LỖI: SỬ DỤNG PHƯƠNG THỨC AN TOÀN ĐỂ GỬI TÁC VỤ ---
        if bot_loop:
            asyncio.run_coroutine_threadsafe(process_notification(request.data), bot_loop)
        # -----------------------------------------------------------
        return Response("OK", status=200)

@restricted
async def add_channel(update: Update, context: CallbackContext):
    if not context.args: await update.message.reply_text("Vui lòng nhập link kênh hoặc Channel ID."); return
    user_input = context.args[0]
    async with httpx.AsyncClient() as client:
        async with state_lock:
            state = await load_state(client)
            if state is None: await update.message.reply_text("⚠️ Lỗi: Không thể kết nối DB."); return
            channel_id, final_url = None, None
            if user_input.startswith("UC") and len(user_input) == 24: channel_id, final_url = user_input, f"https://www.youtube.com/channel/{user_input}"
            elif user_input.startswith("http"): final_url, channel_id = user_input, await get_channel_id_from_url(client, user_input)
            else: await update.message.reply_text("❌ Định dạng không hợp lệ."); return
            if not channel_id: await update.message.reply_text("❌ Không tìm thấy Channel ID."); return
            if channel_id in state["channels"]: await update.message.reply_text("✅ Kênh đã có trong danh sách."); return
            feed = await asyncio.to_thread(feedparser.parse, f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}')
            channel_name = feed.feed.get('title', "Tên không xác định")
            state["channels"][channel_id] = {"url": final_url, "name": channel_name, "last_video_id": None}
            if await save_state(client, state):
                safe_name = html.escape(channel_name)
                await update.message.reply_text(f"✅ Đã thêm kênh: <b>{safe_name}</b>. Đang đăng ký nhận thông báo...", parse_mode=ParseMode.HTML)
                if await manage_subscription(channel_id, "subscribe"): await update.message.reply_text(f"✅ Đăng ký cho <b>{safe_name}</b> thành công!", parse_mode=ParseMode.HTML)
                else: await update.message.reply_text(f"⚠️ Lỗi khi đăng ký cho <b>{safe_name}</b>.", parse_mode=ParseMode.HTML)
            else: await update.message.reply_text("⚠️ Lỗi: Không thể lưu thay đổi.")

@restricted
async def remove_channel(update: Update, context: CallbackContext):
    if not context.args: await update.message.reply_text("Vui lòng nhập link/ID cần xóa."); return
    user_input = context.args[0]
    async with httpx.AsyncClient() as client:
        async with state_lock:
            state = await load_state(client)
            if state is None: await update.message.reply_text("⚠️ Lỗi: Không thể kết nối DB."); return
            channel_id_to_remove = None
            if user_input.startswith("UC") and len(user_input) == 24: channel_id_to_remove = user_input
            elif user_input.startswith("http"): channel_id_to_remove = await get_channel_id_from_url(client, user_input)
            else: await update.message.reply_text("❌ Định dạng không hợp lệ."); return
            if not channel_id_to_remove: await update.message.reply_text("❌ Không tìm thấy kênh."); return
            if channel_id_to_remove in state["channels"]:
                channel_name = state["channels"][channel_id_to_remove].get('name', 'Kênh không rõ tên')
                del state["channels"][channel_id_to_remove]
                if await save_state(client, state):
                    safe_name = html.escape(channel_name)
                    await update.message.reply_text(f"🗑️ Đã xóa kênh: <b>{safe_name}</b>.", parse_mode=ParseMode.HTML)
                    await manage_subscription(channel_id_to_remove, "unsubscribe")
                else: await update.message.reply_text("⚠️ Lỗi: Không thể lưu thay đổi.")
            else: await update.message.reply_text("Kênh này không có trong danh sách.")

@restricted
async def resubscribeall(update: Update, context: CallbackContext):
    await update.message.reply_text("Bắt đầu quá trình đăng ký lại cho tất cả các kênh...")
    async with httpx.AsyncClient() as client:
        state = await load_state(client)
        if state is None or not state.get("channels"): await update.message.reply_text("Không có kênh nào."); return
        success_count, fail_count = 0, 0
        for channel_id in state["channels"]:
            if await manage_subscription(channel_id, "subscribe"): success_count += 1
            else: fail_count += 1
            await asyncio.sleep(1)
    await update.message.reply_text(f"Hoàn tất! Thành công: {success_count}, thất bại: {fail_count}.")

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
    global application, bot_loop
    # --- SỬA LỖI: LƯU LẠI EVENT LOOP CỦA BOT ---
    bot_loop = asyncio.new_event_loop()
    # -------------------------------------------
    asyncio.set_event_loop(bot_loop)
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("add", add_channel))
    application.add_handler(CommandHandler("remove", remove_channel))
    application.add_handler(CommandHandler("list", list_channels))
    application.add_handler(CommandHandler("resubscribeall", resubscribeall))
    logger.info("Bot Telegram đã khởi động và đang chạy...")
    application.run_polling(stop_signals=None)

if __name__ == "__main__":
    if not all([JSONBIN_API_KEY, JSONBIN_BIN_ID]):
        logger.error("Thiếu biến môi trường JSONBIN_API_KEY hoặc JSONBIN_BIN_ID!")
    else:
        logger.info("Khởi động luồng cho bot Telegram...")
        bot_thread = threading.Thread(target=run_bot)
        bot_thread.daemon = True
        bot_thread.start()
        app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))