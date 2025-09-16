# main.py

import asyncio
import logging
import os
import json
import uuid
import re
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Union

# --- Сторонние библиотеки ---
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.base import JobLookupError
from pyrogram import Client, errors, filters
from pyrogram.types import Message as PyrogramMessage, InputMediaPhoto

# --- Локальные импорты ---
import generative_channels
import advertising_tasks
import channel_management_tasks
from utils import *

# --- НАСТРОЙКА И ИНИЦИАЛИЗАЦИЯ ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
global_scheduler = AsyncIOScheduler(timezone="UTC")
main_router = Router()
dp.include_router(main_router)

MAILING_LOCK = asyncio.Lock()
MAILINGS_FILE = SCRIPT_DIR / "mailings.json"
MEDIA_GROUPS = {}

# --- ЛОГИКА PYROGRAM (только для forwarding) ---
async def process_single_message(client: Client, message: PyrogramMessage, task_config: dict):
    task_name = task_config['task_name']
    if message.document or message.poll or message.location or message.audio: return
    original_text = message.text or message.caption or ""
    processed_text = await process_text_logic(original_text, task_config, client)

    path_to_process = None
    all_temp_files = []

    try:
        media_paths_for_socials = []
        if message.photo:
            path_to_process = await message.download(in_memory=False, file_name=str(TEMP_DIR / f"{uuid.uuid4()}.jpg"))
            all_temp_files.append(path_to_process)

            rw_config = task_config.get('remove_watermark', {})
            if rw_config.get('enabled'):
                cleaned_path = await remove_external_watermark_async(path_to_process, task_name, rw_config)
                if cleaned_path:
                    all_temp_files.append(cleaned_path); path_to_process = cleaned_path

            watermarked_path = await apply_watermark_async(path_to_process, task_config.get('watermark_file'))
            if watermarked_path:
                 all_temp_files.append(watermarked_path)

            final_path_to_send = watermarked_path or path_to_process
            media_paths_for_socials.append(final_path_to_send)
            await send_with_retry(client.send_photo, task_config['target_channel'], photo=final_path_to_send, caption=processed_text, task_name=task_name)

        elif message.video:
            media_path = await message.download(in_memory=False, file_name=str(TEMP_DIR / f"{uuid.uuid4()}.mp4"))
            all_temp_files.append(media_path)
            media_paths_for_socials.append(media_path)
            await send_with_retry(client.send_video, task_config['target_channel'], video=media_path, caption=processed_text, task_name=task_name)
        elif message.animation:
            media_path = await message.download(in_memory=False, file_name=str(TEMP_DIR / f"{uuid.uuid4()}.mp4"))
            all_temp_files.append(media_path)
            media_paths_for_socials.append(media_path)
            await send_with_retry(client.send_animation, task_config['target_channel'], animation=media_path, caption=processed_text, task_name=task_name)
        elif processed_text:
            await send_with_retry(client.send_message, task_config['target_channel'], processed_text, task_name=task_name)

        if media_paths_for_socials:
             await post_to_instagram(task_name, task_config, processed_text, media_paths_for_socials)
             await post_to_x(task_name, task_config, processed_text, media_paths_for_socials)

        stats_lock = await get_task_lock(f"{task_name}_stats"); stats_file = TASKS_DIR / f"{task_name}_stats.json"
        async with stats_lock:
            stats_data = await read_json_file(stats_file) or {}; today = str(datetime.now().date())
            stats_data.setdefault(today, {"posts": 0})["posts"] += 1; await write_json_file(stats_file, stats_data)
    finally:
        await asyncio.gather(*[remove_file_async(p) for p in all_temp_files if p])

async def process_media_group(client: Client, messages: list[PyrogramMessage], task_config: dict):
    task_name = task_config['task_name']
    caption_message = next((m for m in messages if m.caption), messages[0])
    original_text = caption_message.caption or ""
    processed_text = await process_text_logic(original_text, task_config, client)
    media_to_send, all_temp_files = [], []
    media_paths_for_socials = []
    try:
        download_tasks = [m.download(in_memory=False, file_name=str(TEMP_DIR / f"{uuid.uuid4()}.jpg")) for m in messages if m.photo]
        downloaded_paths = await asyncio.gather(*download_tasks)
        all_temp_files.extend(downloaded_paths)
        paths_to_process = list(downloaded_paths)
        rw_config = task_config.get('remove_watermark', {})
        if rw_config.get('enabled'):
            cleaned_paths_tasks = [remove_external_watermark_async(path, task_name, rw_config) for path in downloaded_paths]
            cleaned_paths_results = await asyncio.gather(*cleaned_paths_tasks)
            temp_cleaned_paths = [p for p in cleaned_paths_results if p]
            if len(temp_cleaned_paths) == len(paths_to_process):
                paths_to_process = temp_cleaned_paths
            all_temp_files.extend(temp_cleaned_paths)
        watermark_tasks = [apply_watermark_async(path, task_config.get('watermark_file')) for path in paths_to_process]
        watermarked_paths = await asyncio.gather(*watermark_tasks)
        temp_watermarked_paths = [p for p in watermarked_paths if p]
        all_temp_files.extend(temp_watermarked_paths)
        for i, original_path in enumerate(paths_to_process):
            final_path = watermarked_paths[i] or original_path
            media_paths_for_socials.append(final_path)
            caption = processed_text if i == 0 else ""
            media_to_send.append(InputMediaPhoto(media=final_path, caption=caption))
        if media_to_send:
            await send_with_retry(client.send_media_group, task_config['target_channel'], media=media_to_send, task_name=task_name)
            await post_to_instagram(task_name, task_config, processed_text, media_paths_for_socials)
            await post_to_x(task_name, task_config, processed_text, media_paths_for_socials)
            stats_lock = await get_task_lock(f"{task_name}_stats"); stats_file = TASKS_DIR / f"{task_name}_stats.json"
            async with stats_lock:
                stats_data = await read_json_file(stats_file) or {}; today = str(datetime.now().date())
                stats_data.setdefault(today, {"posts": 0})["posts"] += 1; await write_json_file(stats_file, stats_data)
    finally:
        await asyncio.gather(*[remove_file_async(p) for p in all_temp_files if p])

async def poll_source_channels(client: Client, config: dict, task_name: str):
    logging.info(f"[{task_name}] Начинаю опрос каналов...")
    state = await read_task_state(task_name); processed_media_groups_in_run = set()
    for channel_id in config.get('source_channels', []):
        str_channel_id = str(channel_id); last_known_id = state.get(str_channel_id, 0); new_messages = []
        try:
            if last_known_id == 0:
                logging.info(f"[{task_name}] Первый запуск для канала {channel_id}. Устанавливаю начальный ID.")
                async for last_msg in client.get_chat_history(channel_id, limit=1):
                    if last_msg: state[str_channel_id] = last_msg.id
                await write_task_state(task_name, state); continue
            async for message in client.get_chat_history(channel_id, limit=50):
                if message.id <= last_known_id: break
                new_messages.append(message)
            if not new_messages: continue
            new_messages.reverse(); max_id_in_run = last_known_id
            for message in new_messages:
                try:
                    if message.media_group_id:
                        if message.media_group_id in processed_media_groups_in_run: max_id_in_run = max(max_id_in_run, message.id); continue
                        media_group_messages = await client.get_media_group(channel_id, message.id)
                        await process_media_group(client, media_group_messages, config)
                        last_message_in_group_id = max(m.id for m in media_group_messages)
                        max_id_in_run = max(max_id_in_run, last_message_in_group_id); processed_media_groups_in_run.add(message.media_group_id)
                    else:
                        await process_single_message(client, message, config)
                        max_id_in_run = max(max_id_in_run, message.id)
                except Exception as e:
                    logging.error(f"[{task_name}] Ошибка при обработке сообщения {message.id}: {e}", exc_info=True)
                    max_id_in_run = max(max_id_in_run, message.id)
            if max_id_in_run > last_known_id:
                state[str_channel_id] = max_id_in_run; await write_task_state(task_name, state)
        except errors.FloodWait as e: logging.warning(f"[{task_name}] FloodWait при опросе {channel_id}. Пауза {e.x} сек."); await asyncio.sleep(e.x)
        except Exception as e: logging.error(f"[{task_name}] Критическая ошибка при опросе {channel_id}: {e}", exc_info=True)

# --- Воркеры и управление задачами ---
async def run_forwarding_worker(task_name, config, bot):
    pyrogram_client, worker_scheduler = None, AsyncIOScheduler()
    try:
        pyrogram_client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], proxy=config.get('proxy'), workdir=str(SESSIONS_DIR))
        @pyrogram_client.on_message(filters.me & filters.text & filters.regex(f"\\[{task_name}\\] Self-test message."))
        async def message_handler(_, message: PyrogramMessage): logging.info(f"[{task_name}] ✅ Тестовое сообщение получено!"); await message.delete()
        await pyrogram_client.start()
        await check_channel_access(pyrogram_client, config, bot, ADMIN_IDS[0])
        worker_scheduler.add_job(poll_source_channels, 'interval', minutes=POLLING_INTERVAL_MINUTES, args=[pyrogram_client, config, task_name], next_run_time=datetime.now())
        worker_scheduler.start()
        ACTIVE_CLIENTS[task_name] = pyrogram_client
        logging.info(f"Воркер '{task_name}' запущен (опрос каждые {POLLING_INTERVAL_MINUTES} мин).")
        await bot.send_message(ADMIN_IDS[0], f"✅ Воркер <b>{task_name}</b> запущен в режиме опроса.")
        await asyncio.Event().wait()
    except (errors.AuthKeyUnregistered, errors.UserDeactivated, errors.AuthRestart) as e:
        await set_task_error(task_name, f"Критическая ошибка авторизации: {e.__class__.__name__}. Сессия недействительна.", bot)
    except asyncio.CancelledError:
        logging.info(f"Воркер {task_name} отменен.")
    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в воркере: {e}", exc_info=True)
        await set_task_error(task_name, f"Критическая ошибка: {e}", bot)
    finally:
        if worker_scheduler.running: worker_scheduler.shutdown()
        if pyrogram_client and pyrogram_client.is_connected: await pyrogram_client.stop()
        if task_name in ACTIVE_CLIENTS: ACTIVE_CLIENTS.pop(task_name)
        logging.info(f"Воркер {task_name} завершил работу.")

async def start_task(task_name):
    if task_name in ACTIVE_TASKS:
        logging.warning(f"Попытка запустить уже активную задачу {task_name}")
        return
    task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if not config: 
            logging.error(f"Не найдена конфигурация для задачи {task_name}")
            return
        
        task_type = config.get("task_type", "forwarding")
        
        # Обновленный словарь с правильными вызовами воркеров
        worker_map = {
            "forwarding": run_forwarding_worker,
            "generative": generative_channels.run_generative_worker,
            "advertising": advertising_tasks.run_advertising_worker,
            "channel_management": channel_management_tasks.run_channel_management_worker
        }
        
        if task_type in worker_map:
            worker_coro = worker_map[task_type](task_name, config, bot)
        else:
            logging.error(f"Неизвестный тип задачи '{task_type}' для {task_name}")
            return
            
        config['status'] = 'active'
        config['last_error'] = None
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        
    task = asyncio.create_task(worker_coro)
    ACTIVE_TASKS[task_name] = task
    task.add_done_callback(lambda t: ACTIVE_TASKS.pop(task_name, None))


# --- ЛОГИКА РАССЫЛОК ---
async def send_mailing_job(mailing_id, direct_data=None):
    mailing_data = direct_data
    if not mailing_data:
        mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}
        mailing_data = mailings.get(mailing_id)
    if not mailing_data:
        if not direct_data:
            try: global_scheduler.remove_job(mailing_id)
            except JobLookupError: pass
        return
    is_global = mailing_id.startswith("global_")
    target_tasks = []
    if is_global:
        task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
        for task_file in task_files:
            task_config = await read_json_file(task_file)
            if task_config and task_config.get("task_type", "forwarding") == "forwarding":
                target_tasks.append(task_config)
    else:
        task_name = mailing_data['task_name']
        task_config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if task_config: target_tasks.append(task_config)
    if not target_tasks:
        return
    for task_config in target_tasks:
        task_name = task_config['task_name']
        target_channel_id = task_config.get('target_channel')
        if not target_channel_id:
            continue
        sender_client, is_temp_client = None, False
        if task_config.get('status') == 'active' and task_name in ACTIVE_CLIENTS and ACTIVE_CLIENTS[task_name].is_connected:
            sender_client = ACTIVE_CLIENTS[task_name]
        else:
            sender_client = Client(task_name, api_id=task_config['api_id'], api_hash=task_config['api_hash'], workdir=str(SESSIONS_DIR), proxy=task_config.get('proxy'))
            is_temp_client = True
        temp_files_for_this_task = []
        try:
            if is_temp_client: await sender_client.start()
            if 'media_group_ids' in mailing_data:
                media_to_send = []
                messages_from_storage = await sender_client.get_messages(STORAGE_CHANNEL_ID, mailing_data['media_group_ids'])
                download_tasks = [m.download(in_memory=False, file_name=str(TEMP_DIR / f"mailing_{uuid.uuid4().hex}")) for m in messages_from_storage if m.photo]
                downloaded_paths = await asyncio.gather(*download_tasks)
                temp_files_for_this_task.extend(downloaded_paths)
                for i, path in enumerate(downloaded_paths):
                    media_to_send.append(InputMediaPhoto(media=path, caption=mailing_data.get('caption', '') if i == 0 else ""))
                if media_to_send:
                    success = await send_with_retry(sender_client.send_media_group, target_channel_id, media=media_to_send, task_name=f"Mailing-{task_name}")
                    if not success:
                         await bot.send_message(ADMIN_IDS[0], f"❗️Не удалось отправить альбом (рассылка) для задачи <b>{task_name}</b> в канал <code>{target_channel_id}</code>.")
            else:
                success = await send_with_retry(
                    sender_client.copy_message,
                    chat_id=target_channel_id,
                    from_chat_id=STORAGE_CHANNEL_ID,
                    message_id=mailing_data['storage_message_id'],
                    task_name=f"Mailing-{task_name}"
                )
                if not success:
                    await bot.send_message(ADMIN_IDS[0], f"❗️Не удалось отправить сообщение (рассылка) для задачи <b>{task_name}</b> в канал <code>{target_channel_id}</code>.")
        except Exception as e:
            await bot.send_message(ADMIN_IDS[0], f"❗️Критическая ошибка рассылки для <b>{task_name}</b>: {e}")
        finally:
            if is_temp_client and sender_client and sender_client.is_connected: await sender_client.stop()
            await asyncio.gather(*[remove_file_async(p) for p in temp_files_for_this_task])
    if not direct_data and mailing_data.get('schedule_type') == 'one-time':
        mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}
        if mailing_id in mailings: mailings.pop(mailing_id)
        await write_json_file(MAILINGS_FILE, mailings, MAILING_LOCK)

async def schedule_mailing(mailing_data):
    mailing_id = mailing_data['id']
    if mailing_data['schedule_type'] == 'one-time':
        global_scheduler.add_job(send_mailing_job, 'date', run_date=datetime.fromisoformat(mailing_data['run_date']), id=mailing_id, args=[mailing_id, None], misfire_grace_time=3600)
    elif mailing_data['schedule_type'] == 'recurring':
        global_scheduler.add_job(send_mailing_job, 'interval', hours=mailing_data['interval_hours'], id=mailing_id, args=[mailing_id, None], misfire_grace_time=3600)

# --- ЛОГИКА СТАТИСТИКИ ---
async def get_social_stats(task_name: str) -> dict:
    lock = await get_task_lock(f"{task_name}_social"); file_path = SOCIAL_POSTS_DIR / f"{task_name}.json"
    posts = await read_json_file(file_path, lock) or []; insta_stats = {"posts": 0, "likes": 0, "comments": 0}; x_stats = {"posts": 0, "likes": 0, "comments": 0, "views": 0}
    for post in posts:
        if post['platform'] == 'instagram': insta_stats['posts'] += 1; insta_stats['likes'] += post.get('stats', {}).get('likes', 0); insta_stats['comments'] += post.get('stats', {}).get('comments', 0)
        elif post['platform'] == 'twitter': x_stats['posts'] += 1; x_stats['likes'] += post.get('stats', {}).get('likes', 0); x_stats['comments'] += post.get('stats', {}).get('comments', 0); x_stats['views'] += post.get('stats', {}).get('views', 0)
    return {"instagram": insta_stats, "twitter": x_stats}

async def update_social_stats_job():
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in task_files:
        task_name = task_file.stem; config = await read_json_file(task_file)
        if not config or config.get("task_type") not in ["forwarding", "generative"]:
            continue
        social_lock = await get_task_lock(f"{task_name}_social"); social_posts_file = SOCIAL_POSTS_DIR / f"{task_name}.json"
        all_social_posts = await read_json_file(social_posts_file, social_lock) or []; updated = False
        insta_config = config.get("instagram", {}); x_config = config.get("twitter", {})
        if insta_config.get("enabled"):
            try: from instagrapi import Client as InstagramClient
            except ImportError: insta_config["enabled"] = False; continue
            try:
                cl = InstagramClient(); cl.load_settings(SESSIONS_DIR / f"{task_name}_instagram.json")
                cl.login(insta_config['username'], insta_config['password'])
                for post in all_social_posts:
                    if post['platform'] == 'instagram': info = cl.media_info(post['post_id']).dict(); post['stats']['likes'] = info.get('like_count', 0); post['stats']['comments'] = info.get('comment_count', 0); updated = True
            except Exception as e: logging.warning(f"Не удалось обновить статистику Instagram для {task_name}: {e}")
        if x_config.get("enabled"):
            try: import tweepy
            except ImportError: x_config["enabled"] = False; continue
            try:
                client_v2 = tweepy.Client(consumer_key=x_config["consumer_key"], consumer_secret=x_config["consumer_secret"], access_token=x_config["access_token"], access_token_secret=x_config["access_token_secret"])
                for post in all_social_posts:
                    if post['platform'] == 'twitter': response = client_v2.get_tweet(post['post_id'], tweet_fields=["public_metrics"]); metrics = response.data.public_metrics; post['stats']['likes'] = metrics.get('like_count', 0); post['stats']['comments'] = metrics.get('reply_count', 0); post['stats']['views'] = metrics.get('impression_count', 0); updated = True
            except Exception as e: logging.warning(f"Не удалось обновить статистику X для {task_name}: {e}")
        if updated: await write_json_file(social_posts_file, all_social_posts, social_lock)
        await asyncio.sleep(10)

async def get_task_advanced_stats(task_name: str):
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config or config.get("task_type") not in ["forwarding", "generative", "channel_management"]:
        return {"error": "Для этого типа задачи статистика не собирается."}
    if config.get("task_type") == "channel_management":
        channels = config.get("management_config", {}).get("target_channels", [])
        if not channels: return {"error": "В задаче нет целевых каналов."}
        target_channel_id_for_stats = channels[0]['id']
    else:
        target_channel_id_for_stats = config['target_channel']
    stats = {"last_post_views": "н/д", "total_views": 0, "reactions_week": 0, "reactions_day": 0, "reactions_total": 0, "subscribers": "н/д", "subscribers_day": "н/д", "subscribers_week": "н/д"}
    client_to_use, is_temp_client = None, False
    if task_name in ACTIVE_CLIENTS and ACTIVE_CLIENTS[task_name].is_connected: client_to_use = ACTIVE_CLIENTS[task_name]
    else: client_to_use = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy')); is_temp_client = True
    try:
        if is_temp_client: await client_to_use.start()
        chat = await client_to_use.get_chat(target_channel_id_for_stats)
        stats['subscribers'] = chat.members_count; now = datetime.now().astimezone(); one_day_ago, one_week_ago = now - timedelta(days=1), now - timedelta(days=7)
        history_iter = client_to_use.get_chat_history(target_channel_id_for_stats, limit=200)
        is_first = True
        async for message in history_iter:
            if not hasattr(message, 'views') or not message.views: continue
            if is_first: stats["last_post_views"] = message.views; is_first = False
            stats["total_views"] += message.views
            if message.reactions and message.reactions.reactions:
                message_reactions = sum(r.count for r in message.reactions.reactions)
                stats["reactions_total"] += message_reactions
                if message.date > one_week_ago: stats["reactions_week"] += message_reactions
                if message.date > one_day_ago: stats["reactions_day"] += message_reactions
        stats_lock = await get_task_lock(f"{task_name}_stats"); full_stats_data = await read_json_file(TASKS_DIR / f"{task_name}_stats.json", stats_lock) or {}
        yesterday_str, week_ago_str = str((now.date() - timedelta(days=1))), str((now.date() - timedelta(days=7)))
        if yesterday_str in full_stats_data and full_stats_data[yesterday_str].get("subscribers") is not None: stats['subscribers_day'] = stats['subscribers'] - full_stats_data[yesterday_str]["subscribers"]
        if week_ago_str in full_stats_data and full_stats_data[week_ago_str].get("subscribers") is not None: stats['subscribers_week'] = stats['subscribers'] - full_stats_data[week_ago_str]["subscribers"]
        social_stats = await get_social_stats(task_name); stats["social_stats"] = social_stats
        return stats
    except Exception as e: return {"error": str(e)}
    finally:
        if is_temp_client and client_to_use.is_connected: await client_to_use.stop()

# --- ПЛАНИРОВЩИК ---
async def daily_forwarding_report_job():
    report_lines = [f"📊 <b>Ежедневный отчет (Форвардинг) за {datetime.now().strftime('%Y-%m-%d')}</b>\n"]
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    found_any = False
    for task_file in task_files:
        config = await read_json_file(task_file)
        if not config or config.get("task_type", "forwarding") != "forwarding":
            continue
        task_name = task_file.stem; stats_file = TASKS_DIR / f"{task_name}_stats.json"
        stats_data = await read_json_file(stats_file)
        today_str = str(datetime.now().date())
        posts_today = stats_data.get(today_str, {}).get('posts', 0) if stats_data else 0
        if posts_today > 0:
            report_lines.append(f"<b>{task_name}</b>: {posts_today} постов."); found_any = True
    if found_any and len(report_lines) > 1:
        try: await bot.send_message(REPORTING_CHANNEL_ID, "\n".join(report_lines))
        except Exception as e: logging.error(f"Не удалось отправить отчет по форвардингу: {e}")

async def daily_subscriber_check():
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in task_files:
        task_name = task_file.stem; config = await read_json_file(task_file)
        if not config: continue
        target_id_for_check = None
        task_type = config.get("task_type", "forwarding")
        if task_type in ["forwarding", "generative"] and 'target_channel' in config:
            target_id_for_check = config['target_channel']
        elif task_type == "channel_management":
            channels = config.get("management_config", {}).get("target_channels", [])
            if channels: target_id_for_check = channels[0]['id']
        if not target_id_for_check: continue
        temp_client = None
        try:
            temp_client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
            await temp_client.start()
            subscribers_count = "н/д"
            try:
                chat = await temp_client.get_chat(target_id_for_check)
                subscribers_count = chat.members_count
            except errors.FloodWait as e: await asyncio.sleep(e.x)
            except Exception as e: logging.error(f"[DailyCheck-{task_name}] Ошибка получения чата: {e}")
            if subscribers_count != "н/д":
                stats_lock = await get_task_lock(f"{task_name}_stats")
                async with stats_lock:
                    stats_file = TASKS_DIR / f"{task_name}_stats.json"
                    stats_data = await read_json_file(stats_file) or {}
                    today_str = str(datetime.now().date())
                    stats_data.setdefault(today_str, {})['subscribers'] = subscribers_count
                    await write_json_file(stats_file, stats_data)
            await asyncio.sleep(5)
        except Exception as e: logging.error(f"Не удалось проверить подписчиков для {task_name}: {e}", exc_info=True)
        finally:
            if temp_client and temp_client.is_connected: await temp_client.stop()

# --- FSM И ОБРАБОТЧИКИ ---
class TaskCreation(StatesGroup):
    name, api_id, api_hash, source_ids, target_id, target_link, proxy, translation, \
    select_ai_provider, get_ai_config_details, \
    watermark, \
    remove_watermark_toggle, remove_watermark_mode, \
    final_details, instagram_toggle, instagram_creds, twitter_toggle, twitter_creds = [State() for _ in range(18)]

class TaskEditing(StatesGroup):
    select_field = State()
    get_new_value = State()
    get_instagram_creds, get_twitter_creds, select_ai_provider, get_ai_config_details = [State() for _ in range(4)]
    get_gen_posts_per_day, get_gen_text_provider, get_gen_wants_images, get_gen_image_provider = [State() for _ in range(4)]

class MailingCreation(StatesGroup):
    select_task, get_content, select_schedule_type, get_schedule_details, confirm_album = [State() for _ in range(5)]
class GlobalMailing(StatesGroup):
    get_content, select_schedule_type, get_schedule_details, confirm_album = [State() for _ in range(4)]
class SocialMediaPosting(StatesGroup):
    get_content, confirm_album = State(), State()


# --- КЛАВИАТУРЫ ---
async def manage_tasks_keyboard():
    buttons = []
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in sorted(task_files):
        try:
            config = await read_json_file(task_file)
            if config and config.get("task_type", "forwarding") == "forwarding":
                 buttons.append([InlineKeyboardButton(text=f"📋 {task_file.stem}", callback_data=f"task_view:{task_file.stem}")])
        except Exception as e: logging.error(f"Ошибка чтения файла задачи {task_file.name}: {e}")
    buttons.append([InlineKeyboardButton(text="➕ Добавить новую задачу (Форвардинг)", callback_data="task_add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# ЗАМЕНИТЕ ЭТУ ФУНКЦИЮ в main.py

def task_control_keyboard(task_name, config):
    is_active = task_name in ACTIVE_TASKS
    status_icon, status_text = ("✅", "active") if is_active else ("⏸️", "inactive")
    start_stop_text, start_stop_action = ("⏸️ Остановить", "task_stop") if is_active else ("▶️ Запустить", "task_start")
    task_type = config.get("task_type", "forwarding")
    buttons = [
        [InlineKeyboardButton(text=f"Статус: {status_text} {status_icon}", callback_data="noop")],
        [InlineKeyboardButton(text=start_stop_text, callback_data=f"{start_stop_action}:{task_name}"),
         InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"task_edit:{task_name}")]
    ]
    if task_type in ['forwarding', 'generative', 'channel_management']:
        stats_buttons = [InlineKeyboardButton(text="🔬 Расш. статистика", callback_data=f"task_adv_stats:{task_name}")]
        if task_type == 'forwarding':
            stats_buttons.append(InlineKeyboardButton(text="📜 Посмотреть источники", callback_data=f"task_view_sources:{task_name}"))
        buttons.append(stats_buttons)
        if task_type == 'generative':
            buttons.append([InlineKeyboardButton(text="🚀 Сгенерировать пост сейчас", callback_data=f"gen_post_now:{task_name}")])
        if task_type == 'channel_management':
            buttons.append([InlineKeyboardButton(text="📝 Управление контентом", callback_data=f"channel_manage_content:{task_name}")])
        if task_type != 'channel_management':
             buttons.append([InlineKeyboardButton(text="📲 Отправка в SOCMEDIA", callback_data=f"task_social_post:{task_name}")])
    elif task_type == 'advertising':
        buttons.extend([
            # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
            # Добавлена кнопка "Воронка ответов"
            [InlineKeyboardButton(text="🔀 Воронка ответов", callback_data=f"adv_funnel_manage:{task_name}")],
            [InlineKeyboardButton(text="📣 Рассылка сейчас", callback_data=f"adv_send_now:{task_name}")],
            [InlineKeyboardButton(text="🧪 Тест сейчас", callback_data=f"adv_test_now:{task_name}")],
            [InlineKeyboardButton(text="👤 Показать имя аккаунта", callback_data=f"adv_show_account:{task_name}")]
        ])
    back_callback = "manage_tasks"
    if task_type == 'generative': back_callback = 'manage_gen_tasks'
    elif task_type == 'advertising': back_callback = 'manage_adv_tasks'
    elif task_type == 'channel_management': back_callback = 'manage_channel_tasks'
    extend_buttons = []
    
    target_link = None
    if task_type in ['forwarding', 'generative']:
        target_link = config.get('target_channel_link')
    elif task_type == 'channel_management':
        channels = config.get("management_config", {}).get("target_channels", [])
        if channels: target_link = channels[0].get('link')

    if target_link:
        if target_link.startswith('@'):
            url = f"https://t.me/{target_link[1:]}"
        elif target_link.startswith('t.me/'):
            url = f"https://{target_link}"
        else:
            url = target_link
        extend_buttons.append(InlineKeyboardButton(text="🔗 Перейти в целевой канал", url=url))

    extend_buttons.append(InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"task_delete:{task_name}"))
    extend_buttons.append(InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=back_callback))
    buttons.append(extend_buttons)
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def task_edit_keyboard(task_name, config):
    task_type = config.get("task_type", "forwarding")
    buttons = []
    if task_type == "forwarding":
        rw_config = config.get('remove_watermark', {})
        rw_status = f"Вкл ({rw_config.get('mode', 'N/A')})" if rw_config.get('enabled') else "Выкл"
        fields = {'source_channels': 'Каналы-источники', 'target_channel': 'Целевой канал', 'translation': 'Язык перевода', 'ai_config': '🤖 AI-рерайтинг', 'remove_watermark': f'💧 Удаление Watermark: {rw_status}', 'hashtags': 'Хэштеги', 'referral_links': 'Ссылки (ротация)', 'append_link': '🔗 Добавить ссылку в конец', 'replace_links': '🔁 Заменить ссылки'}
        for field, desc in fields.items(): buttons.append([InlineKeyboardButton(text=desc, callback_data=f"edit_field:{task_name}:{field}")])
    elif task_type == "generative":
        gen_config = config.get('generative_config', {})
        text_provider = gen_config.get('text_provider', 'N/A'); image_provider = gen_config.get('image_provider', 'none'); posts_per_day = gen_config.get('posts_per_day', 'N/A')
        fields = {'gen_posts_per_day': f'Постов в день: {posts_per_day}', 'gen_text_provider': f'🤖 AI для текста: {text_provider.title()}', 'gen_image_provider': f'🎨 AI для картинок: {image_provider.title()}'}
        for field, desc in fields.items(): buttons.append([InlineKeyboardButton(text=desc, callback_data=f"edit_gen_field:{task_name}:{field}")])
    elif task_type == "advertising":
        adv_config = config.get('advertising_config', {})
        sends_per_hour = adv_config.get('sends_per_hour', 'N/A')
        num_groups = len(adv_config.get('target_group_ids', []))
        groups_text = f"{num_groups} групп" if num_groups > 0 else "Все группы"
        fields = {
            'sends_per_hour': f'Рассылок в час: {sends_per_hour}',
            'target_groups': f'Целевые группы: {groups_text}',
            'message_variants': 'Варианты сообщений (перезаписать)'
        }
        for field, desc in fields.items():
            buttons.append([InlineKeyboardButton(text=desc, callback_data=f"edit_adv_field:{task_name}:{field}")])
    if task_type != "advertising" and task_type != "channel_management":
        insta_enabled = config.get('instagram', {}).get('enabled', False); x_enabled = config.get('twitter', {}).get('enabled', False)
        buttons.extend([
            [InlineKeyboardButton(text=f"{'✅' if insta_enabled else '❌'} Instagram", callback_data=f"edit_toggle_social:{task_name}:instagram")],
            [InlineKeyboardButton(text="Изменить креды Instagram", callback_data=f"edit_creds_social:{task_name}:instagram")],
            [InlineKeyboardButton(text=f"{'✅' if x_enabled else '❌'} X (Twitter)", callback_data=f"edit_toggle_social:{task_name}:twitter")],
            [InlineKeyboardButton(text="Изменить креды X (Twitter)", callback_data=f"edit_creds_social:{task_name}:twitter")]
        ])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def mailing_view_keyboard(mailing_id): return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗑️ Удалить рассылку", callback_data=f"mailing_delete:{mailing_id}")], [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data="manage_mailings")]])


# --- ОСНОВНЫЕ ОБРАБОТЧИКИ КОМАНД И КОЛБЭКОВ ---
@main_router.message(CommandStart(), AdminFilter())
async def cmd_start(message: Message, state: FSMContext): await state.clear(); await message.answer("Добро пожаловать!", reply_markup=main_menu_keyboard())

@main_router.callback_query(F.data == "main_menu", AdminFilter())
async def cb_main_menu(callback: CallbackQuery, state: FSMContext): await state.clear(); await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard()); await safe_answer_callback(callback)

@main_router.callback_query(F.data == "manage_tasks", AdminFilter())
async def cb_manage_tasks(callback: CallbackQuery, state: FSMContext):
    await state.clear(); await callback.message.edit_text("Управление задачами (Форвардинг):", reply_markup=await manage_tasks_keyboard()); await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("task_view:"), AdminFilter())
async def cb_task_view(callback: CallbackQuery, state: FSMContext):
    await state.clear(); task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config: await safe_answer_callback(callback, "Ошибка: не удалось найти конфигурацию задачи.", show_alert=True); await cb_main_menu(callback, state); return
    task_type = config.get("task_type", "forwarding")
    type_map = {"forwarding": "Форвардинг", "generative": "Генеративный", "advertising": "Рекламная задача", "channel_management": "Ведение канала"}
    text = f"Управление задачей: <b>{task_name}</b> (Тип: {type_map.get(task_type, 'Неизвестный')})\n\n"
    if task_type == "forwarding": text += f"Режим работы: <b>Опрос каналов</b> (раз в {POLLING_INTERVAL_MINUTES} мин)\n\n"
    if task_type == "channel_management":
        channels = config.get("management_config", {}).get("target_channels", [])
        text += "<b>Целевые каналы:</b>\n"
        for ch in channels: text += f" - <code>{ch['id']}</code> ({ch['lang']})\n"
        text += "\n"
    if config.get('last_error'): text += f"Последняя ошибка: <pre>{config['last_error']}</pre>\n"
    await callback.message.edit_text(text, reply_markup=task_control_keyboard(task_name, config)); await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("task_start:"), AdminFilter())
async def cb_task_start(callback: CallbackQuery):
    task_name = callback.data.split(":")[1]; await start_task(task_name); await safe_answer_callback(callback, f"Запускаю задачу {task_name}...")
    await asyncio.sleep(2.5); updated_config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if updated_config:
        try: await callback.message.edit_reply_markup(reply_markup=task_control_keyboard(task_name, updated_config))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e): logging.error(f"Ошибка при обновлении клавиатуры: {e}")

@main_router.callback_query(F.data.startswith("task_stop:"), AdminFilter())
async def cb_task_stop(callback: CallbackQuery):
    task_name = callback.data.split(":")[1]
    await stop_worker(task_name, bot)
    await safe_answer_callback(callback, f"Останавливаю задачу {task_name}...")
    await asyncio.sleep(1.5); config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if config:
        try: await callback.message.edit_reply_markup(reply_markup=task_control_keyboard(task_name, config))
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e): logging.error(f"Ошибка при обновлении клавиатуры: {e}")

@main_router.callback_query(F.data.startswith("task_delete:"), AdminFilter())
async def cb_task_delete(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await stop_worker(task_name, bot, "Задача удалена.")
    for path in [TASKS_DIR / f"{task_name}.json", TASKS_DIR / f"{task_name}_stats.json", SESSIONS_DIR / f"{task_name}.session", SESSIONS_DIR / f"{task_name}_instagram.json", STATE_DIR / f"{task_name}_state.json", SOCIAL_POSTS_DIR / f"{task_name}.json"]:
        if os.path.exists(path):
            try: os.remove(path)
            except OSError as e: logging.error(f"Не удалось удалить файл {path}: {e}")
    await safe_answer_callback(callback, f"Задача {task_name} и все ее файлы удалены.", show_alert=True)
    await cb_main_menu(callback, state)

@main_router.callback_query(F.data.startswith("task_view_sources:"), AdminFilter())
async def cb_task_view_sources(callback: CallbackQuery):
    task_name = callback.data.split(":")[1]; await safe_answer_callback(callback); msg = await callback.message.edit_text(f"📜 Получаю названия каналов-источников для <b>{task_name}</b>...")
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config or not config.get('source_channels'):
        await msg.edit_text("У этой задачи нет каналов-источников.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]])); return
    client_to_use, is_temp_client = None, False
    if task_name in ACTIVE_CLIENTS and ACTIVE_CLIENTS[task_name].is_connected: client_to_use = ACTIVE_CLIENTS[task_name]
    else: client_to_use = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy')); is_temp_client = True
    try:
        if is_temp_client: await client_to_use.start()
        source_channels = config.get('source_channels', []); channel_lines = [f"<b>Источники для задачи {task_name}:</b>"]
        for channel_id in source_channels:
            try:
                chat = await client_to_use.get_chat(channel_id)
                channel_lines.append(f"• {chat.title} (<code>{channel_id}</code>)")
            except Exception as e: channel_lines.append(f"• <code>{channel_id}</code> (Ошибка доступа: {e.__class__.__name__})")
            await asyncio.sleep(0.5)
        await msg.edit_text("\n".join(channel_lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]]))
    except Exception as e: await msg.edit_text(f"❌ Произошла ошибка при получении названий: <pre>{e}</pre>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]]))
    finally:
        if is_temp_client and client_to_use.is_connected: await client_to_use.stop()

@main_router.callback_query(F.data.startswith("task_stats:"), AdminFilter())
async def cb_task_stats(callback: CallbackQuery):
    try: _, _, task_name = callback.data.split(":", 2)
    except ValueError: await safe_answer_callback(callback, "Ошибка в данных кнопки.", show_alert=True); return
    stats_data = await read_json_file(TASKS_DIR / f"{task_name}_stats.json")
    if not stats_data: await safe_answer_callback(callback, "Статистика постов для этой задачи пока отсутствует.", show_alert=True); return
    total_posts = sum(daily_stats.get("posts", 0) for daily_stats in stats_data.values()); title = f"📊 <b>Статистика постов для задачи {task_name}</b>"
    try: await bot.send_message(REPORTING_CHANNEL_ID, f"{title}\n\nВсего постов за все время: {total_posts}"); await safe_answer_callback(callback, "Отчет отправлен.", show_alert=True)
    except Exception as e: await safe_answer_callback(callback, f"Ошибка отправки отчета: {e}", show_alert=True)

@main_router.callback_query(F.data.startswith("task_adv_stats:"), AdminFilter())
async def cb_task_advanced_stats(callback: CallbackQuery):
    task_name = callback.data.split(":", 1)[1]; await safe_answer_callback(callback); msg = await callback.message.edit_text("🔬 Собираю расширенную статистику, это может занять до минуты...")
    stats = await get_task_advanced_stats(task_name)
    if "error" in stats: await msg.edit_text(f"Ошибка: <pre>{stats['error']}</pre>", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]])); return
    report = (f"🔬 <b>Расширенная статистика для {task_name}</b>\n\n"
        f"<b><u>Telegram:</u></b>\n"
        f"👥 Подписчиков: <b>{stats.get('subscribers', 'н/д')}</b>\n" f"📈 Прирост за сутки: <b>{stats.get('subscribers_day', 'н/д')}</b>\n" f"🚀 Прирост за неделю: <b>{stats.get('subscribers_week', 'н/д')}</b>\n"
        f"👀 Просмотров на последнем посту: <b>{stats['last_post_views']}</b>\n" f"👁 Всего просмотров (на посл. 200): <b>{stats['total_views']}</b>\n"
        f"❤️ Реакций за 24 часа: <b>{stats['reactions_day']}</b>\n" f"🔥 Реакций за неделю: <b>{stats['reactions_week']}</b>\n" f"💖 Всего реакций (на посл. 200): <b>{stats['reactions_total']}</b>\n")
    social_stats = stats.get("social_stats", {})
    if social_stats:
        insta_stats, x_stats = social_stats.get("instagram", {}), social_stats.get("twitter", {})
        report += f"\n<b><u>Instagram/Threads:</u></b>\n📝 Постов: <b>{insta_stats.get('posts', 0)}</b> | ❤️ Лайков: <b>{insta_stats.get('likes', 0)}</b> | 💬 Комментариев: <b>{insta_stats.get('comments', 0)}</b>\n"
        report += f"\n<b><u>X (Twitter):</u></b>\n📝 Постов: <b>{x_stats.get('posts', 0)}</b> | ❤️ Лайков: <b>{x_stats.get('likes', 0)}</b> | 💬 Комментариев: <b>{x_stats.get('comments', 0)}</b> | 👁 Просмотров: <b>{x_stats.get('views', 0)}</b>\n"
    await msg.edit_text(report, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]]))

# --- ОБРАБОТЧИКИ РЕДАКТИРОВАНИЯ ЗАДАЧИ ---
async def cb_task_edit(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config: await safe_answer_callback(callback, "Задача не найдена.", show_alert=True); return
    await state.update_data(task_name=task_name)
    task_type = config.get("task_type", "forwarding")
    if task_type == "channel_management":
        await callback.message.edit_text(f"Редактирование задачи <b>{task_name}</b>:", reply_markup=channel_management_tasks.channel_task_edit_keyboard(task_name, config))
    else:
        await callback.message.edit_text(f"Какое поле задачи <b>{task_name}</b> вы хотите изменить?", reply_markup=task_edit_keyboard(task_name, config))
    await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("edit_toggle_social:"), AdminFilter())
async def cb_edit_toggle_social(callback: CallbackQuery, state: FSMContext):
    _, task_name, platform = callback.data.split(":"); task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if platform not in config: config[platform] = {"enabled": False, "last_status": "Not configured"}
        is_enabled = config[platform].get("enabled", False)
        config[platform]["enabled"] = not is_enabled; await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await safe_answer_callback(callback, f"Постинг в {platform.title()} {'включен' if not is_enabled else 'отключен'}.")
    await callback.message.edit_reply_markup(reply_markup=task_edit_keyboard(task_name, config))

@main_router.callback_query(F.data.startswith("edit_creds_social:"), AdminFilter())
async def cb_edit_creds_social(callback: CallbackQuery, state: FSMContext):
    _, task_name, platform = callback.data.split(":"); await state.update_data(task_name=task_name)
    if platform == 'instagram': await state.set_state(TaskEditing.get_instagram_creds); await callback.message.edit_text("Введите логин и пароль для Instagram через пробел (например, `myuser mypass`).")
    elif platform == 'twitter': await state.set_state(TaskEditing.get_twitter_creds); await callback.message.edit_text("Пришлите данные для X (Twitter) в 4 строки:\n1. Consumer Key\n2. Consumer Secret\n3. Access Token\n4. Access Token Secret")
    await safe_answer_callback(callback)

@main_router.message(TaskEditing.get_instagram_creds, AdminFilter())
async def process_edit_instagram_creds(message: Message, state: FSMContext):
    data = await state.get_data(); task_name = data['task_name']
    try: username, password = message.text.split(' ', 1)
    except ValueError: await message.answer("Неверный формат. Введите логин и пароль через пробел."); return
    task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['instagram']['username'] = username.strip(); config['instagram']['password'] = password.strip(); config['instagram']['last_status'] = "Updated, not checked"; await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear(); await message.answer(f"Учетные данные Instagram для <b>{task_name}</b> обновлены.")
    await message.answer("Возврат в главное меню.", reply_markup=main_menu_keyboard())

@main_router.message(TaskEditing.get_twitter_creds, AdminFilter())
async def process_edit_twitter_creds(message: Message, state: FSMContext):
    data = await state.get_data(); task_name = data['task_name']
    try: consumer_key, consumer_secret, access_token, access_token_secret = message.text.split('\n', 3)
    except ValueError: await message.answer("Неверный формат. Введите 4 ключа, каждый с новой строки."); return
    task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['twitter']['consumer_key'] = consumer_key.strip(); config['twitter']['consumer_secret'] = consumer_secret.strip(); config['twitter']['access_token'] = access_token.strip(); config['twitter']['access_token_secret'] = access_token_secret.strip(); config['twitter']['last_status'] = "Updated, not checked"; await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear(); await message.answer(f"Учетные данные X (Twitter) для <b>{task_name}</b> обновлены.")
    await message.answer("Возврат в главное меню.", reply_markup=main_menu_keyboard())

@main_router.callback_query(F.data.startswith("edit_field:"), AdminFilter())
async def cb_edit_field(callback: CallbackQuery, state: FSMContext):
    _, task_name, field = callback.data.split(":"); await state.update_data(field_to_edit=field)
    if field == 'ai_config':
        await state.set_state(TaskEditing.select_ai_provider)
        providers_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="OpenAI (ChatGPT)", callback_data="aiconfig_provider:openai")], [InlineKeyboardButton(text="Anthropic (Claude)", callback_data="aiconfig_provider:claude")], [InlineKeyboardButton(text="Google (Gemini)", callback_data="aiconfig_provider:gemini")], [InlineKeyboardButton(text="Локальный AI (Llama, и т.д.)", callback_data="aiconfig_provider:local")], [InlineKeyboardButton(text="❌ Отключить рерайтинг", callback_data="aiconfig_provider:disable")], [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_edit:{task_name}")]])
        await callback.message.edit_text("Выберите провайдера для AI-рерайтинга:", reply_markup=providers_kb); return
    if field == 'remove_watermark':
        rw_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Включить (Режим ФОТО)", callback_data=f"edit_rw_mode:{task_name}:PHOTOS")], [InlineKeyboardButton(text="Включить (Режим ТАБЛИЦЫ)", callback_data=f"edit_rw_mode:{task_name}:TABLES")], [InlineKeyboardButton(text="❌ Выключить", callback_data=f"edit_rw_mode:{task_name}:DISABLE")], [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_edit:{task_name}")]])
        await callback.message.edit_text("Выберите режим удаления внешних водяных знаков:", reply_markup=rw_kb); return
    await state.set_state(TaskEditing.get_new_value)
    prompt_text = f"Введите новое значение для поля <b>{field}</b>."
    if field == 'translation': prompt_text = "Введите целевой язык для перевода (например, `ru`).\n\n<b>Чтобы отключить перевод, отправьте `-`.</b>"
    elif field == 'append_link': prompt_text = "Отправьте одну ссылку, которая будет добавляться в конец каждого поста. Чтобы удалить, отправьте `-`."
    elif field == 'replace_links': prompt_text = ("Отправьте список замен, каждая с новой строки, в формате:\n`старая_ссылка -> новая_ссылка`\n\nЧтобы очистить список, отправьте `-`.")
    await callback.message.edit_text(prompt_text); await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("edit_rw_mode:"), AdminFilter())
async def cb_edit_remove_watermark_mode(callback: CallbackQuery, state: FSMContext):
    await state.clear(); _, task_name, mode = callback.data.split(":"); task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if mode == "DISABLE": config['remove_watermark'] = {"enabled": False, "mode": "PHOTOS"}; await safe_answer_callback(callback, "Удаление вотермарков отключено.")
        else: config['remove_watermark'] = {"enabled": True, "mode": mode}; await safe_answer_callback(callback, f"Удаление вотермарков включено в режиме {mode}.")
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await callback.message.edit_text(f"Настройки для <b>{task_name}</b> обновлены. Какое поле вы хотите изменить?", reply_markup=task_edit_keyboard(task_name, config))

@main_router.callback_query(F.data.startswith("aiconfig_provider:"), AdminFilter(), TaskEditing.select_ai_provider)
async def process_ai_provider_select(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]; data = await state.get_data(); task_name = data['task_name']
    if provider == "disable":
        task_lock = await get_task_lock(task_name)
        async with task_lock: config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['ai_config'] = {"enabled": False}; await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        await state.clear(); await callback.message.edit_text("✅ AI-рерайтинг для этой задачи отключен.")
        await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard()); return
    await state.update_data(ai_provider=provider); await state.set_state(TaskEditing.get_ai_config_details)
    prompt_text = f"Введите название модели для **{provider}** (или отправьте `-`, чтобы использовать модель по умолчанию)."
    if provider == "local": prompt_text = "Введите данные для локального AI в формате:\n`модель\nhttp://endpoint/api/generate`"
    await callback.message.edit_text(prompt_text); await safe_answer_callback(callback)

@main_router.message(TaskEditing.get_ai_config_details, AdminFilter())
async def process_ai_config_details(message: Message, state: FSMContext):
    data = await state.get_data(); task_name, provider = data['task_name'], data['ai_provider']
    ai_config = {"enabled": True, "provider": provider, "prompt": DEFAULT_AI_PROMPT}
    if provider == "local":
        try: model, endpoint = message.text.strip().split('\n', 1); ai_config["model"] = model.strip(); ai_config["endpoint"] = endpoint.strip()
        except ValueError: await message.answer("Неверный формат. Введите модель и эндпоинт, каждый с новой строки."); return
    else:
        if message.text.strip() != '-': ai_config["model"] = message.text.strip()
    task_lock = await get_task_lock(task_name)
    async with task_lock: config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['ai_config'] = ai_config; await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear(); await message.answer(f"✅ Настройки AI для задачи <b>{task_name}</b> обновлены. Провайдер: <b>{provider}</b>.")
    await message.answer("Возврат в главное меню.", reply_markup=main_menu_keyboard())

@main_router.message(TaskEditing.get_new_value, AdminFilter())
async def process_new_value(message: Message, state: FSMContext):
    data = await state.get_data(); task_name, field, new_value_text = data['task_name'], data['field_to_edit'], message.text
    task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        try:
            if new_value_text.strip() == '-': config[field] = None
            elif field == 'source_channels': config[field] = [int(i.strip()) for i in new_value_text.split(',')]
            elif field in ['target_channel', 'api_id']: config[field] = int(new_value_text)
            elif field in ['hashtags', 'referral_links']: config[field] = [line.strip() for line in new_value_text.split('\n') if line.strip()]
            elif field == 'proxy': config[field] = new_value_text
            elif field == 'translation':
                if new_value_text.strip() == '-': config[field] = {'enabled': False, 'target_lang': config.get(field, {}).get('target_lang', 'en')}
                else:
                    if len(new_value_text.strip()) < 2: await message.answer("Неверный формат языка. Используйте двухбуквенный код."); return
                    config[field] = {'enabled': True, 'target_lang': new_value_text.strip()}
            elif field == 'append_link': config[field] = new_value_text.strip()
            elif field == 'replace_links':
                links_list = []
                for line in new_value_text.split('\n'):
                    if '->' in line: source, target = line.split('->', 1); links_list.append({'source': source.strip(), 'target': target.strip()})
                config[field] = links_list if links_list else None
            else: config[field] = new_value_text
        except ValueError: await message.answer("Неверный формат значения. Попробуйте снова."); return
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear()
    await message.answer(f"Поле <b>{field}</b> для задачи <b>{task_name}</b> успешно обновлено.")
    await message.answer("Возврат в главное меню.", reply_markup=main_menu_keyboard())


@main_router.callback_query(F.data.startswith("edit_gen_field:"), AdminFilter())
async def cb_edit_gen_field(callback: CallbackQuery, state: FSMContext):
    _, task_name, field = callback.data.split(":"); await state.update_data(task_name=task_name)
    if field == 'gen_posts_per_day':
        await state.set_state(TaskEditing.get_gen_posts_per_day)
        await callback.message.edit_text("Введите новое количество постов в день (1-24).")
    elif field == 'gen_text_provider':
        await state.set_state(TaskEditing.get_gen_text_provider)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🤖 Gemini", callback_data="edit_gen_text:gemini")], [InlineKeyboardButton(text="🤖 OpenAI", callback_data="edit_gen_text:openai")], [InlineKeyboardButton(text="🤖 Claude", callback_data="edit_gen_text:claude")]])
        await callback.message.edit_text("Выберите нового провайдера для генерации текста:", reply_markup=kb)
    elif field == 'gen_image_provider':
        await state.set_state(TaskEditing.get_gen_wants_images)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да, генерировать", callback_data="edit_gen_wants_images:yes")], [InlineKeyboardButton(text="❌ Нет, только текст", callback_data="edit_gen_wants_images:no")]])
        await callback.message.edit_text("Генерировать изображения для постов?", reply_markup=kb)
    await safe_answer_callback(callback)

@main_router.message(TaskEditing.get_gen_posts_per_day, AdminFilter())
async def process_edit_gen_posts_per_day(message: Message, state: FSMContext):
    try:
        posts_per_day = int(message.text.strip())
        if not 1 <= posts_per_day <= 24: raise ValueError
        data = await state.get_data(); task_name = data['task_name']; task_lock = await get_task_lock(task_name)
        async with task_lock:
            config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['generative_config']['posts_per_day'] = posts_per_day
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        await message.answer("✅ Количество постов в день обновлено.")
        await state.clear(); await message.answer("Возврат в главное меню.", reply_markup=main_menu_keyboard())
    except ValueError: await message.answer("Неверное число. Введите целое число от 1 до 24.")

@main_router.callback_query(F.data.startswith("edit_gen_text:"), TaskEditing.get_gen_text_provider)
async def process_edit_gen_text_provider(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]; data = await state.get_data(); task_name = data['task_name']; task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['generative_config']['text_provider'] = provider
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await callback.message.edit_text("✅ Провайдер для текста обновлен.")
    await state.clear(); await cb_main_menu(callback, state)

@main_router.callback_query(F.data.startswith("edit_gen_wants_images:"), TaskEditing.get_gen_wants_images)
async def process_edit_gen_wants_images(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":")[1]
    if choice == 'no':
        data = await state.get_data(); task_name = data['task_name']; task_lock = await get_task_lock(task_name)
        async with task_lock:
            config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['generative_config']['image_provider'] = 'none'
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        await callback.message.edit_text("✅ Генерация изображений отключена.")
        await state.clear(); await cb_main_menu(callback, state)
    else:
        await state.set_state(TaskEditing.get_gen_image_provider)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎨 DALL-E 3 (OpenAI)", callback_data="edit_gen_image_provider:dalle3")], [InlineKeyboardButton(text="🎨 Stable Diffusion (Replicate)", callback_data="edit_gen_image_provider:replicate")]])
        await callback.message.edit_text("Выберите провайдера для генерации изображений:", reply_markup=kb)
    await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("edit_gen_image_provider:"), TaskEditing.get_gen_image_provider)
async def process_edit_gen_image_provider(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]; data = await state.get_data(); task_name = data['task_name']; task_lock = await get_task_lock(task_name)
    async with task_lock:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json"); config['generative_config']['image_provider'] = provider
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await callback.message.edit_text("✅ Провайдер для изображений обновлен.")
    await state.clear(); await cb_main_menu(callback, state)

# --- ПРОЦЕСС СОЗДАНИЯ ЗАДАЧИ (FORWARDING) ---
@main_router.callback_query(F.data == "task_add", AdminFilter())
async def cb_task_add(callback: CallbackQuery, state: FSMContext): await state.set_state(TaskCreation.name); await callback.message.edit_text("<b>Шаг 1/12:</b> Введите уникальное название для задачи (имя файла сессии)."); await safe_answer_callback(callback)
@main_router.message(TaskCreation.name, AdminFilter())
async def process_task_name(message: Message, state: FSMContext):
    task_name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', task_name) or os.path.exists(TASKS_DIR / f"{task_name}.json"):
        await message.answer("Ошибка: Задача с таким именем уже существует или имя содержит недопустимые символы."); return
    if not os.path.exists(SESSIONS_DIR / f"{task_name}.session"): await message.answer(f"<b>Ошибка:</b> файл сессии `sessions/{task_name}.session` не найден."); await state.clear(); return
    await state.update_data(name=task_name); await state.set_state(TaskCreation.api_id); await message.answer("<b>Шаг 2/12:</b> Введите `api_id`.")
@main_router.message(TaskCreation.api_id, AdminFilter())
async def process_task_api_id(message: Message, state: FSMContext):
    try: await state.update_data(api_id=int(message.text)); await state.set_state(TaskCreation.api_hash); await message.answer("<b>Шаг 3/12:</b> Введите `api_hash`.")
    except ValueError: await message.answer("API ID должен быть числом."); return
@main_router.message(TaskCreation.api_hash, AdminFilter())
async def process_task_api_hash(message: Message, state: FSMContext): await state.update_data(api_hash=message.text); await state.set_state(TaskCreation.source_ids); await message.answer("<b>Шаг 4/12:</b> Введите ID каналов-источников через запятую.")
@main_router.message(TaskCreation.source_ids, AdminFilter())
async def process_task_source_ids(message: Message, state: FSMContext):
    try: await state.update_data(source_ids=[int(i.strip()) for i in message.text.split(',')]); await state.set_state(TaskCreation.target_id); await message.answer("<b>Шаг 5/12:</b> Введите ID целевого канала.")
    except ValueError: await message.answer("Неверный формат. Введите числовые ID через запятую.")
@main_router.message(TaskCreation.target_id, AdminFilter())
async def process_task_target_id(message: Message, state: FSMContext):
    try: await state.update_data(target_id=int(message.text)); await state.set_state(TaskCreation.target_link); await message.answer("<b>Шаг 6/12:</b> Введите публичную ссылку на целевой канал (например, @channelname).")
    except ValueError: await message.answer("Неверный формат. Введите числовой ID.")
@main_router.message(TaskCreation.target_link, AdminFilter())
async def process_task_target_link(message: Message, state: FSMContext): await state.update_data(target_link=message.text); await state.set_state(TaskCreation.proxy); await message.answer("<b>Шаг 7/12:</b> Введите данные прокси (`scheme://user:pass@host:port`) или отправьте `-`.")
@main_router.message(TaskCreation.proxy, AdminFilter())
async def process_task_proxy(message: Message, state: FSMContext):
    proxy_str, proxy_dict = message.text.strip(), None
    if proxy_str != '-':
        try: scheme, rest = proxy_str.split('://', 1); creds, host_port = rest.split('@', 1); user, password = creds.split(':', 1); host, port = host_port.split(':', 1); proxy_dict = {"scheme": scheme, "hostname": host, "port": int(port), "username": user, "password": password}
        except Exception: await message.answer("Неверный формат прокси. Используйте `scheme://user:pass@host:port` или `-`."); return
    await state.update_data(proxy=proxy_dict); await state.set_state(TaskCreation.translation); await message.answer("<b>Шаг 8/12:</b> Введите целевой язык для перевода (например: `ru`).\n\n<b>Чтобы пропустить, отправьте `-`.</b>")
@main_router.message(TaskCreation.translation, AdminFilter())
async def process_task_translation(message: Message, state: FSMContext):
    target_lang_input = message.text.strip(); translation_config = {}
    if target_lang_input == '-': translation_config = {"enabled": False, "target_lang": "en"}
    else:
        if len(target_lang_input) < 2: await message.answer("Неверный формат языка. Используйте двухбуквенный код (например, `ru`)."); return
        translation_config = {"enabled": True, "target_lang": target_lang_input}
    await state.update_data(translation=translation_config); await state.set_state(TaskCreation.select_ai_provider)
    providers_kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="OpenAI (По умолч.)", callback_data="aiconfig_create:openai")], [InlineKeyboardButton(text="Anthropic (Claude)", callback_data="aiconfig_create:claude")], [InlineKeyboardButton(text="Google (Gemini)", callback_data="aiconfig_create:local")], [InlineKeyboardButton(text="❌ Пропустить (Отключить)", callback_data="aiconfig_create:disable")]])
    await message.answer("<b>Шаг 9/12:</b> Настройте AI-рерайтинг.", reply_markup=providers_kb)
@main_router.callback_query(F.data.startswith("aiconfig_create:"), AdminFilter(), TaskCreation.select_ai_provider)
async def process_create_ai_provider(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]
    if provider == "disable":
        await state.update_data(ai_config={"enabled": False}); await state.set_state(TaskCreation.watermark)
        await callback.message.edit_text("<b>Шаг 10/12:</b> Загрузите ВАШ водяной знак как ДОКУМЕНТ (файл .png)."); await safe_answer_callback(callback); return
    await state.update_data(ai_provider=provider); await state.set_state(TaskCreation.get_ai_config_details)
    prompt_text = f"Введите название модели для **{provider}** (или отправьте `-`, чтобы использовать модель по умолчанию)."
    if provider == "local": prompt_text = "Введите данные для локального AI в формате:\n`модель\nhttp://endpoint/api/generate`"
    await callback.message.edit_text(prompt_text); await safe_answer_callback(callback)
@main_router.message(TaskCreation.get_ai_config_details, AdminFilter())
async def process_create_ai_details(message: Message, state: FSMContext):
    data = await state.get_data(); provider = data['ai_provider']; ai_config = {"enabled": True, "provider": provider, "prompt": DEFAULT_AI_PROMPT}
    if provider == "local":
        try: model, endpoint = message.text.strip().split('\n', 1); ai_config["model"] = model.strip(); ai_config["endpoint"] = endpoint.strip()
        except ValueError: await message.answer("Неверный формат."); return
    else:
        if message.text.strip() != '-': ai_config["model"] = message.text.strip()
    await state.update_data(ai_config=ai_config); await state.set_state(TaskCreation.watermark)
    await message.answer("<b>Шаг 10/12:</b> Загрузите ВАШ водяной знак как ДОКУМЕНТ (файл .png).")

@main_router.message(TaskCreation.watermark, F.document, AdminFilter())
async def process_task_watermark_doc(message: Message, state: FSMContext):
    if not message.document.mime_type or 'image/png' not in message.document.mime_type: await message.answer("Ошибка. Отправьте файл .png"); return
    data = await state.get_data(); task_name = data['name']; file_path = WATERMARKS_DIR / f"{task_name}.png"
    await bot.download(message.document, destination=file_path); await state.update_data(watermark_file=str(file_path))
    await state.set_state(TaskCreation.remove_watermark_toggle)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="rw_toggle:yes")], [InlineKeyboardButton(text="❌ Нет", callback_data="rw_toggle:no")]])
    await message.answer("<b>Шаг 11/12:</b> Включить удаление внешних водяных знаков с изображений?", reply_markup=kb)

@main_router.message(TaskCreation.watermark, AdminFilter())
async def process_task_watermark_other(message: Message, state: FSMContext): await message.answer("Отправьте водяной знак как ДОКУМЕНТ .png.")

@main_router.callback_query(F.data.startswith("rw_toggle:"), AdminFilter(), TaskCreation.remove_watermark_toggle)
async def process_rw_toggle(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":")[1]
    if action == "no":
        await state.update_data(remove_watermark={"enabled": False, "mode": "PHOTOS"})
        await state.set_state(TaskCreation.final_details)
        await callback.message.edit_text("<b>Шаг 12/12:</b> Введите хэштеги, ссылки и т.д. или отправьте `-`.\n(См. документацию)")
    else:
        await state.set_state(TaskCreation.remove_watermark_mode)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Режим ФОТО (удаляет текст)", callback_data="rw_mode:PHOTOS")], [InlineKeyboardButton(text="Режим ТАБЛИЦЫ (сохраняет текст)", callback_data="rw_mode:TABLES")]])
        await callback.message.edit_text("Выберите режим удаления водяных знаков:", reply_markup=kb)
    await safe_answer_callback(callback)

@main_router.callback_query(F.data.startswith("rw_mode:"), AdminFilter(), TaskCreation.remove_watermark_mode)
async def process_rw_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(remove_watermark={"enabled": True, "mode": mode})
    await state.set_state(TaskCreation.final_details)
    await callback.message.edit_text(f"Режим {mode} выбран.\n\n<b>Шаг 12/12:</b> Введите хэштеги, ссылки и т.д. или отправьте `-`.\n(См. документацию)")
    await safe_answer_callback(callback)


@main_router.message(TaskCreation.final_details, AdminFilter())
async def process_task_final_details(message: Message, state: FSMContext):
    text = message.text; hashtags, links = [], [];
    if text.strip() != '-':
        lines = text.split('\n'); hashtags = [f"#{tag.strip().lstrip('#')}" for tag in lines[0].split(',') if tag.strip()]
        if len(lines) > 1: links = [link.strip() for link in lines[1:] if link.strip()]
    await state.update_data(hashtags=hashtags, links=links)
    await state.set_state(TaskCreation.instagram_toggle)
    await message.answer("<b>Завершение: Социальные сети</b>\n\nВключить постинг в Instagram/Threads?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="task_insta_toggle:yes"), InlineKeyboardButton(text="❌ Нет", callback_data="task_insta_toggle:no")]]))

@main_router.callback_query(F.data.startswith("task_insta_toggle:"), AdminFilter(), TaskCreation.instagram_toggle)
async def process_task_instagram_toggle(callback: CallbackQuery, state: FSMContext):
    if callback.data.split(":")[1] == 'yes': await state.update_data(instagram_enabled=True); await state.set_state(TaskCreation.instagram_creds); await callback.message.edit_text("Введите логин и пароль от Instagram через пробел.")
    else: await state.update_data(instagram_enabled=False, instagram_creds=None); await state.set_state(TaskCreation.twitter_toggle); await callback.message.edit_text("Включить постинг в X (Twitter)?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="task_x_toggle:yes"), InlineKeyboardButton(text="❌ Нет", callback_data="task_x_toggle:no")]]))
    await safe_answer_callback(callback)

@main_router.message(TaskCreation.instagram_creds, AdminFilter())
async def process_task_instagram_creds(message: Message, state: FSMContext):
    try: username, password = message.text.strip().split(' ', 1); await state.update_data(instagram_creds={"username": username, "password": password}); await state.set_state(TaskCreation.twitter_toggle); await message.answer("Включить постинг в X (Twitter)?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="task_x_toggle:yes"), InlineKeyboardButton(text="❌ Нет", callback_data="task_x_toggle:no")]]))
    except ValueError: await message.answer("Неверный формат. Введите логин и пароль через пробел.")

@main_router.callback_query(F.data.startswith("task_x_toggle:"), AdminFilter(), TaskCreation.twitter_toggle)
async def process_task_x_toggle(callback: CallbackQuery, state: FSMContext):
    if callback.data.split(":")[1] == 'yes': await state.update_data(twitter_enabled=True); await state.set_state(TaskCreation.twitter_creds); await callback.message.edit_text("Пришлите данные для X (Twitter) в 4 строки:\n1. Consumer Key\n2. Consumer Secret\n3. Access Token\n4. Access Token Secret")
    else: await state.update_data(twitter_enabled=False, twitter_creds=None); await finalize_task_creation(callback.message, state)
    await safe_answer_callback(callback)

@main_router.message(TaskCreation.twitter_creds, AdminFilter())
async def process_task_x_creds(message: Message, state: FSMContext):
    try:
        consumer_key, consumer_secret, access_token, access_token_secret = message.text.strip().split('\n', 3)
        creds = {"consumer_key": consumer_key.strip(), "consumer_secret": consumer_secret.strip(), "access_token": access_token.strip(), "access_token_secret": access_token_secret.strip()}
        await state.update_data(twitter_creds=creds); await message.answer("Завершаю создание задачи..."); await finalize_task_creation(message, state)
    except ValueError: await message.answer("Неверный формат. Введите 4 ключа, каждый с новой строки.")

async def finalize_task_creation(message: Message, state: FSMContext):
    data = await state.get_data(); insta_creds, x_creds = data.get('instagram_creds'), data.get('twitter_creds')
    task_config = {
        "task_name": data['name'], "task_type": "forwarding", "api_id": data['api_id'], "api_hash": data['api_hash'], "status": "inactive", "last_error": None,
        "source_channels": data['source_ids'], "target_channel": data['target_id'], "target_channel_link": data['target_link'],
        "proxy": data.get('proxy'), "translation": data['translation'], "ai_config": data.get('ai_config'),
        "watermark_file": data.get('watermark_file'),
        "remove_watermark": data.get('remove_watermark', {"enabled": False, "mode": "PHOTOS"}),
        "hashtags": data.get('hashtags', []), "referral_links": data.get('links', []),
        "append_link": None, "replace_links": None,
        "instagram": {"enabled": data.get('instagram_enabled', False), "username": insta_creds.get('username', '') if insta_creds else '', "password": insta_creds.get('password', '') if insta_creds else '', "last_status": "Not configured"},
        "twitter": {"enabled": data.get('twitter_enabled', False), "consumer_key": x_creds.get('consumer_key', '') if x_creds else '', "consumer_secret": x_creds.get('consumer_secret', '') if x_creds else '', "access_token": x_creds.get('access_token', '') if x_creds else '', "access_token_secret": x_creds.get('access_token_secret', '') if x_creds else '', "last_status": "Not configured"}
    }
    task_lock = await get_task_lock(data['name']); await write_json_file(TASKS_DIR / f"{data['name']}.json", task_config, task_lock)
    await state.clear(); await message.answer(f"✅ Задача <b>{data['name']}</b> успешно создана!", reply_markup=main_menu_keyboard())

# --- ОСТАЛЬНЫЕ ОБРАБОТЧИКИ ---
async def manage_mailings_keyboard():
    buttons = []; mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}
    for mailing_id, data in mailings.items(): mailing_type = "Глобальная" if mailing_id.startswith("global_") else f"Задача: {data['task_name']}"; buttons.append([InlineKeyboardButton(text=f"Рассылка {mailing_id[:8]} ({mailing_type})", callback_data=f"mailing_view:{mailing_id}")])
    buttons.extend([[InlineKeyboardButton(text="➕ Создать рассылку", callback_data="mailing_add")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]]); return InlineKeyboardMarkup(inline_keyboard=buttons)
@main_router.callback_query(F.data == "manage_mailings", AdminFilter())
async def cb_manage_mailings(callback: CallbackQuery, state: FSMContext): await state.clear(); await callback.message.edit_text("Управление рассылками", reply_markup=await manage_mailings_keyboard())
@main_router.callback_query(F.data == "mailing_add", AdminFilter())
async def cb_mailing_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(MailingCreation.select_task)
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    if not task_files: await safe_answer_callback(callback, "Сначала создайте хотя бы одну задачу.", show_alert=True); await state.clear(); return
    tasks_by_type = {"forwarding": [], "generative": [], "advertising": []}
    type_to_emoji = {"forwarding": "📋", "generative": "✨", "advertising": "📣"}
    type_to_name = {"forwarding": "Форвардинг", "generative": "Генеративные", "advertising": "Рекламные"}
    for task_file in sorted(task_files):
        try:
            config = await read_json_file(task_file)
            if config:
                task_type = config.get("task_type", "forwarding")
                if task_type in tasks_by_type: tasks_by_type[task_type].append(task_file.stem)
        except Exception as e: logging.error(f"Ошибка чтения файла задачи {task_file.name} при создании рассылки: {e}")
    buttons = []; text = "<b>Шаг 1/3:</b> Выберите задачу для рассылки."
    for task_type, task_names in tasks_by_type.items():
        if task_names:
            emoji = type_to_emoji.get(task_type, "📁"); category_name = type_to_name.get(task_type, "Прочие")
            buttons.append([InlineKeyboardButton(text=f"{emoji} {category_name} {emoji}", callback_data="noop")])
            for name in task_names: buttons.append([InlineKeyboardButton(text=name, callback_data=f"mailing_select_task:{name}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="manage_mailings")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)
@main_router.callback_query(F.data.startswith("mailing_select_task:"), AdminFilter(), MailingCreation.select_task)
async def cb_mailing_select_task(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]; await state.update_data(task_name=task_name); await state.set_state(MailingCreation.get_content)
    await callback.message.edit_text("<b>Шаг 2/3:</b> Пришлите сообщение для рассылки."); await safe_answer_callback(callback)
async def handle_album_message(message: Message, state: FSMContext, callback_prefix: str):
    media_group_id = message.media_group_id
    if media_group_id not in TEMP_MEDIA_GROUPS:
        async def send_confirmation_after_delay():
            await asyncio.sleep(ALBUM_COLLECTION_DELAY)
            media_group = TEMP_MEDIA_GROUPS.get(media_group_id)
            if media_group:
                current_state_str = await state.get_state()
                if not current_state_str or 'confirm_album' not in current_state_str: await state.set_state(f"confirm_album_placeholder_for_{callback_prefix}")
                await bot.send_message(chat_id=message.chat.id, text=f"Получен альбом с {len(media_group['messages'])} фото. Подтвердите.", reply_markup=confirm_album_keyboard(media_group_id, callback_prefix))
        TEMP_MEDIA_GROUPS[media_group_id] = {'messages': [], 'timer': asyncio.create_task(send_confirmation_after_delay())}
    if message.photo: TEMP_MEDIA_GROUPS[media_group_id]['messages'].append({'message_id': message.message_id, 'chat_id': message.chat.id, 'caption': message.caption or "", 'photo_file_id': message.photo[-1].file_id})
@main_router.message(F.content_type.in_({'text', 'photo', 'video', 'animation'}), MailingCreation.get_content, AdminFilter())
async def process_mailing_content(message: Message, state: FSMContext):
    if message.media_group_id: await handle_album_message(message, state, "confirm_album"); return
    try:
        forwarded_message = await bot.forward_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=message.chat.id, message_id=message.message_id)
        await state.update_data(storage_message_id=forwarded_message.message_id); await state.set_state(MailingCreation.select_schedule_type)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Сейчас", callback_data="mailing_type:now")], [InlineKeyboardButton(text="🕒 Однократно", callback_data="mailing_type:one-time")], [InlineKeyboardButton(text="🔁 Периодически", callback_data="mailing_type:recurring")]])
        await message.answer("<b>Шаг 3/3:</b> Выберите тип рассылки:", reply_markup=keyboard)
    except Exception as e: await message.answer(f"Не удалось сохранить сообщение. Ошибка: {e}"); await state.clear()
@main_router.callback_query(F.data.startswith("confirm_album:"), AdminFilter())
async def cb_confirm_mailing_album(callback: CallbackQuery, state: FSMContext):
    _, action, media_group_id = callback.data.split(":"); album_data = TEMP_MEDIA_GROUPS.pop(media_group_id, None)
    if not album_data: await safe_answer_callback(callback, "Время ожидания истекло.", show_alert=True); return
    if action == "no": await state.set_state(MailingCreation.get_content); await callback.message.edit_text("Альбом отменен. Пришлите новое сообщение."); await safe_answer_callback(callback); return
    try:
        media_group_messages = album_data['messages']; media_group_ids = []; caption = next((msg['caption'] for msg in media_group_messages if msg['caption']), "")
        for msg_data in media_group_messages: forwarded = await bot.forward_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=msg_data['chat_id'], message_id=msg_data['message_id']); media_group_ids.append(forwarded.message_id)
        await state.update_data(media_group_ids=media_group_ids, caption=caption); await state.set_state(MailingCreation.select_schedule_type)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Сейчас", callback_data="mailing_type:now")], [InlineKeyboardButton(text="🕒 Однократно", callback_data="mailing_type:one-time")], [InlineKeyboardButton(text="🔁 Периодически", callback_data="mailing_type:recurring")]])
        await callback.message.edit_text(f"<b>Шаг 3/3:</b> Альбом ({len(media_group_ids)} фото) сохранен. Выберите тип рассылки:", reply_markup=keyboard)
    except Exception as e: await safe_answer_callback(callback, f"Ошибка сохранения альбома: {e}", show_alert=True); await state.set_state(MailingCreation.get_content); await callback.message.edit_text("Пришлите сообщение заново.")
@main_router.callback_query(F.data.startswith("mailing_type:"), AdminFilter(), MailingCreation.select_schedule_type)
async def cb_mailing_type(callback: CallbackQuery, state: FSMContext):
    schedule_type = callback.data.split(":")[1]; data = await state.get_data(); mailing_id = str(uuid.uuid4())
    mailing_data = {"id": mailing_id, "task_name": data['task_name'], "schedule_type": schedule_type}
    if 'media_group_ids' in data: mailing_data['media_group_ids'] = data['media_group_ids']; mailing_data['caption'] = data.get('caption', '')
    else: mailing_data['storage_message_id'] = data['storage_message_id']
    if schedule_type == 'now':
        await callback.message.edit_text("Отправляю..."); await send_mailing_job(mailing_id=mailing_id, direct_data=mailing_data)
        await callback.message.edit_text(f"✅ Рассылка <b>{mailing_id[:8]}</b> была отправлена.", reply_markup=main_menu_keyboard()); await state.clear(); return
    await state.update_data(schedule_type=schedule_type); await state.set_state(MailingCreation.get_schedule_details)
    if schedule_type == 'one-time': await callback.message.edit_text("Введите дату и время `ГГГГ-ММ-ДД ЧЧ:ММ`.")
    else: await callback.message.edit_text("Введите интервал в часах (например, `24`).")
    await safe_answer_callback(callback)
@main_router.message(MailingCreation.get_schedule_details, AdminFilter())
async def process_mailing_schedule(message: Message, state: FSMContext):
    data = await state.get_data(); schedule_type, details, mailing_id = data['schedule_type'], message.text, str(uuid.uuid4())
    mailing_data = {"id": mailing_id, "task_name": data['task_name'], "schedule_type": schedule_type}
    if 'media_group_ids' in data: mailing_data['media_group_ids'] = data['media_group_ids']; mailing_data['caption'] = data.get('caption', '')
    else: mailing_data['storage_message_id'] = data['storage_message_id']
    try:
        if schedule_type == 'one-time': mailing_data['run_date'] = datetime.strptime(details, "%Y-%m-%d %H:%M").isoformat()
        else: mailing_data['interval_hours'] = int(details)
    except ValueError: await message.answer("Неверный формат."); return
    mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}; mailings[mailing_id] = mailing_data
    await write_json_file(MAILINGS_FILE, mailings, MAILING_LOCK); await schedule_mailing(mailing_data); await state.clear()
    await message.answer(f"✅ Рассылка <b>{mailing_id[:8]}</b> создана.", reply_markup=main_menu_keyboard())
@main_router.callback_query(F.data.startswith("mailing_view:"), AdminFilter())
async def cb_mailing_view(callback: CallbackQuery):
    mailing_id = callback.data.split(":")[1]; mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}; mailing_data = mailings.get(mailing_id)
    if not mailing_data: await safe_answer_callback(callback, "Рассылка не найдена.", show_alert=True); return
    target_info = "Глобальная" if mailing_id.startswith("global_") else f"Задача: {mailing_data['task_name']}"; text = f"Просмотр рассылки <b>{mailing_id[:8]}</b>\n\n<b>Цель:</b> {target_info}\n<b>Тип:</b> {mailing_data.get('schedule_type', 'now')}\n"
    if 'media_group_ids' in mailing_data: text += f"<b>Контент:</b> Альбом ({len(mailing_data['media_group_ids'])} фото)\n"
    else: text += "<b>Контент:</b> Одиночное сообщение\n"
    if mailing_data.get('schedule_type') == 'one-time': text += f"<b>Дата:</b> {mailing_data['run_date']}\n"
    elif mailing_data.get('schedule_type') == 'recurring': text += f"<b>Интервал:</b> {mailing_data['interval_hours']} часов\n"
    await callback.message.edit_text(text, reply_markup=mailing_view_keyboard(mailing_id))
@main_router.callback_query(F.data.startswith("mailing_delete:"), AdminFilter())
async def cb_mailing_delete(callback: CallbackQuery, state: FSMContext):
    mailing_id = callback.data.split(":")[1]; mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}
    if mailing_id in mailings: mailings.pop(mailing_id); await write_json_file(MAILINGS_FILE, mailings, MAILING_LOCK)
    try: global_scheduler.remove_job(mailing_id)
    except JobLookupError: pass
    await safe_answer_callback(callback, f"Рассылка {mailing_id[:8]} удалена.", show_alert=True); await cb_manage_mailings(callback, state)
@main_router.callback_query(F.data == "global_mailing_add", AdminFilter())
async def cb_global_mailing_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GlobalMailing.get_content)
    await callback.message.edit_text("Готовим **глобальную рассылку**.\n\nПришлите сообщение."); await safe_answer_callback(callback)
@main_router.message(F.content_type.in_({'text', 'photo', 'video', 'animation'}), GlobalMailing.get_content, AdminFilter())
async def process_global_mailing_content(message: Message, state: FSMContext):
    if message.media_group_id: await handle_album_message(message, state, "global_confirm_album"); return
    try:
        forwarded_message = await bot.forward_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=message.chat.id, message_id=message.message_id)
        await state.update_data(storage_message_id=forwarded_message.message_id); await state.set_state(GlobalMailing.select_schedule_type)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Сейчас", callback_data="global_mailing_type:now")], [InlineKeyboardButton(text="🕒 Однократно", callback_data="global_mailing_type:one-time")], [InlineKeyboardButton(text="🔁 Периодически", callback_data="global_mailing_type:recurring")]])
        await message.answer("Выберите тип глобальной рассылки:", reply_markup=keyboard)
    except Exception as e: await message.answer(f"Ошибка сохранения: {e}"); await state.clear()
@main_router.callback_query(F.data.startswith("global_confirm_album:"), AdminFilter())
async def cb_confirm_global_mailing_album(callback: CallbackQuery, state: FSMContext):
    _, action, media_group_id = callback.data.split(":"); album_data = TEMP_MEDIA_GROUPS.pop(media_group_id, None)
    if not album_data: await safe_answer_callback(callback, "Время ожидания истекло.", show_alert=True); return
    if action == "no": await state.set_state(GlobalMailing.get_content); await callback.message.edit_text("Альбом отменен."); await safe_answer_callback(callback); return
    try:
        media_group_messages = album_data['messages']; media_group_ids = []; caption = next((msg['caption'] for msg in media_group_messages if msg['caption']), "")
        for msg_data in media_group_messages: forwarded = await bot.forward_message(chat_id=STORAGE_CHANNEL_ID, from_chat_id=msg_data['chat_id'], message_id=msg_data['message_id']); media_group_ids.append(forwarded.message_id)
        await state.update_data(media_group_ids=media_group_ids, caption=caption); await state.set_state(GlobalMailing.select_schedule_type)
        keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="▶️ Сейчас", callback_data="global_mailing_type:now")], [InlineKeyboardButton(text="🕒 Однократно", callback_data="global_mailing_type:one-time")], [InlineKeyboardButton(text="🔁 Периодически", callback_data="global_mailing_type:recurring")]])
        await callback.message.edit_text(f"Альбом ({len(media_group_ids)} фото) сохранен. Выберите тип рассылки:", reply_markup=keyboard); await safe_answer_callback(callback)
    except Exception as e: await safe_answer_callback(callback, f"Ошибка сохранения: {e}", show_alert=True); await state.set_state(GlobalMailing.get_content); await callback.message.edit_text("Пришлите сообщение заново.")
@main_router.callback_query(F.data.startswith("global_mailing_type:"), AdminFilter(), GlobalMailing.select_schedule_type)
async def cb_global_mailing_type(callback: CallbackQuery, state: FSMContext):
    schedule_type = callback.data.split(":")[1]; data = await state.get_data(); mailing_id = f"global_{str(uuid.uuid4())}"
    mailing_data = {"id": mailing_id, "schedule_type": schedule_type}
    if 'media_group_ids' in data: mailing_data['media_group_ids'] = data['media_group_ids']; mailing_data['caption'] = data.get('caption', '')
    else: mailing_data['storage_message_id'] = data['storage_message_id']
    if schedule_type == 'now':
        await callback.message.edit_text("Отправляю глобальную рассылку..."); await send_mailing_job(mailing_id=mailing_id, direct_data=mailing_data)
        await callback.message.edit_text(f"✅ Глобальная рассылка <b>{mailing_id[:8]}</b> отправлена.", reply_markup=main_menu_keyboard()); await state.clear(); await safe_answer_callback(callback); return
    await state.update_data(schedule_type=schedule_type); await state.set_state(GlobalMailing.get_schedule_details)
    if schedule_type == 'one-time': await callback.message.edit_text("Введите дату и время `ГГГГ-ММ-ДД ЧЧ:ММ`.")
    else: await callback.message.edit_text("Введите интервал в часах (например, `24`).")
    await safe_answer_callback(callback)
@main_router.message(GlobalMailing.get_schedule_details, AdminFilter())
async def process_global_mailing_schedule(message: Message, state: FSMContext):
    data = await state.get_data(); schedule_type, details = data['schedule_type'], message.text; mailing_id = f"global_{str(uuid.uuid4())}"
    mailing_data = {"id": mailing_id, "schedule_type": schedule_type}
    if 'media_group_ids' in data: mailing_data['media_group_ids'] = data['media_group_ids']; mailing_data['caption'] = data.get('caption', '')
    else: mailing_data['storage_message_id'] = data['storage_message_id']
    try:
        if schedule_type == 'one-time': mailing_data['run_date'] = datetime.strptime(details, "%Y-%m-%d %H:%M").isoformat()
        else: mailing_data['interval_hours'] = int(details)
    except ValueError: await message.answer("Неверный формат."); return
    mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}; mailings[mailing_id] = mailing_data
    await write_json_file(MAILINGS_FILE, mailings, MAILING_LOCK); await schedule_mailing(mailing_data); await state.clear()
    await message.answer(f"✅ Глобальная рассылка <b>{mailing_id[:8]}</b> создана.", reply_markup=main_menu_keyboard())
@main_router.callback_query(F.data == "view_activity", AdminFilter())
async def cb_view_activity(callback: CallbackQuery, state: FSMContext):
    await state.clear(); active_tasks = len(ACTIVE_TASKS); task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]; total_tasks = len(task_files)
    mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}; active_mailings = sum(1 for m in mailings.values() if m.get('schedule_type') in ['one-time', 'recurring'])
    text = (f"📊 <b>Текущая активность</b>\n\n" f"🛠 <b>Задачи:</b> {active_tasks} активных из {total_tasks}\n" f"📨 <b>Рассылки:</b> {active_mailings} запланированных\n" f"⏰ <b>Планировщик:</b> {'работает' if global_scheduler.running else 'остановлен'}\n")
    buttons = [[InlineKeyboardButton(text="📋 К задачам", callback_data="manage_tasks")], [InlineKeyboardButton(text="📨 К рассылкам", callback_data="manage_mailings")], [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")]]
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)); await safe_answer_callback(callback)
@main_router.callback_query(F.data.startswith("task_social_post:"), AdminFilter())
async def cb_task_social_post(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]; await state.update_data(task_name=task_name); await state.set_state(SocialMediaPosting.get_content)
    await callback.message.edit_text(f"Готовим пост для соцсетей задачи <b>{task_name}</b>.\nПришлите контент (текст, фото, альбом)."); await safe_answer_callback(callback)
@main_router.message(F.content_type.in_({'text', 'photo', 'video'}), SocialMediaPosting.get_content, AdminFilter())
async def process_social_post_content(message: Message, state: FSMContext):
    data = await state.get_data(); task_name = data['task_name']
    if message.media_group_id: await handle_album_message(message, state, "social_confirm_album"); return
    msg = await message.answer("Обрабатываю и отправляю пост...")
    text_content, media_paths, temp_files = message.caption or message.text or "", [], []
    media_to_download = message.photo[-1] if message.photo else message.video if message.video else None
    if media_to_download:
        try:
            extension = ".mp4" if message.video else ".jpg"; temp_path = TEMP_DIR / f"temp_social_{uuid.uuid4()}{extension}"
            await bot.download(media_to_download, destination=temp_path)
            original_path = str(temp_path); temp_files.append(original_path); media_paths.append(original_path)
        except Exception as e: await msg.edit_text(f"Не удалось скачать медиа: {e}"); return
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    await post_to_instagram(task_name, config, text_content, media_paths)
    await post_to_x(task_name, config, text_content, media_paths)
    for p in temp_files: await remove_file_async(p)
    await msg.edit_text("✅ Отправка в соцсети завершена.", reply_markup=main_menu_keyboard()); await state.clear()
@main_router.callback_query(F.data.startswith("social_confirm_album:"), AdminFilter())
async def cb_confirm_social_album(callback: CallbackQuery, state: FSMContext):
    _, action, media_group_id = callback.data.split(":"); album_data = TEMP_MEDIA_GROUPS.pop(media_group_id, None)
    if not album_data: await safe_answer_callback(callback, "Время ожидания истекло.", show_alert=True); return
    if action == "no": await state.set_state(SocialMediaPosting.get_content); await callback.message.edit_text("Альбом отменен."); await safe_answer_callback(callback); return
    data = await state.get_data(); task_name = data['task_name']; msg = await callback.message.edit_text("Обрабатываю и отправляю альбом...")
    caption = next((msg['caption'] for msg in album_data['messages'] if msg['caption']), ""); media_paths, temp_files = [], []
    try:
        for msg_data in album_data['messages']:
            photo_file_id = msg_data.get('photo_file_id')
            if not photo_file_id: continue
            temp_path = TEMP_DIR / f"temp_social_{uuid.uuid4()}.jpg"; await bot.download(photo_file_id, destination=temp_path)
            original_path = str(temp_path); temp_files.append(original_path); media_paths.append(original_path)
        if not media_paths: raise ValueError("Не удалось скачать фото из альбома.")
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        await post_to_instagram(task_name, config, caption, media_paths)
        await post_to_x(task_name, config, caption, media_paths)
        await msg.edit_text("✅ Отправка альбома в соцсети завершена.", reply_markup=main_menu_keyboard())
    except Exception as e: logging.error(f"Ошибка ручной отправки альбома: {e}", exc_info=True); await msg.edit_text(f"❗️ Произошла ошибка: {e}")
    finally:
        for p in temp_files: await remove_file_async(p)
        await state.clear()

# --- Запуск бота ---
async def on_startup():
    global_scheduler.add_job(daily_forwarding_report_job, 'cron', hour=8, minute=0, misfire_grace_time=3600)
    global_scheduler.add_job(daily_subscriber_check, 'cron', hour=9, minute=0, misfire_grace_time=3600)
    global_scheduler.add_job(update_social_stats_job, 'interval', hours=4, misfire_grace_time=3600, next_run_time=datetime.now() + timedelta(minutes=5))
    global_scheduler.start()
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in task_files:
        config = await read_json_file(task_file)
        if config and config.get('status') == 'active':
            logging.info(f"Запускаю активную задачу '{task_file.stem}' при старте...")
            await start_task(task_file.stem)
    mailings = await read_json_file(MAILINGS_FILE, MAILING_LOCK) or {}; now = datetime.now()
    for mailing_id, mailing_data in list(mailings.items()):
        if mailing_data.get('schedule_type') == 'one-time' and 'run_date' in mailing_data:
            try:
                if datetime.fromisoformat(mailing_data['run_date']) < now:
                    mailings.pop(mailing_id); continue
            except (ValueError, TypeError): continue
        await schedule_mailing(mailing_data)
    await write_json_file(MAILINGS_FILE, mailings, MAILING_LOCK)
    logging.info("Бот успешно запущен.")

async def on_shutdown():
    active_task_names = list(ACTIVE_TASKS.keys())
    for task_name in active_task_names:
        await stop_worker(task_name, bot, "Бот завершает работу.")
    if global_scheduler.running:
        global_scheduler.shutdown()
    await asyncio.sleep(2)
    logging.info("Бот остановлен.")

async def run_bot():
    generative_channels.register_handlers(main_router, bot)
    advertising_tasks.register_handlers(main_router, bot)
    channel_management_tasks.register_handlers(main_router, bot)
    
    main_router.callback_query.register(cb_task_edit, F.data.startswith("task_edit:"), AdminFilter())
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

async def main():
    max_delay, attempt = 300, 0
    while True:
        try:
            await run_bot()
            break
        except asyncio.CancelledError:
            break
        except Exception as e:
            await on_shutdown()
            delay = min(2 ** attempt, max_delay)
            logging.error(f"Критический сбой бота: {e}. Перезапуск через {delay} секунд...", exc_info=True)
            await asyncio.sleep(delay)
            attempt += 1

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Бот остановлен.")