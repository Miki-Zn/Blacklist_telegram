# channel_management_tasks.py

import asyncio
import logging
import random
import os
import uuid
import re
from datetime import datetime, timedelta
from typing import Union, List

# Aiogram
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.client.bot import Bot
from aiogram.exceptions import TelegramBadRequest

# Pyrogram и планировщик
from pyrogram import Client
from pyrogram.errors import RPCError, FloodWait, UserAlreadyParticipant
from pyrogram.types import InputMediaPhoto, InputMediaVideo
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# Утилиты
from utils import (
    TASKS_DIR, SESSIONS_DIR, WATERMARKS_DIR, TEMP_DIR, ADMIN_IDS, ACTIVE_CLIENTS,
    STORAGE_CHANNEL_ID, ALBUM_COLLECTION_DELAY,
    write_json_file, read_json_file, get_task_lock, remove_file_async,
    apply_watermark_async, confirm_album_keyboard, AdminFilter, TEMP_MEDIA_GROUPS,
    stop_worker, main_menu_keyboard, azure_translate, send_with_retry, safe_answer_callback,
    generate_ai_text, generate_ai_image
)

# Глобальный экземпляр бота
bot_instance: Union[Bot, None] = None
cb_task_edit = None

# --- FSM (Конечные автоматы) ---
class ChannelTaskCreation(StatesGroup):
    name, api_id, api_hash, proxy, watermark = [State() for _ in range(5)]
    get_main_channel_id, get_main_channel_link, get_main_channel_lang = [State() for _ in range(3)]
    ask_for_another_channel, get_extra_channel_id, get_extra_channel_link, get_extra_channel_lang = [State() for _ in range(4)]

class ContentPoolManagement(StatesGroup):
    get_content_type = State()
    get_content_prompt = State()
    ask_for_image_type = State()
    get_custom_image = State()
    get_immediate_post_content = State()
    confirm_album = State()
    get_schedule_details_type = State()
    get_schedule_details_value = State()

class ChannelTaskEditing(StatesGroup):
    get_new_channel_id = State()
    get_new_channel_link = State()
    get_new_channel_lang = State()


# --- Клавиатуры ---
def channel_task_edit_keyboard(task_name: str, config: dict):
    mng_config = config.get("management_config", {})
    trans_provider = mng_config.get("translation_provider", "azure")
    text_gen_provider = mng_config.get("text_generation_provider", "openai")
    trans_text = "Azure" if trans_provider == "azure" else f"AI ({trans_provider.title()})"
    buttons = [
        [InlineKeyboardButton(text="- Каналы и языки", callback_data=f"channel_edit_channels:{task_name}")],
        [InlineKeyboardButton(text=f"- Оператор перевода: {trans_text}", callback_data=f"channel_edit_translator_menu:{task_name}")],
        [InlineKeyboardButton(text=f"- AI для генерации текста: {text_gen_provider.title()}", callback_data=f"channel_edit_text_gen_menu:{task_name}")],
        [InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def channel_content_pool_keyboard(task_name: str):
    buttons = [
        [InlineKeyboardButton(text="➕ Обычный пост", callback_data=f"channel_add_content_menu:{task_name}")],
        [InlineKeyboardButton(text="✨ Генеративный пост", callback_data=f"channel_add_generative:{task_name}")],
        [InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"channel_post_now:{task_name}")],
        [InlineKeyboardButton(text="📋 Показать все посты", callback_data=f"channel_view_content:{task_name}")],
        # --- ИСПРАВЛЕНИЕ ЗДЕСЬ ---
        # Меняем callback_data на "task_view", чтобы вернуться к главному меню задачи
        [InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Основная логика выполнения постов ---
async def _execute_post_logic(task_name: str, client: Client, config: dict, post_content: dict):
    all_temp_files = []
    try:
        mng_config = config.get("management_config", {})
        original_text, media_ids, media_type = "", [], "none"

        if post_content['type'] == 'generative':
            logging.info(f"[{task_name}] Генерация контента...")
            text_provider = mng_config.get("text_generation_provider", "openai")
            prompt = post_content.get("prompt", "")
            system_prompt = "Ты — креативный AI, который пишет интересные посты для Telegram-канала."
            generated_text = await generate_ai_text(text_provider, prompt, system_prompt)
            if not generated_text: logging.error(f"[{task_name}] Не удалось сгенерировать текст."); return False
            original_text = generated_text
            if post_content.get("image_source") == "generate":
                image_prompt = f"Создай фотореалистичное изображение для поста на тему: {prompt}"
                generated_image_path = await generate_ai_image('dalle3', image_prompt, task_name)
                if generated_image_path:
                    all_temp_files.append(generated_image_path); media_ids = [generated_image_path]; media_type = "photo"
            elif post_content.get("image_source") == "custom":
                media_ids = post_content.get("storage_message_ids", []); media_type = post_content.get("media_type", "none")
        else:
            original_text = post_content.get("text", ""); media_ids = post_content.get("storage_message_ids", []); media_type = post_content.get("media_type", "none")

        downloaded_paths = []
        if media_ids:
            if post_content.get("image_source") == "generate":
                downloaded_paths = media_ids
            else:
                try:
                    messages = await client.get_messages(STORAGE_CHANNEL_ID, media_ids)
                    for m in messages:
                        # --- ИСПРАВЛЕНИЕ: Добавляем расширение файла при скачивании ---
                        ext = ".jpg" if m.photo else ".mp4" if m.video else ""
                        file_name = str(TEMP_DIR / f"mng_{uuid.uuid4().hex}{ext}")
                        file_path = await m.download(in_memory=False, file_name=file_name)
                        downloaded_paths.append(file_path); all_temp_files.append(file_path)
                except Exception as e:
                    logging.error(f"[{task_name}] КРИТИЧЕСКАЯ ОШИБКА: Не удалось скачать медиа из хранилища (ID: {STORAGE_CHANNEL_ID}). Убедитесь, что аккаунт '{client.me.username}' является администратором в этом канале. Ошибка: {e}")
                    return False

        final_media_paths = downloaded_paths
        watermark_file = config.get('watermark_file')
        if watermark_file and os.path.exists(watermark_file) and media_type in ['photo', 'album']:
            watermarked_tasks = [apply_watermark_async(p, watermark_file) for p in downloaded_paths]
            watermarked_results = await asyncio.gather(*watermarked_tasks)
            final_media_paths = [watermarked or original for original, watermarked in zip(downloaded_paths, watermarked_results)]
            all_temp_files.extend(p for p in watermarked_results if p)

        channels = mng_config.get("target_channels", []); base_lang = channels[0]['lang'] if channels else 'ru'
        trans_provider = mng_config.get("translation_provider", "azure")
        all_successful = True
        for channel in channels:
            target_id, target_lang = channel['id'], channel['lang']
            text_to_send = original_text
            if target_lang != base_lang and original_text:
                translated_text = None
                if trans_provider != 'azure':
                    ai_prompt = f"Переведи этот текст на '{target_lang}' без каких-либо комментариев. Выдай только перевод:\n\n{original_text}"
                    translated_text = await generate_ai_text(trans_provider, ai_prompt, "Ты — профессиональный переводчик.")
                else:
                    translated_text = await azure_translate(original_text, target_lang)
                text_to_send = translated_text or original_text
            
            logging.info(f"[{task_name}] Отправляю пост в канал {target_id}...")
            
            success = False
            if media_type == 'album' and final_media_paths:
                media_group = [InputMediaPhoto(media=path, caption=text_to_send if i == 0 else "") for i, path in enumerate(final_media_paths)]
                success = await send_with_retry(client.send_media_group, target_id, media=media_group, task_name=task_name)
            elif media_type == 'photo' and final_media_paths:
                success = await send_with_retry(client.send_photo, target_id, photo=final_media_paths[0], caption=text_to_send, task_name=task_name)
            elif media_type == 'video' and final_media_paths:
                success = await send_with_retry(client.send_video, target_id, video=final_media_paths[0], caption=text_to_send, task_name=task_name)
            elif text_to_send:
                success = await send_with_retry(client.send_message, target_id, text_to_send, task_name=task_name)
            
            if success:
                logging.info(f"[{task_name}] Пост успешно отправлен в {target_id}.")
            else:
                logging.error(f"[{task_name}] НЕ УДАЛОСЬ отправить пост в {target_id}.")
                all_successful = False
                
            await asyncio.sleep(random.randint(5, 10))
        return all_successful
    finally:
        await asyncio.gather(*[remove_file_async(p) for p in all_temp_files if p])

async def execute_channel_post_job(task_name: str, content_id: str):
    logging.info(f"[{task_name}] Начинаю выполнение поста с ID: {content_id}")
    
    # Сначала читаем конфиг, чтобы убедиться, что пост существует
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config: 
        logging.error(f"[{task_name}] Не найден конфиг для выполнения задачи.")
        return

    content_pool = config.get("management_config", {}).get("content_pool", [])
    content_item = next((item for item in content_pool if item["content_id"] == content_id), None)
    
    if not content_item: 
        logging.error(f"[{task_name}] Не найден контент с ID {content_id}. Возможно, он уже был удален.")
        return

    # Создаем временный клиент для выполнения задачи
    client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
    
    try:
        await client.start()
        
        # Выполняем основную логику отправки поста
        success = await _execute_post_logic(task_name, client, config, content_item)
        
        # После выполнения, снова блокируем и обновляем файл
        async with await get_task_lock(task_name):
            config_fresh = await read_json_file(TASKS_DIR / f"{task_name}.json")
            content_pool_fresh = config_fresh.get("management_config", {}).get("content_pool", [])
            
            # --- ИСПРАВЛЕНИЕ И НОВАЯ ФУНКЦИЯ ЗДЕСЬ ---
            if success and content_item.get('schedule_type') == 'scheduled':
                # Если пост был успешно отправлен и он был одноразовым, удаляем его
                logging.info(f"[{task_name}] Пост {content_id} успешно отправлен и будет удален из очереди.")
                content_pool_fresh = [p for p in content_pool_fresh if p.get("content_id") != content_id]
            else:
                # Для повторяющихся постов просто обновляем время
                item_to_update = next((p for p in content_pool_fresh if p.get("content_id") == content_id), None)
                if item_to_update and item_to_update.get('schedule_type') == 'recurring':
                    item_to_update['last_posted'] = datetime.now().isoformat()

            config_fresh["management_config"]["content_pool"] = content_pool_fresh
            await write_json_file(TASKS_DIR / f"{task_name}.json", config_fresh)

    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в `execute_channel_post_job`: {e}", exc_info=True)
    finally:
        if client.is_connected: 
            await client.stop()

# --- Воркер и планировщик ---
async def run_channel_management_worker(task_name: str, config: dict, bot: Bot):
    worker_scheduler = AsyncIOScheduler(timezone="UTC")
    try:
        content_pool = config.get("management_config", {}).get("content_pool", [])
        for item in content_pool:
            content_id = item['content_id']
            schedule_type = item.get('schedule_type')
            if schedule_type == 'scheduled' and item.get('status', 'pending') == 'pending':
                try:
                    run_date = datetime.fromisoformat(item['run_date'])
                    now_utc = datetime.now(run_date.tzinfo)
                    if run_date > now_utc:
                        worker_scheduler.add_job(execute_channel_post_job, 'date', run_date=run_date, args=[task_name, content_id], id=content_id)
                    else: item['status'] = 'error_date_passed'
                except (ValueError, TypeError): item['status'] = 'error_invalid_date'
            elif schedule_type == 'recurring':
                interval = item['interval_hours']
                worker_scheduler.add_job(execute_channel_post_job, 'interval', hours=interval, args=[task_name, content_id], id=content_id)
        async with await get_task_lock(task_name):
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        worker_scheduler.start()
        await bot.send_message(ADMIN_IDS[0], f"✅ Воркер ведения канала <b>{task_name}</b> запущен.")
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logging.info(f"Воркер {task_name} отменен.")
    except Exception as e:
        await stop_worker(task_name, bot, f"Критическая ошибка: {e}")
    finally:
        if worker_scheduler.running: worker_scheduler.shutdown()
        
# --- FSM Обработчики: Создание Задачи ---
async def cb_manage_channel_tasks(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    buttons = []
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in sorted(task_files):
        try:
            config = await read_json_file(task_file)
            if config and config.get("task_type") == "channel_management":
                buttons.append([InlineKeyboardButton(text=f"✍️ {task_file.stem}", callback_data=f"task_view:{task_file.stem}")])
        except Exception: continue
    buttons.append([InlineKeyboardButton(text="➕ Добавить новую задачу (Ведение канала)", callback_data="channel_task_add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    await callback.message.edit_text("Управление задачами 'Ведение Канала':", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)

async def cb_channel_task_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(ChannelTaskCreation.name)
    await callback.message.edit_text("<b>Шаг 1/5: Создание задачи ведения канала</b>\n\nВведите уникальное название (имя сессии, только латиница).")
    await safe_answer_callback(callback)

async def process_channel_task_name(message: Message, state: FSMContext):
    task_name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', task_name) or (TASKS_DIR / f"{task_name}.json").exists():
        await message.answer("Ошибка: Задача с таким именем уже существует или имя некорректно."); return
    if not (SESSIONS_DIR / f"{task_name}.session").exists():
        await message.answer(f"<b>Ошибка:</b> файл сессии `sessions/{task_name}.session` не найден."); return
    await state.update_data(name=task_name)
    await state.set_state(ChannelTaskCreation.api_id)
    await message.answer("<b>Шаг 2/5:</b> Введите `api_id`.")

async def process_channel_task_api_id(message: Message, state: FSMContext):
    try: await state.update_data(api_id=int(message.text)); await state.set_state(ChannelTaskCreation.api_hash); await message.answer("<b>Шаг 3/5:</b> Введите `api_hash`.")
    except ValueError: await message.answer("API ID должен быть числом.")

async def process_channel_task_api_hash(message: Message, state: FSMContext):
    await state.update_data(api_hash=message.text.strip()); await state.set_state(ChannelTaskCreation.proxy); await message.answer("<b>Шаг 4/5:</b> Введите данные прокси (`scheme://user:pass@host:port`) или отправьте `-`.")

async def process_channel_task_proxy(message: Message, state: FSMContext):
    proxy_str, proxy_dict = message.text.strip(), None
    if proxy_str != '-':
        try: scheme, rest = proxy_str.split('://', 1); creds, host_port = rest.split('@', 1); user, password = creds.split(':', 1); host, port = host_port.split(':', 1); proxy_dict = {"scheme": scheme, "hostname": host, "port": int(port), "username": user, "password": password}
        except Exception: await message.answer("Неверный формат прокси."); return
    await state.update_data(proxy=proxy_dict); await state.set_state(ChannelTaskCreation.watermark); await message.answer("<b>Шаг 5/5:</b> Отправьте файл `.png` в качестве водяного знака или отправьте `-`.")

async def process_channel_task_watermark(message: Message, state: FSMContext):
    if message.text and message.text.strip() == '-': await state.update_data(watermark_file=None)
    elif message.document and 'image/png' in message.document.mime_type:
        data = await state.get_data(); file_path = WATERMARKS_DIR / f"{data['name']}.png"
        await bot_instance.download(message.document, destination=file_path); await state.update_data(watermark_file=str(file_path))
    else: await message.answer("Ошибка: отправьте .png как ДОКУМЕНТ или отправьте `-`."); return
    await state.update_data(target_channels=[])
    await state.set_state(ChannelTaskCreation.get_main_channel_id)
    await message.answer("<b>Настройка основного канала (1/3):</b>\n\nВведите ID целевого канала (например, `-100123...`).")

async def process_channel_id(message: Message, state: FSMContext, is_main: bool):
    try: channel_id = int(message.text.strip()); await state.update_data(current_channel_id=channel_id); next_state = ChannelTaskCreation.get_main_channel_link if is_main else ChannelTaskCreation.get_extra_channel_link; await state.set_state(next_state); await message.answer("<b>Настройка канала (2/3):</b>\n\nВведите публичную ссылку на канал (например, `@channel` или `t.me/channel`).")
    except ValueError: await message.answer("ID должен быть числом.")

async def process_channel_link(message: Message, state: FSMContext, is_main: bool):
    await state.update_data(current_channel_link=message.text.strip()); next_state = ChannelTaskCreation.get_main_channel_lang if is_main else ChannelTaskCreation.get_extra_channel_lang; await state.set_state(next_state); await message.answer("<b>Настройка канала (3/3):</b>\n\nВведите язык канала (двухбуквенный код, например, `ru`, `en`).")

async def process_channel_lang(message: Message, state: FSMContext, is_main: bool):
    lang = message.text.strip().lower()
    if not re.match(r'^[a-z]{2,3}$', lang): await message.answer("Неверный формат языка. Введите двухбуквенный код."); return
    data = await state.get_data(); current_channels = data.get('target_channels', [])
    current_channels.append({"id": data['current_channel_id'], "link": data['current_channel_link'], "lang": lang})
    await state.update_data(target_channels=current_channels)
    if is_main:
        await state.set_state(ChannelTaskCreation.ask_for_another_channel)
        await message.answer("✅ Основной канал добавлен.\n\nХотите добавить еще один канал на другом языке?", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Да", callback_data="add_extra_channel:yes"), InlineKeyboardButton(text="❌ Нет, завершить", callback_data="add_extra_channel:no")]]))
    else: await finalize_channel_task_creation(message, state)

async def cb_ask_for_another_channel(callback: CallbackQuery, state: FSMContext):
    if callback.data.split(":")[1] == 'yes': await state.set_state(ChannelTaskCreation.get_extra_channel_id); await callback.message.edit_text("<b>Настройка доп. канала (1/3):</b>\n\nВведите ID канала.")
    else: await finalize_channel_task_creation(callback.message, state)
    await safe_answer_callback(callback)

async def finalize_channel_task_creation(message: Message, state: FSMContext):
    await message.answer("Сохраняю задачу...")
    data = await state.get_data()
    management_config = {"target_channels": data['target_channels'], "content_pool": [], "translation_provider": "azure", "text_generation_provider": "openai"}
    task_config = {
        "task_name": data['name'], "task_type": "channel_management",
        "api_id": data['api_id'], "api_hash": data['api_hash'], "status": "inactive", "last_error": None,
        "proxy": data.get('proxy'), "watermark_file": data.get('watermark_file'),
        "management_config": management_config
    }
    await write_json_file(TASKS_DIR / f"{data['name']}.json", task_config)
    await state.clear()
    await message.answer(f"✅ Задача ведения канала <b>{data['name']}</b> успешно создана!", reply_markup=main_menu_keyboard())

# --- FSM Обработчики: Редактирование ---
async def cb_edit_translator_menu(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Azure (Стандартный)", callback_data=f"channel_set_translator:{task_name}:azure")],
        [InlineKeyboardButton(text="AI: OpenAI", callback_data=f"channel_set_translator:{task_name}:openai")],
        [InlineKeyboardButton(text="AI: Gemini", callback_data=f"channel_set_translator:{task_name}:gemini")],
        [InlineKeyboardButton(text="AI: Claude", callback_data=f"channel_set_translator:{task_name}:claude")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_edit:{task_name}")]
    ])
    await callback.message.edit_text("Выберите оператора для перевода:", reply_markup=kb)
    await safe_answer_callback(callback)

async def cb_set_translator(callback: CallbackQuery, state: FSMContext):
    _, task_name, provider = callback.data.split(":")
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        config["management_config"]["translation_provider"] = provider
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    fake_callback = callback_query_from_message(callback.message, f"task_edit:{task_name}")
    await cb_task_edit(fake_callback, state)
    await safe_answer_callback(callback, f"Оператор перевода изменен на: {provider.title()}")

async def cb_edit_text_gen_menu(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="OpenAI", callback_data=f"channel_set_text_gen:{task_name}:openai")],
        [InlineKeyboardButton(text="Gemini", callback_data=f"channel_set_text_gen:{task_name}:gemini")],
        [InlineKeyboardButton(text="Claude", callback_data=f"channel_set_text_gen:{task_name}:claude")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_edit:{task_name}")]
    ])
    await callback.message.edit_text("Выберите AI для генерации текстов:", reply_markup=kb)
    await safe_answer_callback(callback)

async def cb_set_text_gen(callback: CallbackQuery, state: FSMContext):
    _, task_name, provider = callback.data.split(":")
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        config["management_config"]["text_generation_provider"] = provider
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    fake_callback = callback_query_from_message(callback.message, f"task_edit:{task_name}")
    await cb_task_edit(fake_callback, state)
    await safe_answer_callback(callback, f"AI для генерации текстов изменен на: {provider.title()}")

async def cb_edit_channels_menu(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    channels = config.get("management_config", {}).get("target_channels", [])
    buttons = []
    text = f"<b>Редактирование каналов для '{task_name}':</b>\n\n"
    for i, ch in enumerate(channels):
        text += f"{i+1}. Канал ID: <code>{ch['id']}</code> (язык: {ch['lang']})\n"
        buttons.append([InlineKeyboardButton(text=f"🗑️ Удалить канал {i+1}", callback_data=f"channel_delete_channel:{task_name}:{i}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить новый канал", callback_data=f"channel_add_new_channel:{task_name}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_edit:{task_name}")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)

async def cb_delete_channel(callback: CallbackQuery, state: FSMContext):
    _, task_name, index_str = callback.data.split(":")
    index = int(index_str)
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        channels = config["management_config"]["target_channels"]
        if 0 <= index < len(channels):
            if len(channels) == 1:
                await safe_answer_callback(callback, "Нельзя удалить последний канал!", show_alert=True); return
            deleted_channel = channels.pop(index)
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
            await safe_answer_callback(callback, f"Канал {deleted_channel['id']} удален.")
        else:
            await safe_answer_callback(callback, "Ошибка: неверный индекс канала.", show_alert=True); return
    await cb_edit_channels_menu(callback, state)

async def cb_add_new_channel(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(ChannelTaskEditing.get_new_channel_id)
    await state.update_data(task_name=task_name)
    await callback.message.edit_text("<b>Добавление нового канала (1/3):</b>\n\nВведите ID нового канала.")
    await safe_answer_callback(callback)

async def process_new_channel_id(message: Message, state: FSMContext):
    try:
        channel_id = int(message.text.strip())
        await state.update_data(new_channel_id=channel_id)
        await state.set_state(ChannelTaskEditing.get_new_channel_link)
        await message.answer("<b>Добавление нового канала (2/3):</b>\n\nВведите публичную ссылку.")
    except ValueError: await message.answer("ID должен быть числом.")

async def process_new_channel_link(message: Message, state: FSMContext):
    await state.update_data(new_channel_link=message.text.strip())
    await state.set_state(ChannelTaskEditing.get_new_channel_lang)
    await message.answer("<b>Добавление нового канала (3/3):</b>\n\nВведите язык (например, `en`).")

async def process_new_channel_lang(message: Message, state: FSMContext):
    lang = message.text.strip().lower()
    if not re.match(r'^[a-z]{2,3}$', lang): await message.answer("Неверный формат языка."); return
    data = await state.get_data(); task_name = data['task_name']
    new_channel = {"id": data['new_channel_id'], "link": data['new_channel_link'], "lang": lang}
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        config["management_config"]["target_channels"].append(new_channel)
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear()
    await message.answer("✅ Новый канал добавлен!")
    fake_callback_query = callback_query_from_message(message, f"channel_edit_channels:{task_name}")
    await cb_edit_channels_menu(fake_callback_query, state)

# --- FSM Обработчики: Управление контентом ---
async def cb_manage_content_pool(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await callback.message.edit_text(f"Управление контентом для задачи <b>{task_name}</b>:", reply_markup=channel_content_pool_keyboard(task_name))
    await safe_answer_callback(callback)

async def cb_add_content_menu(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓️ Запланированный", callback_data=f"channel_add_content:{task_name}:scheduled")],
        [InlineKeyboardButton(text="🔄 Повторяющийся", callback_data=f"channel_add_content:{task_name}:recurring")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"channel_manage_content:{task_name}")]
    ])
    await callback.message.edit_text("Выберите тип обычного поста:", reply_markup=kb)
    await safe_answer_callback(callback)
    
async def cb_add_content(callback: CallbackQuery, state: FSMContext):
    _, task_name, schedule_type = callback.data.split(":")
    await state.set_state(ContentPoolManagement.get_content_type)
    await state.update_data(task_name=task_name, content_type="regular", schedule_type=schedule_type)
    await callback.message.edit_text("Пришлите контент для поста (текст, фото, видео, альбом).")
    await safe_answer_callback(callback)

async def process_content_for_pool(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get('content_type') == 'generative':
        await process_generative_custom_image(message, state); return
    if message.media_group_id: await state.set_state(ContentPoolManagement.confirm_album); await handle_album_message(message, state, "pool_confirm_album"); return
    try:
        content_data = {"text": "", "media_type": "none", "storage_message_ids": []}
        if message.text: content_data.update({"text": message.text})
        elif message.photo or message.video:
            forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, message.chat.id, message.message_id)
            content_data.update({"text": message.caption or "", "media_type": message.content_type, "storage_message_ids": [forwarded.message_id]})
        await state.update_data(content_to_add=content_data)
        await state.set_state(ContentPoolManagement.get_schedule_details_value)
        prompt = "Введите дату `ГГГГ-ММ-ДД ЧЧ:ММ`." if data['schedule_type'] == 'scheduled' else "Введите интервал в часах (например, `24`)."
        await message.answer(f"Контент сохранен.\n\n{prompt}")
    except Exception as e: await message.answer(f"Ошибка сохранения: {e}"); await state.clear()

async def cb_confirm_pool_album(callback: CallbackQuery, state: FSMContext):
    _, action, media_group_id = callback.data.split(":"); album_data = TEMP_MEDIA_GROUPS.pop(media_group_id, None)
    if not album_data: await safe_answer_callback(callback, "Время ожидания истекло.", show_alert=True); return
    if action == "no": await state.set_state(ContentPoolManagement.get_content_type); await callback.message.edit_text("Альбом отменен."); return
    try:
        media_ids, text = [], next((msg.caption for msg in album_data['messages'] if msg.caption), "")
        for msg in album_data['messages']: forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, msg.chat.id, msg.message_id); media_ids.append(forwarded.message_id)
        content_data = {"text": text, "media_type": "album", "storage_message_ids": media_ids}
        data = await state.get_data()
        await state.update_data(content_to_add=content_data)
        await state.set_state(ContentPoolManagement.get_schedule_details_value)
        prompt = "Введите дату `ГГГГ-ММ-ДД ЧЧ:ММ`." if data['schedule_type'] == 'scheduled' else "Введите интервал в часах."
        await callback.message.edit_text(f"Альбом сохранен.\n\n{prompt}")
    except Exception as e: await safe_answer_callback(callback, f"Ошибка сохранения: {e}", show_alert=True); await state.set_state(ContentPoolManagement.get_content_type); await callback.message.edit_text("Пришлите сообщение заново.")
    await safe_answer_callback(callback)

async def process_schedule_details(message: Message, state: FSMContext):
    data = await state.get_data()
    task_name, content_type, schedule_type, details = data['task_name'], data['content_type'], data['schedule_type'], message.text.strip()
    content_item = data.get('content_to_add', {})
    if content_type == 'generative':
        content_item['prompt'] = data.get('prompt')
        content_item['image_source'] = data.get('image_source')
    content_item.update({"content_id": str(uuid.uuid4()), "type": content_type, "schedule_type": schedule_type})
    try:
        if schedule_type == 'scheduled': content_item["run_date"] = datetime.strptime(details, "%Y-%m-%d %H:%M").isoformat(); content_item["status"] = "pending"
        else: content_item["interval_hours"] = int(details); content_item["last_posted"] = None
    except ValueError: await message.answer("Неверный формат."); return
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        config["management_config"]["content_pool"].append(content_item)
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    await state.clear()
    await message.answer("✅ Пост добавлен в очередь!", reply_markup=channel_content_pool_keyboard(task_name))

async def cb_add_generative_post(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(ContentPoolManagement.get_content_prompt)
    await state.update_data(task_name=task_name, content_type="generative")
    await callback.message.edit_text("Введите промпт для генерации текста поста:")
    await safe_answer_callback(callback)

async def process_generative_prompt(message: Message, state: FSMContext):
    await state.update_data(prompt=message.text)
    await state.set_state(ContentPoolManagement.ask_for_image_type)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼️ Загрузить свою", callback_data="gen_image_type:custom")],
        [InlineKeyboardButton(text="✨ Сгенерировать AI", callback_data="gen_image_type:generate")],
        [InlineKeyboardButton(text="❌ Без картинки", callback_data="gen_image_type:none")]
    ])
    await message.answer("Отлично. Теперь выберите источник изображения:", reply_markup=kb)

async def cb_handle_image_choice(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":")[1]
    await state.update_data(image_source=choice)
    if choice == "custom":
        await state.set_state(ContentPoolManagement.get_custom_image)
        await callback.message.edit_text("Пришлите ваше изображение или альбом.")
    else:
        await state.set_state(ContentPoolManagement.get_schedule_details_type)
        text = "Изображение будет сгенерировано." if choice == "generate" else "Пост будет без изображения."
        await callback.message.edit_text(f"{text}\n\nТеперь выберите тип публикации:", reply_markup=get_schedule_type_keyboard())
    await safe_answer_callback(callback)
    
async def process_generative_custom_image(message: Message, state: FSMContext):
    if message.media_group_id: await state.set_state(ContentPoolManagement.confirm_album); await handle_album_message(message, state, "pool_confirm_album"); return
    try:
        content_data = {"text": "", "media_type": "none", "storage_message_ids": []}
        if message.photo or message.video:
            forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, message.chat.id, message.message_id)
            content_data.update({"text": message.caption or "", "media_type": message.content_type, "storage_message_ids": [forwarded.message_id]})
        await state.update_data(content_to_add=content_data)
        await state.set_state(ContentPoolManagement.get_schedule_details_type)
        await message.answer("Изображение сохранено.\n\nТеперь выберите тип публикации:", reply_markup=get_schedule_type_keyboard())
    except Exception as e: await message.answer(f"Ошибка сохранения: {e}"); await state.clear()

def get_schedule_type_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗓️ Запланировать", callback_data="gen_schedule_type:scheduled")],
        [InlineKeyboardButton(text="🔄 Повторять", callback_data="gen_schedule_type:recurring")]
    ])

async def cb_get_schedule_type(callback: CallbackQuery, state: FSMContext):
    schedule_type = callback.data.split(":")[1]
    await state.update_data(schedule_type=schedule_type)
    await state.set_state(ContentPoolManagement.get_schedule_details_value)
    prompt = "Введите дату `ГГГГ-ММ-ДД ЧЧ:ММ`." if schedule_type == 'scheduled' else "Введите интервал в часах (например, `24`)."
    await callback.message.edit_text(prompt)
    await safe_answer_callback(callback)

async def cb_post_now(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(ContentPoolManagement.get_immediate_post_content)
    await state.update_data(task_name=task_name)
    await callback.message.edit_text("Пришлите контент для немедленной публикации.")
    await safe_answer_callback(callback)

async def process_immediate_post_content(message: Message, state: FSMContext):
    await message.answer("Отправляю пост, пожалуйста, подождите...")
    data = await state.get_data(); task_name = data['task_name']
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config: await message.answer("Ошибка: не найдена конфигурация задачи."); await state.clear(); return
    content_data = {"type": "immediate", "text": "", "media_type": "none", "storage_message_ids": []}
    if message.text: content_data.update({"text": message.text})
    elif message.photo or message.video:
        forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, message.chat.id, message.message_id)
        content_data.update({"text": message.caption or "", "media_type": message.content_type, "storage_message_ids": [forwarded.message_id]})
    client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
    try:
        await client.start()
        logging.info(f"[{task_name}] Обновляю список диалогов для сессии перед отправкой...")
        async for dialog in client.get_dialogs(): pass
        logging.info(f"[{task_name}] Список диалогов обновлен.")
        
        success = await _execute_post_logic(task_name, client, config, content_data)
        if success:
            await message.answer("✅ Пост успешно опубликован!")
        else:
            await message.answer("❌ Произошла ошибка при публикации. Проверьте лог для деталей.")

    except Exception as e:
        await message.answer(f"❌ Произошла критическая ошибка при публикации: {e}")
    finally:
        if client.is_connected: await client.stop()
        await state.clear()

# ЗАМЕНИТЬ ЭТУ ФУНКЦИЮ
async def cb_view_content_pool(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    content_pool = config.get("management_config", {}).get("content_pool", [])
    if not content_pool:
        await safe_answer_callback(callback, "В этой задаче еще нет постов.", show_alert=True)
        return

    text = f"<b>📋 Посты в задаче '{task_name}':</b>\n\n"
    buttons = []

    for item in content_pool:
        ctype = "✨ Генер." if item.get('type') == 'generative' else "📝 Обыч."
        schedule_type = item.get('schedule_type')

        schedule_info = ""
        if schedule_type == 'scheduled':
            try:
                dt = datetime.fromisoformat(item.get('run_date', ''))
                schedule_info = f"🗓️ {dt.strftime('%d.%m %H:%M')}"
            except (ValueError, TypeError):
                schedule_info = "🗓️ Ошибка даты"
        elif schedule_type == 'recurring':
            schedule_info = f"🔄 {item.get('interval_hours', 'N/A')}ч"

        # Используем сокращенный ID для callback_data
        short_id = item['content_id'][:8]
        button_text = f"{ctype} {schedule_info}"

        # Ограничиваем длину текста на кнопке, чтобы избежать ошибок
        max_len = 40
        if len(button_text) > max_len:
            button_text = button_text[:max_len-3] + "..."

        buttons.append([InlineKeyboardButton(
            text=button_text, 
            callback_data=f"c_vp:{task_name}:{short_id}" # Сокращенный префикс и ID
        )])

    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"channel_manage_content:{task_name}")])

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)

# ЗАМЕНИТЬ ЭТУ ФУНКЦИЮ
async def cb_view_post(callback: CallbackQuery, state: FSMContext):
    _, task_name, short_id = callback.data.split(":")
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    content_pool = config.get("management_config", {}).get("content_pool", [])

    # Ищем пост по началу ID
    item = next((p for p in content_pool if p["content_id"].startswith(short_id)), None)

    if not item:
        await safe_answer_callback(callback, "Пост не найден (возможно, был удален).", show_alert=True)
        await cb_view_content_pool(callback, state) # Возвращаем к списку
        return

    full_content_id = item['content_id']
    text = f"<b>Просмотр поста (ID: {full_content_id[:8]}):</b>\n\n"
    post_text = item.get("text") or item.get("prompt") or "Нет текста"

    # Обрезаем текст для предпросмотра, если он слишком длинный
    if len(post_text) > 3000:
        post_text = post_text[:3000] + "\n\n...(текст слишком длинный для предпросмотра)..."

    text += post_text

    buttons = [
        [InlineKeyboardButton(text="🗑️ Удалить", callback_data=f"c_del_p:{task_name}:{short_id}")],
        [InlineKeyboardButton(text="⬅️ Назад к списку", callback_data=f"channel_view_content:{task_name}")]
    ]

    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)


# ЗАМЕНИТЬ ЭТУ ФУНКЦИЮ
async def cb_delete_post(callback: CallbackQuery, state: FSMContext):
    _, task_name, short_id = callback.data.split(":")

    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        content_pool = config.get("management_config", {}).get("content_pool", [])

        initial_len = len(content_pool)
        # Удаляем пост, находя его по сокращенному ID
        content_pool = [item for item in content_pool if not item["content_id"].startswith(short_id)]

        if len(content_pool) < initial_len:
            config["management_config"]["content_pool"] = content_pool
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
            await safe_answer_callback(callback, "Пост удален.")
        else:
            await safe_answer_callback(callback, "Не удалось найти пост для удаления.", show_alert=True)

    # Возвращаемся к обновленному списку постов
    fake_callback = callback_query_from_message(callback.message, f"channel_view_content:{task_name}")
    await cb_view_content_pool(fake_callback, state)

def callback_query_from_message(message: Message, new_data: str) -> CallbackQuery:
    return CallbackQuery(id=str(uuid.uuid4()), from_user=message.from_user, chat_instance=str(uuid.uuid4()), message=message, data=new_data)
    
# --- Регистрация обработчиков ---
# --- Регистрация обработчиков ---
def register_handlers(router: Router, bot: Bot):
    global bot_instance, cb_task_edit
    bot_instance = bot
    # Позволяет этому модулю вызывать cb_task_edit из main.py для обновления меню
    try:
        from main import cb_task_edit as main_cb_task_edit
        cb_task_edit = main_cb_task_edit
    except ImportError:
        logging.warning("Не удалось импортировать cb_task_edit из main.py. Циклический импорт?")
        cb_task_edit = None

    # Управление задачами (создание, просмотр списка)
    router.callback_query.register(cb_manage_channel_tasks, F.data == "manage_channel_tasks", AdminFilter())
    router.callback_query.register(cb_channel_task_add, F.data == "channel_task_add", AdminFilter())

    # FSM: Создание задачи
    router.message.register(process_channel_task_name, ChannelTaskCreation.name, AdminFilter())
    router.message.register(process_channel_task_api_id, ChannelTaskCreation.api_id, AdminFilter())
    router.message.register(process_channel_task_api_hash, ChannelTaskCreation.api_hash, AdminFilter())
    router.message.register(process_channel_task_proxy, ChannelTaskCreation.proxy, AdminFilter())
    router.message.register(process_channel_task_watermark, F.document | (F.text == "-"), ChannelTaskCreation.watermark, AdminFilter())
    
    # FSM: Создание задачи (добавление каналов)
    async def handle_main_channel_id(message: Message, state: FSMContext): await process_channel_id(message, state, is_main=True)
    async def handle_main_channel_link(message: Message, state: FSMContext): await process_channel_link(message, state, is_main=True)
    async def handle_main_channel_lang(message: Message, state: FSMContext): await process_channel_lang(message, state, is_main=True)
    async def handle_extra_channel_id(message: Message, state: FSMContext): await process_channel_id(message, state, is_main=False)
    async def handle_extra_channel_link(message: Message, state: FSMContext): await process_channel_link(message, state, is_main=False)
    async def handle_extra_channel_lang(message: Message, state: FSMContext): await process_channel_lang(message, state, is_main=False)
    
    router.message.register(handle_main_channel_id, ChannelTaskCreation.get_main_channel_id, AdminFilter())
    router.message.register(handle_main_channel_link, ChannelTaskCreation.get_main_channel_link, AdminFilter())
    router.message.register(handle_main_channel_lang, ChannelTaskCreation.get_main_channel_lang, AdminFilter())
    router.callback_query.register(cb_ask_for_another_channel, F.data.startswith("add_extra_channel:"), ChannelTaskCreation.ask_for_another_channel, AdminFilter())
    router.message.register(handle_extra_channel_id, ChannelTaskCreation.get_extra_channel_id, AdminFilter())
    router.message.register(handle_extra_channel_link, ChannelTaskCreation.get_extra_channel_link, AdminFilter())
    router.message.register(handle_extra_channel_lang, ChannelTaskCreation.get_extra_channel_lang, AdminFilter())

    # Управление контентом (главное меню)
    router.callback_query.register(cb_manage_content_pool, F.data.startswith("channel_manage_content:"), AdminFilter())

    # FSM: Добавление контента (обычного и генеративного)
    router.callback_query.register(cb_add_content, F.data.startswith("channel_add_content:"), AdminFilter())
    router.message.register(process_content_for_pool, ContentPoolManagement.get_content_type, F.content_type.in_({'text', 'photo', 'video'}), AdminFilter())
    router.callback_query.register(cb_confirm_pool_album, F.data.startswith("pool_confirm_album:"), ContentPoolManagement.confirm_album, AdminFilter())
    router.message.register(process_schedule_details, ContentPoolManagement.get_schedule_details_value, AdminFilter())
    
    # FSM: Добавление генеративного контента
    router.callback_query.register(cb_add_content_menu, F.data.startswith("channel_add_content_menu:"), AdminFilter())
    router.callback_query.register(cb_add_generative_post, F.data.startswith("channel_add_generative:"), AdminFilter())
    router.message.register(process_generative_prompt, ContentPoolManagement.get_content_prompt, AdminFilter())
    router.callback_query.register(cb_handle_image_choice, F.data.startswith("gen_image_type:"), ContentPoolManagement.ask_for_image_type, AdminFilter())
    router.message.register(process_generative_custom_image, ContentPoolManagement.get_custom_image, F.content_type.in_({'photo', 'video'}), AdminFilter())
    router.callback_query.register(cb_get_schedule_type, F.data.startswith("gen_schedule_type:"), ContentPoolManagement.get_schedule_details_type, AdminFilter())

    # Просмотр и удаление контента (с исправленными колбэками)
    router.callback_query.register(cb_view_content_pool, F.data.startswith("channel_view_content:"), AdminFilter())
    router.callback_query.register(cb_view_post, F.data.startswith("c_vp:"), AdminFilter())
    router.callback_query.register(cb_delete_post, F.data.startswith("c_del_p:"), AdminFilter())

    # Постинг "сейчас"
    router.callback_query.register(cb_post_now, F.data.startswith("channel_post_now:"), AdminFilter())
    router.message.register(process_immediate_post_content, ContentPoolManagement.get_immediate_post_content, F.content_type.in_({'text', 'photo', 'video'}), AdminFilter())

    # Редактирование задачи (меню и обработчики)
    router.callback_query.register(cb_edit_translator_menu, F.data.startswith("channel_edit_translator_menu:"), AdminFilter())
    router.callback_query.register(cb_set_translator, F.data.startswith("channel_set_translator:"), AdminFilter())
    router.callback_query.register(cb_edit_text_gen_menu, F.data.startswith("channel_edit_text_gen_menu:"), AdminFilter())
    router.callback_query.register(cb_set_text_gen, F.data.startswith("channel_set_text_gen:"), AdminFilter())
    router.callback_query.register(cb_edit_channels_menu, F.data.startswith("channel_edit_channels:"), AdminFilter())
    router.callback_query.register(cb_delete_channel, F.data.startswith("channel_delete_channel:"), AdminFilter())
    
    # FSM: Редактирование (добавление нового канала)
    router.callback_query.register(cb_add_new_channel, F.data.startswith("channel_add_new_channel:"), AdminFilter())
    router.message.register(process_new_channel_id, ChannelTaskEditing.get_new_channel_id, AdminFilter())
    router.message.register(process_new_channel_link, ChannelTaskEditing.get_new_channel_link, AdminFilter())
    router.message.register(process_new_channel_lang, ChannelTaskEditing.get_new_channel_lang, AdminFilter())