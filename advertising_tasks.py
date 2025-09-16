# advertising_tasks.py

import asyncio
import logging
import random
import os
import uuid
import re
from datetime import datetime, timedelta, time, timezone
from typing import Union

# Aiogram и связанные с ним импорты
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.client.bot import Bot

# Pyrogram и планировщик
from pyrogram import Client, filters
from pyrogram.enums import ChatType
from pyrogram.errors import RPCError, FloodWait
from pyrogram.types import InputMediaPhoto, Message as PyrogramMessage
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# --- Импорты из нашего файла utils.py ---
from utils import (
    TASKS_DIR, SESSIONS_DIR, WATERMARKS_DIR, TEMP_DIR, ADMIN_IDS, ACTIVE_CLIENTS,
    STORAGE_CHANNEL_ID, ALBUM_COLLECTION_DELAY,
    write_json_file, read_json_file, get_task_lock, remove_file_async,
    apply_watermark_async, confirm_album_keyboard, AdminFilter, TEMP_MEDIA_GROUPS,
    stop_worker, main_menu_keyboard, send_with_retry, safe_answer_callback
)

# Глобальная переменная для экземпляра бота
bot_instance: Union[Bot, None] = None

# --- FSM для создания, редактирования и тестирования ---
class AdvertisingTaskCreation(StatesGroup):
    name, api_id, api_hash, proxy, watermark, sends_per_hour, target_groups = [State() for _ in range(7)]
    get_message_variants = State()
    confirm_album = State()
    ask_for_more_variants = State()

class AdvertisingTaskEditing(StatesGroup):
    get_new_sends_per_hour = State()
    add_group_id = State()
    get_new_message_variants = State()
    confirm_new_album = State()
    ask_for_more_new_variants = State()
    get_funnel_message = State()

class AdvertisingTaskTesting(StatesGroup):
    get_test_group_id = State()

# --- Логика воркера и выполнения задач ---
async def send_advert_job(task_name: str, config: dict, target_groups_override: list = None):
    mode = "ТЕСТ" if target_groups_override else "РАССЫЛКА"
    logging.info(f"[{task_name}] Начинаю рекламную {mode.lower()}.")

    client = ACTIVE_CLIENTS.get(task_name)
    is_temp_client = False
    if not client or not client.is_connected:
        logging.warning(f"[{task_name}] Активный клиент не найден, создаю временный.")
        client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
        is_temp_client = True

    all_temp_files = []
    sent_count, failed_count = 0, 0

    try:
        if is_temp_client: await client.start()
        
        logging.info(f"[{task_name}] Обновляю список диалогов для рекламной задачи...")
        async for _ in client.get_dialogs(): pass
        logging.info(f"[{task_name}] Список диалогов обновлен.")

        adv_config = config.get("advertising_config", {})
        variants = adv_config.get("message_variants", [])
        if not variants:
            logging.error(f"[{task_name}] Нет вариантов сообщений для рассылки.")
            if not target_groups_override:
                await stop_worker(task_name, bot_instance, "Нет вариантов сообщений для рассылки.")
            return 0, 0

        chosen_variant = random.choice(variants)
        text_to_send = chosen_variant.get("text", "")
        media_files = chosen_variant.get("media_files", [])
        media_type = chosen_variant.get("media_type", "none")
        watermark_file = config.get('watermark_file')

        media_to_send = []
        if media_type != "none":
            messages_from_storage = await client.get_messages(STORAGE_CHANNEL_ID, media_files)
            downloaded_paths = []
            for m in messages_from_storage:
                ext = ".jpg" if m.photo else ".mp4" if m.video else ""
                file_path = await m.download(in_memory=False, file_name=str(TEMP_DIR / f"adv_{uuid.uuid4().hex}{ext}"))
                downloaded_paths.append(file_path)
                all_temp_files.append(file_path)

            final_paths = downloaded_paths
            if watermark_file and os.path.exists(watermark_file) and media_type in ['photo', 'album']:
                watermarked_paths_tasks = [apply_watermark_async(path, watermark_file) for path in downloaded_paths]
                watermarked_paths = await asyncio.gather(*watermarked_paths_tasks)
                final_paths = [watermarked or original for original, watermarked in zip(downloaded_paths, watermarked_paths)]
                all_temp_files.extend(p for p in watermarked_paths if p)

            if media_type == 'album':
                media_to_send.extend(InputMediaPhoto(media=path, caption=text_to_send if i == 0 else "") for i, path in enumerate(final_paths))
            elif media_type in ['photo', 'video'] and final_paths:
                media_to_send = final_paths[0]
        
        target_group_ids = adv_config.get('target_group_ids', [])
        if target_groups_override is not None:
             groups_to_send = target_groups_override
        elif target_group_ids:
             groups_to_send = target_group_ids
        else:
             groups_to_send = [dialog.chat.id async for dialog in client.get_dialogs() if dialog.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]]

        logging.info(f"[{task_name}] Найдено {len(groups_to_send)} групп. Начинаю отправку...")

        for i, group_id in enumerate(groups_to_send):
            success = await send_with_retry(
                client=client,
                media_type=media_type,
                chat_id=group_id,
                text=text_to_send,
                media=media_to_send,
                task_name=task_name
            )

            if success:
                sent_count += 1
                logging.info(f"[{task_name}] Сообщение отправлено в группу {group_id}.")
                if len(groups_to_send) > 1 and i < len(groups_to_send) - 1:
                    delay = random.randint(45, 120)
                    logging.info(f"[{task_name}] Пауза на {delay} секунд...")
                    await asyncio.sleep(delay)
            else:
                failed_count += 1
        
        if mode == "РАССЫЛКА" and sent_count > 0:
            async with await get_task_lock(f"{task_name}_stats"):
                stats_file = TASKS_DIR / f"{task_name}_stats.json"
                stats_data = await read_json_file(stats_file) or {}
                today = str(datetime.now().date())
                stats_data.setdefault(today, {"sent_messages": 0})["sent_messages"] += sent_count
                await write_json_file(stats_file, stats_data)

        logging.info(f"[{task_name}] {mode} завершена. Успешно: {sent_count}, Ошибки: {failed_count}.")
        return sent_count, failed_count
    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в `send_advert_job`: {e}", exc_info=True)
        return 0, 0
    finally:
        if is_temp_client and client and client.is_connected:
            await client.stop()
        await asyncio.gather(*[remove_file_async(p) for p in all_temp_files if p])

async def hourly_scheduler_job(task_name: str, config: dict, scheduler: AsyncIOScheduler):
    logging.info(f"[{task_name}] Запуск ежечасного планировщика рекламных рассылок.")
    try:
        now = datetime.now(timezone.utc)
        
        for job in scheduler.get_jobs():
            if job.id.startswith(f"{task_name}_adv_") and job.next_run_time < now:
                job.remove()

        adv_config = config.get('advertising_config', {})
        sends_per_hour = adv_config.get('sends_per_hour', 1)
        if sends_per_hour > 0:
            interval_seconds = 3600 // sends_per_hour
            for i in range(sends_per_hour):
                delay_seconds = (i * interval_seconds) + random.randint(1, interval_seconds)
                run_date = now + timedelta(seconds=delay_seconds)
                scheduler.add_job(send_advert_job, 'date', run_date=run_date, args=[task_name, config, None], id=f"{task_name}_adv_{uuid.uuid4().hex}")

        logging.info(f"[{task_name}] Запланировано {sends_per_hour} рекламных рассылок на ближайший час.")
    except Exception as e:
        logging.error(f"[{task_name}] Ошибка в ежечасном планировщике рекламы: {e}", exc_info=True)

async def daily_summary_report():
    logging.info("Подготовка ежедневного сводного отчета по рекламным задачам...")
    report_lines = [f"📊 <b>Ежедневный отчет по рекламным рассылкам за {datetime.now().strftime('%d.%m.%Y')}</b>\n"]
    
    active_ad_tasks = []
    for task_name, task_process in ACTIVE_TASKS.items():
        if task_process and not task_process.done():
            config = await read_json_file(TASKS_DIR / f"{task_name}.json")
            if config and config.get("task_type") == "advertising":
                active_ad_tasks.append(task_name)
    
    if not active_ad_tasks:
        logging.info("Нет активных рекламных задач для отчета.")
        return

    total_sent_today = 0
    for task_name in active_ad_tasks:
        stats_file = TASKS_DIR / f"{task_name}_stats.json"
        stats_data = await read_json_file(stats_file)
        today_str = str(datetime.now().date())
        
        sent_today = 0
        if stats_data and today_str in stats_data:
            sent_today = stats_data[today_str].get("sent_messages", 0)
        
        report_lines.append(f"• <b>{task_name}</b>: отправлено {sent_today} сообщений.")
        total_sent_today += sent_today
        
    report_lines.append(f"\n📈 <b>Всего за сутки:</b> {total_sent_today} сообщений.")
    
    full_report = "\n".join(report_lines)
    
    try:
        await bot_instance.send_message(5718511608, full_report)
        logging.info("Ежедневный сводный отчет успешно отправлен.")
    except Exception as e:
        logging.error(f"Не удалось отправить ежедневный сводный отчет: {e}", exc_info=True)

async def run_advertising_worker(task_name: str, config: dict, bot: Bot):
    pyrogram_client, worker_scheduler = None, AsyncIOScheduler(timezone="UTC")
    try:
        pyrogram_client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))

        @pyrogram_client.on_message(filters.private & ~filters.me)
        async def on_private_message(client: Client, pyro_message: PyrogramMessage):
            current_config = await read_json_file(TASKS_DIR / f"{task_name}.json")
            funnel_config = current_config.get("advertising_config", {}).get("funnel", {})

            if funnel_config.get("enabled") and funnel_config.get("message"):
                history = [h async for h in client.get_chat_history(pyro_message.from_user.id, limit=2)]
                if len(history) <= 1:
                    logging.info(f"[{task_name}] Новое ЛС от {pyro_message.from_user.id}. Сработала воронка.")
                    await client.send_message(pyro_message.from_user.id, funnel_config["message"])
                    return

            target_user_id = 5718511608
            logging.info(f"[{task_name}] Получено личное сообщение от {pyro_message.from_user.id}. Уведомляю администратора.")
            notification_text = (
                f"❗️ На аккаунт рекламной задачи <b>{task_name}</b> пришло личное сообщение.\n\n"
                f"От: {pyro_message.from_user.first_name} (@{pyro_message.from_user.username or 'N/A'})\n"
                f"ID: <code>{pyro_message.from_user.id}</code>"
            )
            try:
                await bot.send_message(target_user_id, notification_text)
            except Exception as e:
                logging.error(f"[{task_name}] Не удалось отправить уведомление о ЛС: {e}")

        await pyrogram_client.start()
        ACTIVE_CLIENTS[task_name] = pyrogram_client
        
        worker_scheduler.add_job(hourly_scheduler_job, 'interval', hours=1, args=[task_name, config, worker_scheduler], id=f"{task_name}_hourly_scheduler", next_run_time=datetime.now())
        
        worker_scheduler.start()
        await bot.send_message(ADMIN_IDS[0], f"📣 Рекламный воркер <b>{task_name}</b> запущен.")
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logging.info(f"Рекламный воркер {task_name} отменен.")
    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в рекламном воркере: {e}", exc_info=True)
        await stop_worker(task_name, bot, f"Критическая ошибка: {e}")
    finally:
        if worker_scheduler.running: worker_scheduler.shutdown()
        if pyrogram_client and pyrogram_client.is_connected: await pyrogram_client.stop()
        if task_name in ACTIVE_CLIENTS: ACTIVE_CLIENTS.pop(task_name)
        logging.info(f"Рекламный воркер {task_name} завершил работу.")

async def cb_manage_funnel(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    funnel_config = config.get("advertising_config", {}).get("funnel", {})
    
    status = "✅ Включена" if funnel_config.get("enabled") else "❌ Отключена"
    message_text = funnel_config.get("message", "Сообщение не установлено.")
    
    text = (
        f"<b>🔀 Управление воронкой ответов для '{task_name}'</b>\n\n"
        f"<b>Статус:</b> {status}\n\n"
        f"<b>Текущее сообщение для автоответа:</b>\n<pre>{message_text}</pre>"
    )
    
    buttons = [
        [InlineKeyboardButton(text="Включить / Выключить", callback_data=f"adv_funnel_toggle:{task_name}")],
        [InlineKeyboardButton(text="Изменить сообщение", callback_data=f"adv_funnel_set_msg:{task_name}")],
        [InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)

async def cb_toggle_funnel(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        funnel_config = config.setdefault("advertising_config", {}).setdefault("funnel", {})
        funnel_config["enabled"] = not funnel_config.get("enabled", False)
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    
    await cb_manage_funnel(callback, state)

async def cb_set_funnel_message(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(AdvertisingTaskEditing.get_funnel_message)
    await state.update_data(task_name=task_name)
    await callback.message.edit_text("Пришлите новый текст для автоответа на первое сообщение.")
    await safe_answer_callback(callback)

async def process_funnel_message(message: Message, state: FSMContext):
    data = await state.get_data()
    task_name = data['task_name']
    
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        funnel_config = config.setdefault("advertising_config", {}).setdefault("funnel", {})
        funnel_config["message"] = message.text
        await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        
    await state.clear()
    await message.answer("✅ Сообщение для воронки ответов успешно обновлено.")
    
    fake_callback = CallbackQuery(id=str(uuid.uuid4()), from_user=message.from_user, chat_instance="fake", message=message, data=f"adv_funnel_manage:{task_name}")
    await cb_manage_funnel(fake_callback, state)

async def cb_manage_adv_tasks(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    buttons = []
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    for task_file in sorted(task_files):
        try:
            config = await read_json_file(task_file)
            if config and config.get("task_type") == "advertising":
                buttons.append([InlineKeyboardButton(text=f"📣 {task_file.stem}", callback_data=f"task_view:{task_file.stem}")])
        except Exception: continue
    buttons.append([InlineKeyboardButton(text="➕ Добавить новую рекламную задачу", callback_data="adv_task_add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    await callback.message.edit_text("Управление рекламными задачами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await safe_answer_callback(callback)

async def cb_adv_task_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdvertisingTaskCreation.name)
    await callback.message.edit_text("<b>Шаг 1/7: Создание рекламной задачи</b>\n\nВведите уникальное название (имя сессии, только латиница без пробелов).")
    await safe_answer_callback(callback)

async def process_adv_task_name(message: Message, state: FSMContext):
    task_name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', task_name):
        await message.answer("Название может содержать только латинские буквы, цифры и нижнее подчеркивание.")
        return
    if (TASKS_DIR / f"{task_name}.json").exists():
        await message.answer("Задача с таким именем уже существует. Введите другое.")
        return
    if not (SESSIONS_DIR / f"{task_name}.session").exists():
        await message.answer(f"<b>Ошибка:</b> файл сессии `sessions/{task_name}.session` не найден. Сначала создайте его.")
        return
    await state.update_data(name=task_name, message_variants=[])
    await state.set_state(AdvertisingTaskCreation.api_id)
    await message.answer("<b>Шаг 2/7:</b> Введите `api_id` вашего Telegram-аккаунта.")

async def process_adv_task_api_id(message: Message, state: FSMContext):
    try:
        await state.update_data(api_id=int(message.text))
        await state.set_state(AdvertisingTaskCreation.api_hash)
        await message.answer("<b>Шаг 3/7:</b> Введите `api_hash`.")
    except ValueError:
        await message.answer("API ID должен быть числом. Попробуйте снова.")

async def process_adv_task_api_hash(message: Message, state: FSMContext):
    await state.update_data(api_hash=message.text.strip())
    await state.set_state(AdvertisingTaskCreation.proxy)
    await message.answer("<b>Шаг 4/7:</b> Введите данные прокси в формате `scheme://user:pass@host:port` или отправьте `-`, если прокси не нужен.")

async def process_adv_task_proxy(message: Message, state: FSMContext):
    proxy_str = message.text.strip()
    proxy_dict = None
    if proxy_str != '-':
        try:
            scheme, rest = proxy_str.split('://', 1)
            creds, host_port = rest.split('@', 1)
            user, password = creds.split(':', 1)
            host, port = host_port.split(':', 1)
            proxy_dict = {"scheme": scheme, "hostname": host, "port": int(port), "username": user, "password": password}
        except Exception:
            await message.answer("Неверный формат прокси. Используйте `scheme://user:pass@host:port` или отправьте `-`.")
            return
    await state.update_data(proxy=proxy_dict)
    await state.set_state(AdvertisingTaskCreation.watermark)
    await message.answer("<b>Шаг 5/7:</b> Отправьте файл `.png` в качестве водяного знака. Отправьте `-`, если не нужен.")

async def process_adv_task_watermark(message: Message, state: FSMContext):
    if message.text and message.text.strip() == '-':
        await state.update_data(watermark_file=None)
    elif message.document and 'image/png' in message.document.mime_type:
        data = await state.get_data()
        task_name = data['name']
        file_path = WATERMARKS_DIR / f"{task_name}.png"
        await bot_instance.download(message.document, destination=file_path)
        await state.update_data(watermark_file=str(file_path))
    else:
        await message.answer("Ошибка: отправьте водяной знак как ДОКУМЕНТ .png или отправьте `-` для пропуска.")
        return
    await state.set_state(AdvertisingTaskCreation.sends_per_hour)
    await message.answer("<b>Шаг 6/7:</b> Сколько рассылок по группам делать в ЧАС? (Отправьте число, например `2`)")

async def process_adv_task_sends_per_hour(message: Message, state: FSMContext):
    try:
        sends = int(message.text.strip())
        if not 1 <= sends <= 60: raise ValueError
        await state.update_data(sends_per_hour=sends)
        await state.set_state(AdvertisingTaskCreation.target_groups)
        await message.answer("<b>Шаг 7/7:</b> Введите ID групп для рассылки через запятую (например: `-100123, -100456`).\n\nЧтобы рассылать по всем группам, где состоит аккаунт, отправьте `-`.")
    except ValueError:
        await message.answer("Введите целое число от 1 до 60.")

async def process_adv_task_target_groups(message: Message, state: FSMContext):
    group_ids = []
    if message.text.strip() != '-':
        try:
            group_ids = [int(g.strip()) for g in message.text.split(',')]
        except ValueError:
            await message.answer("Неверный формат. Введите числовые ID через запятую.")
            return
    await state.update_data(target_group_ids=group_ids)
    await state.set_state(AdvertisingTaskCreation.get_message_variants)
    await message.answer("Отлично. Теперь пришлите контент для <b>первого варианта (1/5)</b> сообщения (текст, фото, видео или альбом).")

async def complete_variant_collection(message_or_callback, state: FSMContext):
    data = await state.get_data()
    message = message_or_callback if isinstance(message_or_callback, Message) else message_or_callback.message
    
    current_fsm_state = await state.get_state()

    if current_fsm_state in [AdvertisingTaskCreation.ask_for_more_variants, AdvertisingTaskCreation.get_message_variants, AdvertisingTaskCreation.confirm_album]:
        await finalize_advertising_task_creation(message, state)
    else:
        task_name = data['task_name']
        async with await get_task_lock(task_name):
            config = await read_json_file(TASKS_DIR / f"{task_name}.json")
            if 'advertising_config' in config:
                config['advertising_config']['message_variants'] = data['message_variants']
                await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        await state.clear()
        await message.answer(f"✅ Варианты сообщений для <b>{task_name}</b> обновлены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]]))

async def finalize_advertising_task_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    adv_config = {
        "sends_per_hour": data['sends_per_hour'],
        "target_group_ids": data.get('target_group_ids', []),
        "message_variants": data['message_variants'],
        "funnel": {"enabled": False, "message": ""}
    }
    task_config = {
        "task_name": data['name'], "task_type": "advertising",
        "api_id": data['api_id'], "api_hash": data['api_hash'],
        "status": "inactive", "last_error": None,
        "proxy": data.get('proxy'), "watermark_file": data.get('watermark_file'),
        "advertising_config": adv_config
    }
    await write_json_file(TASKS_DIR / f"{data['name']}.json", task_config)
    await state.clear()
    await message.answer(f"✅ Рекламная задача <b>{data['name']}</b> успешно создана!", reply_markup=main_menu_keyboard())

async def process_content_variant(message: Message, state: FSMContext):
    current_state_str = await state.get_state()
    confirm_album_state = AdvertisingTaskCreation.confirm_album if current_state_str == AdvertisingTaskCreation.get_message_variants else AdvertisingTaskEditing.confirm_new_album
    ask_more_state = AdvertisingTaskCreation.ask_for_more_variants if current_state_str == AdvertisingTaskCreation.get_message_variants else AdvertisingTaskEditing.ask_for_more_new_variants
    album_prefix = "adv_create_album" if current_state_str == AdvertisingTaskCreation.get_message_variants else "adv_edit_album"

    if message.media_group_id:
        await state.set_state(confirm_album_state)
        await handle_album_message(message, state, album_prefix)
        return

    try:
        data = await state.get_data()
        current_variants = data.get('message_variants', [])
        variant_data = {}

        if message.text:
            variant_data = {"text": message.text, "media_type": "none", "media_files": []}
        elif message.photo or message.video:
            forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, message.chat.id, message.message_id)
            variant_data = {"text": message.caption or "", "media_type": message.content_type, "media_files": [forwarded.message_id]}
        else:
            return

        current_variants.append(variant_data)
        await state.update_data(message_variants=current_variants)

        count = len(current_variants)
        if count < 5:
            await state.set_state(ask_more_state)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, добавить еще", callback_data="adv_add_another_variant:yes")],
                [InlineKeyboardButton(text="❌ Нет, достаточно", callback_data="adv_add_another_variant:no")]
            ])
            await message.answer(f"<b>Вариант {count}/5 сохранен.</b>\n\nХотите добавить следующий?", reply_markup=kb)
        else:
            await message.answer("<b>Вариант 5/5 сохранен.</b>\n\nДостигнут лимит. Завершаю...")
            await complete_variant_collection(message, state)
    except Exception as e:
        logging.error(f"Ошибка в process_content_variant: {e}", exc_info=True)
        await message.answer(f"Не удалось сохранить сообщение: {e}")
        await state.clear()

async def process_album_confirmation(callback: CallbackQuery, state: FSMContext):
    current_state_str = await state.get_state()
    get_message_state = AdvertisingTaskCreation.get_message_variants if current_state_str == AdvertisingTaskCreation.confirm_album else AdvertisingTaskEditing.get_new_message_variants
    ask_more_state = AdvertisingTaskCreation.ask_for_more_variants if current_state_str == AdvertisingTaskCreation.confirm_album else AdvertisingTaskEditing.ask_for_more_new_variants

    _, action, media_group_id = callback.data.split(":")
    album_data = TEMP_MEDIA_GROUPS.pop(media_group_id, None)
    if not album_data:
        await safe_answer_callback(callback, "Время ожидания истекло.", show_alert=True)
        return

    if action == "no":
        await state.set_state(get_message_state)
        await callback.message.edit_text("Альбом отменен. Пришлите новое сообщение.")
        await safe_answer_callback(callback)
        return

    try:
        media_ids, text_content = [], next((msg.caption for msg in album_data['messages'] if msg.caption), "")
        for msg in album_data['messages']:
            forwarded = await bot_instance.copy_message(STORAGE_CHANNEL_ID, msg.chat.id, msg.message_id)
            media_ids.append(forwarded.message_id)
        variant_data = {"text": text_content, "media_type": "album", "media_files": media_ids}

        data = await state.get_data()
        current_variants = data.get('message_variants', [])
        current_variants.append(variant_data)
        await state.update_data(message_variants=current_variants)

        count = len(current_variants)
        if count < 5:
            await state.set_state(ask_more_state)
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, добавить еще", callback_data="adv_add_another_variant:yes")],
                [InlineKeyboardButton(text="❌ Нет, достаточно", callback_data="adv_add_another_variant:no")]
            ])
            await callback.message.edit_text(f"<b>Вариант {count}/5 (альбом) сохранен.</b>\n\nХотите добавить следующий?", reply_markup=kb)
        else:
            await callback.message.edit_text("<b>Вариант 5/5 (альбом) сохранен.</b>\n\nДостигнут лимит. Завершаю...")
            await complete_variant_collection(callback, state)
    except Exception as e:
        await safe_answer_callback(callback, f"Ошибка сохранения альбома: {e}", show_alert=True)
        await state.set_state(get_message_state)
        await callback.message.edit_text("Пришлите сообщение заново.")
    finally:
        await safe_answer_callback(callback)

async def cb_adv_send_now(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await callback.message.edit_text(f"Запускаю немедленную рассылку для <b>{task_name}</b>... Это может занять много времени.")
    await safe_answer_callback(callback)
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    sent, failed = await send_advert_job(task_name, config)
    await callback.message.edit_text(f"✅ Рассылка для <b>{task_name}</b> завершена.\n\nУспешно: {sent}\nОшибки: {failed}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]]))

async def cb_adv_test_now(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(AdvertisingTaskTesting.get_test_group_id)
    await state.update_data(task_name=task_name)
    await callback.message.edit_text("Введите ID тестовой группы (число, можно с минусом).")
    await safe_answer_callback(callback)

async def process_adv_test_group_id(message: Message, state: FSMContext):
    try:
        group_id = int(message.text.strip())
        data = await state.get_data()
        task_name = data['task_name']
        await message.answer(f"Отправляю тестовое сообщение в группу <code>{group_id}</code>...")
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        sent, failed = await send_advert_job(task_name, config, target_groups_override=[group_id])
        result_text = "✅ Тестовое сообщение успешно отправлено." if sent > 0 else f"❌ Не удалось отправить тестовое сообщение."
        await message.answer(result_text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]]))
    except (ValueError, Exception) as e:
        await message.answer(f"Ошибка: {e}")
    finally:
        await state.clear()

async def cb_adv_show_account(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    client = ACTIVE_CLIENTS.get(task_name)
    is_temp_client = not (client and client.is_connected)
    if is_temp_client:
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
    try:
        if is_temp_client: await client.start()
        me = await client.get_me()
        full_name = f"{me.first_name or ''} {me.last_name or ''}".strip()
        await safe_answer_callback(callback, f"Аккаунт для {task_name}:\nИмя: {full_name}\nUsername: @{me.username or 'не указан'}", show_alert=True)
    except Exception as e:
        await safe_answer_callback(callback, f"Не удалось получить информацию: {e}", show_alert=True)
    finally:
        if is_temp_client and client and client.is_connected: await client.stop()

async def cb_edit_adv_field(callback: CallbackQuery, state: FSMContext):
    _, task_name, field = callback.data.split(":")
    await state.update_data(task_name=task_name)
    if field == "sends_per_hour":
        await state.set_state(AdvertisingTaskEditing.get_new_sends_per_hour)
        await callback.message.edit_text("Введите новое количество рассылок в ЧАС (1-60).")
    elif field == "target_groups":
        await cb_manage_target_groups(callback, state)
    elif field == "message_variants":
        await state.set_state(AdvertisingTaskEditing.get_new_message_variants)
        await state.update_data(message_variants=[])
        await callback.message.edit_text("<b>Внимание:</b> Вы перезаписываете все варианты сообщений.\n\nПришлите контент для <b>первого (нового)</b> варианта (1/5).")
    await safe_answer_callback(callback)

async def cb_manage_target_groups(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config: return
    
    group_ids = config.get("advertising_config", {}).get("target_group_ids", [])
    
    client = ACTIVE_CLIENTS.get(task_name)
    is_temp_client = not (client and client.is_connected)
    if is_temp_client:
        client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
    
    buttons = []
    text = f"<b>Управление группами для '{task_name}':</b>\n"
    
    try:
        if is_temp_client: await client.start()
        if not group_ids:
            text += "\nСписок групп пуст. Рассылка будет идти по всем группам, где состоит аккаунт."
        else:
            text += "\nНажмите на группу, чтобы удалить ее из списка.\n"
            for group_id in group_ids:
                try:
                    chat = await client.get_chat(group_id)
                    title = chat.title
                except Exception:
                    title = "Неизвестная группа (возможно, ID неверный)"
                buttons.append([InlineKeyboardButton(text=f"🗑️ {title} ({group_id})", callback_data=f"adv_delete_group:{task_name}:{group_id}")])
        
        buttons.append([InlineKeyboardButton(text="➕ Добавить группу по ID", callback_data=f"adv_add_group:{task_name}")])
        buttons.append([InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")])
        
        await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        
    except Exception as e:
        await callback.message.edit_text(f"Ошибка при получении названий групп: {e}\n\nУбедитесь, что аккаунт не забанен.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]]))
    finally:
        if is_temp_client and client.is_connected:
            await client.stop()
        await safe_answer_callback(callback)

async def cb_add_group(callback: CallbackQuery, state: FSMContext):
    task_name = callback.data.split(":")[1]
    await state.set_state(AdvertisingTaskEditing.add_group_id)
    await state.update_data(task_name=task_name)
    await callback.message.edit_text("Введите ID новой группы:")
    await safe_answer_callback(callback)

async def process_add_group_id(message: Message, state: FSMContext):
    try:
        group_id = int(message.text.strip())
        data = await state.get_data()
        task_name = data['task_name']
        
        async with await get_task_lock(task_name):
            config = await read_json_file(TASKS_DIR / f"{task_name}.json")
            if 'target_group_ids' not in config['advertising_config']:
                config['advertising_config']['target_group_ids'] = []
            
            if group_id not in config['advertising_config']['target_group_ids']:
                 config['advertising_config']['target_group_ids'].append(group_id)
                 await write_json_file(TASKS_DIR / f"{task_name}.json", config)
            
        await state.clear()
        
        fake_callback = CallbackQuery(id=str(uuid.uuid4()), from_user=message.from_user, chat_instance="fake", message=message, data=f"edit_adv_field:{task_name}:target_groups")
        await cb_manage_target_groups(fake_callback, state)

    except ValueError:
        await message.answer("ID должен быть числом. Попробуйте снова.")

async def cb_delete_group(callback: CallbackQuery, state: FSMContext):
    _, task_name, group_id_str = callback.data.split(":")
    group_id = int(group_id_str)
    
    async with await get_task_lock(task_name):
        config = await read_json_file(TASKS_DIR / f"{task_name}.json")
        if group_id in config['advertising_config']['target_group_ids']:
            config['advertising_config']['target_group_ids'].remove(group_id)
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
    
    await cb_manage_target_groups(callback, state)


async def process_edit_adv_sends_per_hour(message: Message, state: FSMContext):
    data = await state.get_data()
    task_name = data['task_name']
    try:
        sends = int(message.text.strip())
        assert 1 <= sends <= 60
        async with await get_task_lock(task_name):
            config = await read_json_file(TASKS_DIR / f"{task_name}.json")
            config['advertising_config']['sends_per_hour'] = sends
            await write_json_file(TASKS_DIR / f"{task_name}.json", config)
        await state.clear()
        await message.answer(f"✅ Количество рассылок в час для <b>{task_name}</b> обновлено на: {sends}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]]))
    except (ValueError, AssertionError):
        await message.answer("Введите целое число от 1 до 60.")

def register_handlers(router: Router, bot: Bot):
    global bot_instance
    bot_instance = bot

    router.callback_query.register(cb_manage_adv_tasks, F.data == "manage_adv_tasks", AdminFilter())
    router.callback_query.register(cb_adv_task_add, F.data == "adv_task_add", AdminFilter())
    router.message.register(process_adv_task_name, AdvertisingTaskCreation.name, AdminFilter())
    router.message.register(process_adv_task_api_id, AdvertisingTaskCreation.api_id, AdminFilter())
    router.message.register(process_adv_task_api_hash, AdvertisingTaskCreation.api_hash, AdminFilter())
    router.message.register(process_adv_task_proxy, AdvertisingTaskCreation.proxy, AdminFilter())
    router.message.register(process_adv_task_watermark, F.document | (F.text == '-'), AdvertisingTaskCreation.watermark, AdminFilter())
    router.message.register(process_adv_task_sends_per_hour, AdvertisingTaskCreation.sends_per_hour, AdminFilter())
    router.message.register(process_adv_task_target_groups, AdvertisingTaskCreation.target_groups, AdminFilter())
    router.message.register(process_content_variant, AdvertisingTaskCreation.get_message_variants, F.content_type.in_({'text', 'photo', 'video'}), AdminFilter())
    router.callback_query.register(process_album_confirmation, AdvertisingTaskCreation.confirm_album, F.data.startswith("adv_create_album:"), AdminFilter())
    router.message.register(process_content_variant, AdvertisingTaskEditing.get_new_message_variants, F.content_type.in_({'text', 'photo', 'video'}), AdminFilter())
    router.callback_query.register(process_album_confirmation, AdvertisingTaskEditing.confirm_new_album, F.data.startswith("adv_edit_album:"), AdminFilter())
    
    @router.callback_query(F.data.startswith("adv_add_another_variant:"), AdminFilter())
    async def cb_ask_for_another_variant(callback: CallbackQuery, state: FSMContext):
        current_fsm_state_str = await state.get_state()
        
        get_content_state = None
        if current_fsm_state_str == AdvertisingTaskCreation.ask_for_more_variants:
            get_content_state = AdvertisingTaskCreation.get_message_variants
        elif current_fsm_state_str == AdvertisingTaskEditing.ask_for_more_new_variants:
            get_content_state = AdvertisingTaskEditing.get_new_message_variants

        if callback.data.split(":")[1] == 'yes' and get_content_state:
            await state.set_state(get_content_state)
            data = await state.get_data()
            count = len(data.get('message_variants', []))
            await callback.message.edit_text(f"Пришлите контент для <b>варианта {count + 1}/5</b>.")
        else:
            await callback.message.edit_text("Завершаю...")
            await complete_variant_collection(callback, state)
        await safe_answer_callback(callback)

    router.callback_query.register(cb_edit_adv_field, F.data.startswith("edit_adv_field:"), AdminFilter())
    router.callback_query.register(cb_add_group, F.data.startswith("adv_add_group:"), AdminFilter())
    router.callback_query.register(cb_delete_group, F.data.startswith("adv_delete_group:"), AdminFilter())
    router.message.register(process_add_group_id, AdvertisingTaskEditing.add_group_id, AdminFilter())
    router.message.register(process_edit_adv_sends_per_hour, AdvertisingTaskEditing.get_new_sends_per_hour, AdminFilter())
    
    router.callback_query.register(cb_manage_funnel, F.data.startswith("adv_funnel_manage:"), AdminFilter())
    router.callback_query.register(cb_toggle_funnel, F.data.startswith("adv_funnel_toggle:"), AdminFilter())
    router.callback_query.register(cb_set_funnel_message, F.data.startswith("adv_funnel_set_msg:"), AdminFilter())
    router.message.register(process_funnel_message, AdvertisingTaskEditing.get_funnel_message, AdminFilter())
    
    router.callback_query.register(cb_adv_send_now, F.data.startswith("adv_send_now:"), AdminFilter())
    router.callback_query.register(cb_adv_test_now, F.data.startswith("adv_test_now:"), AdminFilter())
    router.message.register(process_adv_test_group_id, AdvertisingTaskTesting.get_test_group_id, AdminFilter())
    router.callback_query.register(cb_adv_show_account, F.data.startswith("adv_show_account:"), AdminFilter())