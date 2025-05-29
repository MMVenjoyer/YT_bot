import asyncio
import os
import yt_dlp
import requests
from collections import deque
from typing import Deque, Tuple
from datetime import datetime

from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.enums import ParseMode

# ==== НАСТРОЙКИ ====
BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
YANDEX_TOKEN = "YOUR_YANDEX_DISK_TOKEN"
YANDEX_UPLOAD_DIR = "/YouTubeDownloads"
TEMP_DIR = "tmp_downloads"
LOG_FILE = "upload_log.txt"

os.makedirs(TEMP_DIR, exist_ok=True)

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher()
router = Router()
dp.include_router(router)

user_task_queue: Deque[Tuple[int, str]] = deque()
is_processing = False

# ==== КЛАСС ПРОГРЕССА ====


class ProgressTracker:
    def __init__(self, message: Message):
        self.message = message
        self.last_percent = 0

    async def progress_hook(self, d):
        if d['status'] == 'downloading':
            percent = float(
                d.get('_percent_str', '0').strip().replace('%', ''))
            if int(percent) - self.last_percent >= 5:
                self.last_percent = int(percent)
                await self.message.edit_text(f"⏳ Загрузка… {self.last_percent}%")
        elif d['status'] == 'finished':
            await self.message.edit_text("📦 Скачивание завершено. Загружаю в Яндекс.Диск…")


# ==== СКАЧИВАНИЕ ВИДЕО ====
async def download_video_with_progress(url: str, tracker: ProgressTracker) -> str:
    output_template = os.path.join(TEMP_DIR, '%(title).200s.%(ext)s')
    ydl_opts = {
        'format': 'bestvideo+bestaudio/best',
        'merge_output_format': 'mp4',
        'outtmpl': output_template,
        'noplaylist': True,
        'progress_hooks': [tracker.progress_hook],
        'quiet': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        return ydl.prepare_filename(info)


# ==== ЯНДЕКС ДИСК ====
def upload_to_yandex_disk(local_file_path: str, yandex_path: str) -> bool:
    upload_url = "https://cloud-api.yandex.net/v1/disk/resources/upload"
    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
    params = {"path": yandex_path, "overwrite": "true"}
    response = requests.get(upload_url, headers=headers, params=params)
    if response.status_code != 200:
        return False
    with open(local_file_path, "rb") as f:
        upload_response = requests.put(
            response.json()['href'], files={'file': f})
    return upload_response.status_code == 201


def get_public_link(yandex_path: str) -> str | None:
    headers = {"Authorization": f"OAuth {YANDEX_TOKEN}"}
    requests.put("https://cloud-api.yandex.net/v1/disk/resources/publish",
                 headers=headers, params={"path": yandex_path})
    res = requests.get("https://cloud-api.yandex.net/v1/disk/resources",
                       headers=headers, params={"path": yandex_path})
    if res.status_code == 200:
        return res.json().get("public_url")
    return None


def log_event(user_id: int, message: str):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{datetime.now()}] User {user_id}: {message}\n")


# ==== ОБРАБОТКА ОЧЕРЕДИ ====
async def process_queue():
    global is_processing
    if is_processing or not user_task_queue:
        return

    is_processing = True
    while user_task_queue:
        user_id, url = user_task_queue.popleft()
        try:
            status_msg = await bot.send_message(user_id, f"⏳ Загрузка началась: {url}")
            tracker = ProgressTracker(status_msg)
            filename = await download_video_with_progress(url, tracker)

            file_name_only = os.path.basename(filename)
            yandex_path = f"{YANDEX_UPLOAD_DIR}/{file_name_only}"

            success = upload_to_yandex_disk(filename, yandex_path)

            if success:
                public_link = get_public_link(yandex_path)
                if public_link:
                    await bot.send_message(user_id, f"✅ Загружено в Яндекс.Диск:\n<b>{file_name_only}</b>\n🔗 <a href='{public_link}'>Скачать</a>")
                    log_event(user_id, f"Успешно загружено: {public_link}")
                else:
                    await bot.send_message(user_id, f"✅ Загружено, но не удалось получить ссылку.")
                    log_event(user_id, f"Загружено, но нет ссылки: {filename}")
            else:
                await bot.send_message(user_id, f"❌ Ошибка при загрузке в Яндекс.Диск.")
                log_event(
                    user_id, f"Ошибка загрузки в Яндекс.Диск: {filename}")

            os.remove(filename)

        except Exception as e:
            await bot.send_message(user_id, f"⚠️ Ошибка при обработке ссылки:\n<code>{str(e)}</code>")
            log_event(user_id, f"Ошибка: {str(e)}")

    is_processing = False


# ==== ХЭНДЛЕРЫ ====
@router.message(Command("start"))
async def start_handler(message: Message):
    await message.answer("👋 Привет! Просто отправь ссылку на YouTube-видео, и я загружу его в твой Яндекс.Диск.")


@router.message(Command("queue"))
async def queue_status(message: Message):
    user_id = message.chat.id
    if not user_task_queue:
        await message.answer("📭 Очередь пуста.")
        return
    total = len(user_task_queue)
    positions = [i + 1 for i,
                 (uid, _) in enumerate(user_task_queue) if uid == user_id]
    if positions:
        pos_list = ', '.join(map(str, positions))
        await message.answer(f"📊 Всего в очереди: <b>{total}</b>\n🧍‍♂️ Вы в очереди на позициях: {pos_list}")
    else:
        await message.answer(f"📊 Всего в очереди: <b>{total}</b>\n❌ У вас нет активных задач.")


@router.message(Command("cancel"))
async def cancel_user_tasks(message: Message):
    user_id = message.chat.id
    before = len(user_task_queue)
    user_task_queue_copy = deque(
        [item for item in user_task_queue if item[0] != user_id])
    removed = before - len(user_task_queue_copy)
    user_task_queue.clear()
    user_task_queue.extend(user_task_queue_copy)
    await message.answer(f"❌ Отменено задач: {removed}")
    log_event(user_id, f"Отменено задач: {removed}")


@router.message(F.text.startswith("http"))
async def handle_youtube_link(message: Message):
    url = message.text.strip()
    user_task_queue.append((message.chat.id, url))
    await message.answer("📥 Ссылка добавлена в очередь. Ожидай уведомление по завершению.")
    log_event(message.chat.id, f"Добавлена ссылка в очередь: {url}")
    await process_queue()


# ==== ЗАПУСК ====
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
