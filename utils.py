# utils.py

import asyncio
import logging
import json
import os
import re
import base64
import uuid
import random
from datetime import datetime, timedelta
from pathlib import Path
from functools import wraps
from typing import Union

import aiohttp
import cv2
from PIL import Image
from pyrogram import Client, errors
from pyrogram.types import InputMediaPhoto
from aiogram import Bot
from aiogram.types import CallbackQuery, Message
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from config import OPENAI_API_KEY

# --- Импорты из config.py ---
try:
    from config import (
        BOT_TOKEN, ADMIN_IDS, AZURE_API_KEY, AZURE_REGION,
        REPORTING_CHANNEL_ID, STORAGE_CHANNEL_ID,
        OPENAI_API_KEY, OPENAI_MODEL, CLAUDE_API_KEY, GEMINI_API_KEY,
        DEWATERMARK_API_KEY, REPLICATE_API_TOKEN
    )
except ImportError:
    print("Ошибка: не удалось импортировать данные из config.py. Убедитесь, что все переменные на месте.")
    exit()

# --- КОНСТАНТЫ ---
DEFAULT_AI_PROMPT = (
    "Твоя задача — сделать качественный рерайт предоставленного текста. "
    "Внимательно проанализируй конец исходного текста. Если ты обнаружишь там подпись канала, призыв к подписке, упоминание автора или любую другую форму брендинга (например, 'Топор Live. Подписывайтесь', 'Источник: XYZ', 'Больше новостей у нас в канале'), полностью удали эту часть перед рерайтингом. "
    "Твой ответ должен содержать ИСКЛЮЧИТЕЛЬНО переписанный текст. Не добавляй никаких своих комментариев, вступлений или заключений, таких как 'Вот переписанный текст:'. "
    "Цель — выдать чистый, готовый к публикации рерайт основного контента."
)
ALBUM_COLLECTION_DELAY = 2.5
MAX_RETRY_ATTEMPTS = 3
POLLING_INTERVAL_MINUTES = 5

INSTAGRAM_IMAGE_SIZE = (1080, 1350)
TWITTER_IMAGE_SIZE = (1200, 675)
INSTAGRAM_CHAR_LIMIT = 2200
THREADS_CHAR_LIMIT = 500
TWITTER_CHAR_LIMIT = 280
DEWATERMARK_API_URL = "https://platform.dewatermark.ai/api/object_removal/v1/erase_watermark"


# --- ДИРЕКТОРИИ ---
SCRIPT_DIR = Path(__file__).resolve().parent
TASKS_DIR, WATERMARKS_DIR, SESSIONS_DIR, MAILINGS_MEDIA_DIR, STATE_DIR, SOCIAL_POSTS_DIR, TEMP_DIR = (
    SCRIPT_DIR / "tasks", SCRIPT_DIR / "watermarks", SCRIPT_DIR / "sessions",
    SCRIPT_DIR / "mailings_media", SCRIPT_DIR / "task_states", SCRIPT_DIR / "social_posts",
    SCRIPT_DIR / "temp_files"
)
for p in [TASKS_DIR, WATERMARKS_DIR, SESSIONS_DIR, MAILINGS_MEDIA_DIR, STATE_DIR, SOCIAL_POSTS_DIR, TEMP_DIR]: p.mkdir(exist_ok=True)


# --- ОБЩИЕ ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ ---
TASK_LOCKS = {}
ACTIVE_CLIENTS = {}
ACTIVE_TASKS = {}
TEMP_MEDIA_GROUPS = {}


# --- УПРАВЛЕНИЕ ЗАДАЧАМИ ---
async def set_task_error(task_name: str, reason: str, bot: Bot):
    task_lock = await get_task_lock(task_name)
    config_path = TASKS_DIR / f"{task_name}.json"
    async with task_lock:
        config = await read_json_file(config_path)
        if config:
            config['status'] = 'inactive'; config['last_error'] = reason
            await write_json_file(config_path, config)
    try:
        await bot.send_message(ADMIN_IDS[0], f"❗️Задача <b>{task_name}</b> остановлена.\nПричина: {reason}")
    except Exception as e:
        logging.error(f"Не удалось отправить уведомление об ошибке для задачи {task_name}: {e}")

async def stop_worker(task_name: str, bot: Bot, reason: str = "Остановлено администратором."):
    if task_name in ACTIVE_TASKS:
        logging.info(f"Останавливаю задачу {task_name}...")
        task = ACTIVE_TASKS.pop(task_name)
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.CancelledError:
            logging.info(f"Задача {task_name} успешно отменена.")
        except asyncio.TimeoutError:
            logging.warning(f"Задача {task_name} не завершилась после отмены в течение 5 секунд.")
        except Exception as e:
            logging.error(f"Ошибка при ожидании завершения задачи {task_name}: {e}")
    await set_task_error(task_name, reason, bot)

async def check_channel_access(client: Client, task_config: dict, bot: Bot, admin_id: int):
    task_name = task_config['task_name']; task_type = task_config.get('task_type', 'forwarding'); has_errors = False
    if task_type == 'forwarding':
        source_channels = task_config.get('source_channels', [])
        logging.info(f"[{task_name}] Проверка доступа к {len(source_channels)} источникам...")
        for channel_id in source_channels:
            try:
                chat = await client.get_chat(channel_id)
                logging.info(f"[{task_name}] ✅ Доступ к источнику '{chat.title}' ({channel_id}) подтвержден.")
                await asyncio.sleep(0.5)
            except errors.FloodWait as e:
                logging.warning(f"[{task_name}] FloodWait при проверке источника {channel_id}. Пауза {e.x} сек.")
                await asyncio.sleep(e.x)
            except Exception as e:
                await bot.send_message(admin_id, f"❗️<b>[{task_name}]</b> Ошибка доступа к источнику <code>{channel_id}</code>.\n\n<b>Причина:</b> {e.__class__.__name__} - {e}")
                has_errors = True
    if task_type in ['forwarding', 'generative']:
        try:
            target_channel_id = task_config['target_channel']
            chat = await client.get_chat(target_channel_id)
            logging.info(f"[{task_name}] ✅ Доступ к целевому каналу '{chat.title}' ({target_channel_id}) подтвержден.")
        except Exception as e:
            await bot.send_message(admin_id, f"❗️<b>[{task_name}]</b> Критическая ошибка доступа к ЦЕЛЕВОМУ каналу <code>{task_config.get('target_channel')}</code>.\n\n<b>Причина:</b> {e.__class__.__name__} - {e}")
            has_errors = True
    try:
        await client.send_message("me", f"[{task_name}] Self-test message.")
        logging.info(f"[{task_name}] Отправлено тестовое сообщение самому себе.")
    except Exception as e:
        logging.error(f"[{task_name}] Не удалось отправить тестовое сообщение: {e}")
        has_errors = True
    if has_errors:
        await bot.send_message(admin_id, f"<b>[{task_name}]</b> Обнаружены ошибки доступа. Воркер может работать некорректно.")

# --- ОБЩИЕ ФУНКЦИИ ---
def http_retry(max_retries=MAX_RETRY_ATTEMPTS):
    def decorator(async_func):
        @wraps(async_func)
        async def wrapper(*args, **kwargs):
            for attempt in range(max_retries):
                try: return await async_func(*args, **kwargs)
                except aiohttp.ClientError as e:
                    if attempt == max_retries - 1: logging.error(f"HTTP-запрос не удался после {max_retries} попыток: {e}"); return None
                    delay = 2 ** attempt
                    logging.warning(f"Ошибка HTTP-запроса (попытка {attempt + 1}/{max_retries}): {e}. Повтор через {delay} сек...")
                    await asyncio.sleep(delay)
        return wrapper
    return decorator

async def read_json_file(file_path, lock=None):
    def _read():
        if not os.path.exists(file_path): return None
        try:
            with open(file_path, 'r', encoding='utf-8') as f: return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError): return None
    if lock:
        async with lock: return await asyncio.to_thread(_read)
    return await asyncio.to_thread(_read)

async def write_json_file(file_path, data, lock=None):
    def _write():
        with open(file_path, 'w', encoding='utf-8') as f: json.dump(data, f, indent=2, ensure_ascii=False)
    if lock:
        async with lock: await asyncio.to_thread(_write)
    else: await asyncio.to_thread(_write)

async def remove_file_async(path):
    if path and os.path.exists(path):
        try: await asyncio.to_thread(os.remove, path)
        except OSError as e: logging.error(f"Не удалось удалить временный файл {path}: {e}")

async def get_task_lock(task_name):
    if task_name not in TASK_LOCKS: TASK_LOCKS[task_name] = asyncio.Lock()
    return TASK_LOCKS[task_name]

async def read_task_state(task_name: str) -> dict:
    state_file = STATE_DIR / f"{task_name}_state.json"
    state = await read_json_file(state_file)
    return state if isinstance(state, dict) else {}

async def write_task_state(task_name: str, state_data: dict):
    state_file = STATE_DIR / f"{task_name}_state.json"
    await write_json_file(state_file, state_data)

async def safe_answer_callback(callback: CallbackQuery, text: str = None, show_alert: bool = False):
    try: await callback.answer(text, show_alert=show_alert)
    except TelegramBadRequest: pass

# --- КЛАВИАТУРЫ ---
def main_menu_keyboard():
    buttons = [
        [InlineKeyboardButton(text="📋 Форвардинг", callback_data="manage_tasks")],
        [InlineKeyboardButton(text="✨ Генеративные каналы", callback_data="manage_gen_tasks")],
        [InlineKeyboardButton(text="✍️ Ведение каналов", callback_data="manage_channel_tasks")],
        [InlineKeyboardButton(text="📣 Рекламные задачи", callback_data="manage_adv_tasks")],
        [InlineKeyboardButton(text="📨 Глобальные рассылки", callback_data="global_mailing_add")],
        [InlineKeyboardButton(text="📊 Активность", callback_data="view_activity")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
    
def confirm_album_keyboard(media_group_id: str, callback_prefix: str): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Подтвердить альбом", callback_data=f"{callback_prefix}:yes:{media_group_id}")], [InlineKeyboardButton(text="❌ Отменить", callback_data=f"{callback_prefix}:no:{media_group_id}")]])

# --- ОБРАБОТКА КОНТЕНТА ---
@http_retry()
async def azure_translate(text, target_lang):
    if not AZURE_API_KEY or not AZURE_REGION or not text: return text or ""
    endpoint, path, params = "https://api.cognitive.microsofttranslator.com", '/translate', {'api-version': '3.0', 'to': target_lang}
    headers = {'Ocp-Apim-Subscription-Key': AZURE_API_KEY, 'Ocp-Apim-Subscription-Region': AZURE_REGION, 'Content-type': 'application/json'}
    body = [{'text': text}]
    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint + path, params=params, headers=headers, json=body) as resp:
            if resp.status == 200:
                try:
                    return (await resp.json())[0]['translations'][0]['text']
                except (IndexError, KeyError):
                    logging.error(f"Ошибка парсинга ответа Azure: {await resp.text()}")
                    return None
            logging.error(f"Ошибка перевода Azure: {resp.status} - {await resp.text()}"); return None

async def _rewrite_with_openai(text: str, config: dict):
    api_key, model, system_prompt = OPENAI_API_KEY, config.get("model", OPENAI_MODEL), config.get("prompt", DEFAULT_AI_PROMPT)
    if not api_key or api_key.startswith("sk-xxxx"): return None
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": text}], "temperature": 0.7}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload, timeout=120) as resp:
            if resp.status == 200: return (await resp.json())['choices'][0]['message']['content']
            logging.error(f"Ошибка OpenAI API: {resp.status} - {await resp.text()}"); return None

async def _rewrite_with_claude(text: str, config: dict):
    api_key, model, system_prompt = CLAUDE_API_KEY, config.get("model", "claude-3-haiku-20240307"), config.get("prompt", DEFAULT_AI_PROMPT)
    if not api_key: return None
    headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    payload = {"model": model, "system": system_prompt, "messages": [{"role": "user", "content": text}], "max_tokens": 4096, "temperature": 0.7}
    async with aiohttp.ClientSession() as session:
        async with session.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=120) as resp:
            if resp.status == 200: return (await resp.json())['content'][0]['text']
            logging.error(f"Ошибка Claude API: {resp.status} - {await resp.text()}"); return None

async def _rewrite_with_gemini(text: str, config: dict):
    api_key, model, system_prompt = GEMINI_API_KEY, config.get("model", "gemini-1.5-flash"), config.get("prompt", DEFAULT_AI_PROMPT)
    if not api_key: return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {"systemInstruction": {"role": "model", "parts": [{"text": system_prompt}]}, "contents": [{"role": "user", "parts": [{"text": text}]}]}
    headers = {'Content-Type': 'application/json'}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload, timeout=120) as resp:
            if resp.status == 200:
                try: return (await resp.json())['candidates'][0]['content']['parts'][0]['text']
                except (KeyError, IndexError): logging.error(f"Ошибка парсинга ответа Gemini: {await resp.text()}"); return None
            logging.error(f"Ошибка Gemini API: {resp.status} - {await resp.text()}"); return None

async def _rewrite_with_local(text: str, config: dict):
    endpoint, model, system_prompt = config.get("endpoint"), config.get("model", "llama3"), config.get("prompt", DEFAULT_AI_PROMPT)
    if not endpoint: return None
    payload = {"model": model, "system": system_prompt, "prompt": text, "stream": False}; headers = {'Content-Type': 'application/json'}
    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, headers=headers, json=payload, timeout=180) as resp:
            if resp.status == 200:
                try: return (await resp.json())['response']
                except (KeyError, IndexError): logging.error(f"Ошибка парсинга ответа Local AI: {await resp.text()}"); return None
            logging.error(f"Ошибка Local AI: {resp.status} - {await resp.text()}"); return None

@http_retry()
async def rewrite_text_ai(text: str, ai_config: dict | None):
    if not ai_config or not ai_config.get("enabled"): return None
    provider_map = {"openai": _rewrite_with_openai, "claude": _rewrite_with_claude, "gemini": _rewrite_with_gemini, "local": _rewrite_with_local}
    provider = ai_config.get("provider", "openai")
    if provider in provider_map: return await provider_map[provider](text, ai_config)
    logging.warning(f"Неизвестный AI провайдер: {provider}"); return None

async def generate_ai_text(provider: str, prompt: str, system_prompt: str) -> str | None:
    logging.info(f"Запрос на генерацию текста к '{provider}'...")
    ai_config = {"prompt": system_prompt, "enabled": True}
    try:
        if provider == 'openai': return await _rewrite_with_openai(prompt, ai_config)
        elif provider == 'gemini': return await _rewrite_with_gemini(prompt, ai_config)
        elif provider == 'claude': return await _rewrite_with_claude(prompt, ai_config)
    except Exception as e: logging.error(f"Ошибка при генерации текста через {provider}: {e}")
    return None

async def generate_ai_image(provider: str, prompt: str, task_name: str) -> str | None:
    logging.info(f"[{task_name}] Запрос на генерацию изображения к '{provider}'...")
    if provider == 'dalle3':
        if not OPENAI_API_KEY: logging.error(f"[{task_name}] OPENAI_API_KEY не задан."); return None
        headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
        payload = {"model": "dall-e-3", "prompt": prompt, "n": 1, "size": "1024x1024", "quality": "hd"}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post("https://api.openai.com/v1/images/generations", headers=headers, json=payload, timeout=180) as resp:
                    if resp.status == 200:
                        image_url = (await resp.json())['data'][0]['url']
                        async with session.get(image_url) as img_resp:
                            if img_resp.status == 200:
                                output_path = TEMP_DIR / f"gen_{uuid.uuid4().hex}.png"
                                with open(output_path, "wb") as f: f.write(await img_resp.read())
                                logging.info(f"[{task_name}] Изображение DALL-E 3 сохранено в {output_path}"); return str(output_path)
                    else: logging.error(f"[{task_name}] Ошибка API DALL-E 3: {resp.status} - {await resp.text()}")
            except Exception as e: logging.error(f"[{task_name}] Исключение при работе с DALL-E 3: {e}", exc_info=True)
        return None
    elif provider == 'replicate':
        if not REPLICATE_API_TOKEN: logging.error(f"[{task_name}] REPLICATE_API_TOKEN не задан."); return None
        headers = {'Authorization': f'Token {REPLICATE_API_TOKEN}', 'Content-Type': 'application/json'}
        payload = {"version": "d2c52303a2861e6955743b01487f7a8b3c3e800a501a4e215033c5e8841456a1", "input": {"prompt": prompt}}
        async with aiohttp.ClientSession() as session:
            try:
                async with session.post("https://api.replicate.com/v1/predictions", headers=headers, json=payload) as resp:
                    if resp.status not in [200, 201]: logging.error(f"[{task_name}] Ошибка Replicate: {resp.status} - {await resp.text()}"); return None
                    get_url = (await resp.json())["urls"]["get"]
                for _ in range(60):
                    await asyncio.sleep(2)
                    async with session.get(get_url, headers=headers) as get_resp:
                        result = await get_resp.json()
                        if result['status'] == 'succeeded':
                            async with session.get(result['output'][0]) as img_resp:
                                output_path = TEMP_DIR / f"gen_{uuid.uuid4().hex}.png"
                                with open(output_path, "wb") as f: f.write(await img_resp.read())
                                logging.info(f"[{task_name}] Изображение Replicate сохранено в {output_path}"); return str(output_path)
                        elif result['status'] in ['failed', 'canceled']: logging.error(f"[{task_name}] Генерация Replicate не удалась: {result.get('error')}"); return None
            except Exception as e: logging.error(f"[{task_name}] Исключение при работе с Replicate: {e}", exc_info=True)
        return None
    logging.warning(f"[{task_name}] Провайдер изображений '{provider}' не реализован."); return None

async def remove_external_watermark_async(image_path: str, task_name: str, config: dict) -> str | None:
    if not DEWATERMARK_API_KEY: logging.warning(f"[{task_name}] DEWATERMARK_API_KEY не задан."); return None
    logging.info(f"[{task_name}] Удаление внешнего вотермарка с {os.path.basename(image_path)} в режиме {config['mode']}")
    headers = {"X-API-KEY": DEWATERMARK_API_KEY}; form_data = aiohttp.FormData()
    form_data.add_field('remove_text', 'true' if config['mode'] == 'PHOTOS' else 'false')
    try:
        with open(image_path, 'rb') as image_file:
            form_data.add_field('original_preview_image', image_file, filename=os.path.basename(image_path), content_type='image/jpeg')
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
                async with session.post(DEWATERMARK_API_URL, headers=headers, data=form_data) as response:
                    if response.status == 200:
                        response_data = await response.json()
                        if "edited_image" in response_data and "image" in response_data["edited_image"]:
                            image_data = base64.b64decode(response_data["edited_image"]["image"])
                            output_path = TEMP_DIR / f"cleaned_{uuid.uuid4().hex}.png"
                            await asyncio.to_thread(lambda p, d: open(p, 'wb').write(d), output_path, image_data)
                            logging.info(f"[{task_name}] Внешний вотермарк удален. Результат: {output_path}"); return str(output_path)
                    logging.error(f"[{task_name}] Ошибка API dewatermark: {response.status} - {await response.text()}"); return None
    except Exception as e: logging.error(f"[{task_name}] Ошибка при удалении вотермарка: {e}"); return None

async def resize_image(image_path: str, target_size: tuple[int, int], platform: str) -> str | None:
    def _process():
        try:
            image = Image.open(image_path).convert("RGB")
            image.thumbnail(target_size, Image.Resampling.LANCZOS)
            output_path = TEMP_DIR / f"temp_{platform}_{os.path.basename(image_path)}.jpg"
            image.save(output_path, "JPEG", quality=95); return str(output_path)
        except Exception as e: logging.error(f"Ошибка при изменении размера для {platform}: {e}"); return None
    return await asyncio.to_thread(_process)

async def generate_video_thumbnail(video_path: str) -> str | None:
    def _process():
        try:
            cam = cv2.VideoCapture(video_path); ret, frame = cam.read(); cam.release()
            if not ret: logging.error(f"Не удалось извлечь кадр из видео: {video_path}"); return None
            thumbnail_path = f"{os.path.splitext(video_path)[0]}_thumbnail.jpg"; cv2.imwrite(thumbnail_path, frame); return thumbnail_path
        except Exception as e: logging.error(f"Ошибка при создании обложки для видео: {e}", exc_info=True); return None
    return await asyncio.to_thread(_process)

async def apply_watermark_async(image_path, watermark_path):
    if not image_path or not watermark_path or not os.path.exists(watermark_path): return None
    def _process():
        try:
            base_image = Image.open(image_path).convert("RGBA"); watermark = Image.open(watermark_path).convert("RGBA")
            if watermark.mode == 'RGBA': r, g, b, a = watermark.split(); a = a.point(lambda i: i * 0.25); watermark.putalpha(a)
            width, height = base_image.size; wm_width = int(width * 0.25); w_percent = (wm_width / float(watermark.size[0])); w_size = int((float(watermark.size[1]) * float(w_percent)))
            watermark = watermark.resize((wm_width, w_size), Image.Resampling.LANCZOS)
            position = (width - wm_width - 10, height - w_size - 10)
            transparent = Image.new('RGBA', base_image.size, (0,0,0,0)); transparent.paste(base_image, (0,0)); transparent.paste(watermark, position, mask=watermark)
            output_path = TEMP_DIR / f"temp_watermarked_{uuid.uuid4().hex}.jpg"; transparent.convert("RGB").save(output_path, "JPEG"); return str(output_path)
        except Exception as e: logging.error(f"Ошибка наложения водяного знака: {e}"); return None
    return await asyncio.to_thread(_process)

async def set_social_status(task_name: str, platform: str, status: str):
    task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if config and platform in config: config[platform]['last_status'] = status; await write_json_file(TASKS_DIR / f"{task_name}.json", config)

async def save_social_post_id(task_name, platform, post_id, text, media_count):
    lock = await get_task_lock(f"{task_name}_social"); file_path = SOCIAL_POSTS_DIR / f"{task_name}.json"
    posts = await read_json_file(file_path, lock) or []
    posts.append({"platform": platform, "post_id": str(post_id), "text": text, "media_count": media_count, "timestamp": datetime.now().isoformat(), "stats": {"likes": 0, "comments": 0, "views": 0}})
    await write_json_file(file_path, posts, lock)

def get_text_for_platform(full_text, _, char_limit, hashtag_count):
    hashtags = re.findall(r'#\w+', full_text)
    text_without_hashtags = re.sub(r'#\w+', '', full_text).strip()
    selected_hashtags = " ".join(hashtags[:hashtag_count])
    available_len = char_limit - (len(selected_hashtags) + 2)
    if len(text_without_hashtags) > available_len: text_without_hashtags = text_without_hashtags[:available_len-3] + "..."
    final_text = text_without_hashtags
    if selected_hashtags: final_text += f"\n\n{selected_hashtags}"
    return final_text.strip()

async def post_to_instagram(task_name: str, config: dict, text: str, media_paths: list[str]):
    try: from instagrapi import Client as InstagramClient; from instagrapi.exceptions import LoginRequired, BadPassword
    except ImportError: logging.warning(f"[{task_name}] instagrapi не установлена."); return
    insta_config = config.get("instagram", {})
    if not insta_config.get("enabled") or not media_paths: return
    temp_files_to_clean = []
    def _post():
        cl = InstagramClient(); cl.delay_range = [2, 5]
        session_file = SESSIONS_DIR / f"{task_name}_instagram.json"
        if session_file.exists(): cl.load_settings(session_file)
        cl.login(insta_config["username"], insta_config["password"]); cl.dump_settings(session_file)
        insta_text = get_text_for_platform(text, "instagram", INSTAGRAM_CHAR_LIMIT, 30)
        post_result, temp_files_created_in_sync = None, []
        if len(media_paths) == 1:
            media_path = media_paths[0]
            if media_path.lower().endswith(('.mp4', '.mov')):
                thumbnail_path = asyncio.run(generate_video_thumbnail(media_path))
                if thumbnail_path: temp_files_created_in_sync.append(thumbnail_path)
                post_result = cl.clip_upload(path=media_path, caption=insta_text, thumbnail=thumbnail_path)
            else:
                resized_path = asyncio.run(resize_image(media_path, INSTAGRAM_IMAGE_SIZE, "instagram"))
                if resized_path: temp_files_created_in_sync.append(resized_path)
                post_result = cl.photo_upload(path=resized_path or media_path, caption=insta_text)
        else:
            photo_paths = [p for p in media_paths if not p.lower().endswith(('.mp4', '.mov'))]
            processed_photos = []
            for path in photo_paths:
                resized_path = asyncio.run(resize_image(path, INSTAGRAM_IMAGE_SIZE, "instagram"))
                processed_photos.append(resized_path or path)
                if resized_path: temp_files_created_in_sync.append(resized_path)
            if processed_photos: post_result = cl.album_upload(paths=processed_photos, caption=insta_text)
        return (post_result.pk if post_result else None, temp_files_created_in_sync)
    try:
        post_id, temp_files_to_clean = await asyncio.to_thread(_post)
        if post_id:
            await save_social_post_id(task_name, "instagram", post_id, text, len(media_paths))
            logging.info(f"[{task_name}] Успешно опубликовано в Instagram. Post ID: {post_id}")
    except Exception as e: await set_social_status(task_name, "instagram", f"Ошибка: {e}")
    finally: await asyncio.gather(*[remove_file_async(p) for p in temp_files_to_clean])

async def post_to_x(task_name: str, config: dict, text: str, media_paths: list[str]):
    try: import tweepy
    except ImportError: logging.warning(f"[{task_name}] tweepy не установлена."); return
    x_config = config.get("twitter", {})
    if not x_config.get("enabled"): return
    temp_files_to_clean = []
    def _post():
        auth_v1 = tweepy.OAuth1UserHandler(x_config["consumer_key"], x_config["consumer_secret"], x_config["access_token"], x_config["access_token_secret"])
        api_v1 = tweepy.API(auth_v1)
        client_v2 = tweepy.Client(consumer_key=x_config["consumer_key"], consumer_secret=x_config["consumer_secret"], access_token=x_config["access_token"], access_token_secret=x_config["access_token_secret"])
        tweet_text = get_text_for_platform(text, "twitter", TWITTER_CHAR_LIMIT, 3); media_ids, temp_files_created_in_sync = [], []
        for path in media_paths[:4]:
            if not path.lower().endswith(('.mp4', '.mov')):
                resized_path = asyncio.run(resize_image(path, TWITTER_IMAGE_SIZE, "twitter"))
                if resized_path:
                    temp_files_created_in_sync.append(resized_path)
                    media = api_v1.media_upload(filename=resized_path)
                    media_ids.append(media.media_id_string)
        response = client_v2.create_tweet(text=tweet_text, media_ids=media_ids if media_ids else None)
        return (response.data['id'] if response.data else None, temp_files_created_in_sync)
    try:
        tweet_id, temp_files_to_clean = await asyncio.to_thread(_post)
        if tweet_id:
            await save_social_post_id(task_name, "twitter", tweet_id, text, len(media_paths))
            logging.info(f"[{task_name}] Успешно опубликовано в X. Tweet ID: {tweet_id}")
    except Exception as e: await set_social_status(task_name, "twitter", f"Ошибка: {e}")
    finally: await asyncio.gather(*[remove_file_async(p) for p in temp_files_to_clean])

async def process_text_logic(original_text, task_config, client):
    if not original_text: return ""
    task_name = task_config.get('task_name', 'UnknownTask'); processed_text = original_text
    replace_rules = task_config.get('replace_links')
    if replace_rules:
        for item in replace_rules:
            if 'source' in item and 'target' in item:
                source_domain = re.sub(r'^https?://', '', item['source']).strip('/')
                target_link = item['target'].rstrip('/')
                pattern = re.compile(rf'(https?://)?{re.escape(source_domain)}(/\S*)?')
                processed_text = pattern.sub(rf'{target_link}\2', processed_text)
    translation_config = task_config.get('translation', {})
    if translation_config.get('enabled') and translation_config.get('target_lang'):
        translated = await azure_translate(processed_text, translation_config['target_lang'])
        if translated: processed_text = translated
    ai_conf = task_config.get("ai_config", {})
    if ai_conf.get("enabled"):
        rewritten_text = await rewrite_text_ai(processed_text, ai_conf)
        if rewritten_text is not None: processed_text = rewritten_text
    allowed_prefixes = set()
    replace_links_list = task_config.get('replace_links') or []
    for item in replace_links_list:
       if 'target' in item: allowed_prefixes.add(item['target'].rstrip('/')) 
    append_link = task_config.get('append_link')
    if append_link: allowed_prefixes.add(append_link)
    referral_links_list = task_config.get('referral_links') or []
    allowed_prefixes.update(referral_links_list)
    def link_cleaner(match):
        url = match.group(0)
        if any(url.startswith(prefix) for prefix in allowed_prefixes): return url
        return ""
    link_pattern = r'https?://\S+|t\.me/\S+|@\w+'
    processed_text = re.sub(link_pattern, link_cleaner, processed_text)
    text_to_append = []
    if append_link: text_to_append.append(append_link)
    if referral_links_list:
        try:
            history = await client.get_chat_history(task_config['target_channel'], limit=1)
            if history: link_index = history[0].id % len(referral_links_list); text_to_append.append(referral_links_list[link_index])
        except Exception as e: logging.error(f"[{task_name}] Не удалось получить историю для ротации ссылок: {e}")
    hashtags_list = task_config.get('hashtags') or []
    if hashtags_list: text_to_append.append(" ".join(hashtags_list))
    if text_to_append: processed_text = processed_text.strip() + "\n\n" + "\n\n".join(text_to_append)
    return processed_text.strip()

class AdminFilter(BaseFilter):
    async def __call__(self, message: Union[Message, CallbackQuery]) -> bool:
        return message.from_user.id in ADMIN_IDS

# --- ИСПРАВЛЕННАЯ ФУНКЦИЯ ---
async def send_with_retry(client: Client, media_type: str, chat_id: int, text: str, media, task_name: str = "Unknown"):
    for attempt in range(MAX_RETRY_ATTEMPTS):
        try:
            if media_type == 'album' and media:
                await client.send_media_group(chat_id, media=media)
            elif media_type == 'photo' and media:
                await client.send_photo(chat_id, photo=media, caption=text)
            elif media_type == 'video' and media:
                await client.send_video(chat_id, video=media, caption=text)
            else:
                await client.send_message(chat_id, text)
            return True # Успешная отправка
        except errors.FloodWait as e:
            logging.warning(f"[{task_name}] FloodWait в чате {chat_id}. Пауза {e.x + 2} сек.")
            await asyncio.sleep(e.x + 2)
        except (errors.UserNotParticipant, errors.ChatAdminRequired) as e:
            logging.error(f"[{task_name}] Ошибка доступа к чату {chat_id}: {e}. Вероятно, бан.")
            # Здесь можно добавить уведомление администратору, если нужно
            return False # Прекращаем попытки для этого чата
        except Exception as e:
            logging.error(f"[{task_name}] Неизвестная ошибка при отправке в чат {chat_id} (попытка {attempt + 1}): {e}", exc_info=True)
            if attempt < MAX_RETRY_ATTEMPTS - 1:
                await asyncio.sleep(5) # Пауза перед следующей попыткой
            else:
                logging.error(f"[{task_name}] Не удалось отправить сообщение в {chat_id} после {MAX_RETRY_ATTEMPTS} попыток.")
                return False # Все попытки исчерпаны
    return False # Явный возврат в случае, если цикл завершился
