# generative_channels.py (ОБНОВЛЕННАЯ ВЕРСИЯ)

import asyncio
import logging
import random
import os
import re
from datetime import datetime, timedelta
from typing import Union

# Aiogram и связанные с ним импорты
from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.exceptions import TelegramBadRequest
from aiogram.client.bot import Bot

# Pyrogram и планировщик
from pyrogram import Client
from pyrogram.errors import RPCError
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# --- Импорты из нашего нового файла utils.py ---
# Убраны импорты из main.py, теперь все зависимости ведут на utils
from utils import (
    TASKS_DIR, SESSIONS_DIR, WATERMARKS_DIR, ADMIN_IDS, ACTIVE_CLIENTS,
    write_json_file, read_json_file, get_task_lock, remove_file_async,
    apply_watermark_async, post_to_instagram, post_to_x,
    generate_ai_image, generate_ai_text, AdminFilter,
    check_channel_access, stop_worker
)

# Глобальная переменная для экземпляра бота, устанавливается при регистрации
bot_instance: Union[Bot, None] = None


# --- FSM для создания генеративной задачи ---
class GenerativeTaskCreation(StatesGroup):
    name = State()
    api_id = State()
    api_hash = State()
    target_id = State()
    target_link = State()
    proxy = State()
    watermark = State()
    channel_theme = State()
    channel_style = State()
    posts_per_day = State()
    target_language = State()
    text_provider = State()
    wants_images = State()
    image_provider = State()


# --- Функции для работы с AI ---

async def create_base_prompt_for_channel(theme: str, style: str, language: str) -> str:
    """Создает качественный системный промпт для будущих генераций."""
    meta_prompt = (
        "Ты — эксперт по созданию AI-промптов. Твоя задача — создать детальный и эффективный системный промпт для другой языковой модели (LLM). "
        "Этот промпт будет использоваться для генерации постов в Telegram-канале.\n\n"
        "**Инструкции для тебя:**\n"
        "1. Проанализируй предоставленные тему, стиль и язык.\n"
        "2. Создай системный промпт, который будет четко инструктировать LLM.\n"
        "3. Промпт должен включать указания по тону, структуре поста, целевой аудитории и формату ответа.\n"
        "4. В промпте должно быть указано, что LLM должна отвечать ТОЛЬКО готовым текстом поста, без каких-либо вступлений вроде 'Вот ваш пост:'.\n"
        "5. Укажи, что в конце поста не должно быть никаких подписей, хэштегов или призывов к действию.\n\n"
        f"**Данные для создания промпта:**\n"
        f"- **Основная тема канала:** {theme}\n"
        f"- **Стиль изложения:** {style}\n"
        f"- **Язык постов:** {language}\n\n"
        "Твой результат — это ТОЛЬКО сгенерированный системный промпт. Ничего больше."
    )
    logging.info("Генерация базового промпта для канала...")
    # Используем generate_ai_text из utils
    base_prompt = await generate_ai_text('gemini', meta_prompt, "Ты — ассистент.")
    return base_prompt or f"Ты — автор постов для Telegram-канала на тему '{theme}'. Пиши в стиле '{style}' на языке '{language}'. Твой ответ - только текст поста."

# --- Логика воркера и выполнения задач ---

async def generate_post_job(task_name: str, config: dict):
    """Основная функция, которая генерирует, публикует пост в телеграм и соцсети."""
    logging.info(f"[{task_name}] Начинаю генерацию нового поста.")
    gen_config = config.get('generative_config', {})
    client = ACTIVE_CLIENTS.get(task_name)
    image_path, final_image_path, watermarked_path = None, None, None
    media_paths_for_socials = []

    if not client or not client.is_connected:
        logging.warning(f"[{task_name}] Клиент Pyrogram не активен. Пропускаю генерацию.")
        return

    try:
        # --- Этап 1: Генерация контента ---
        base_prompt = gen_config.get('base_prompt', 'Напиши интересный пост.')
        theme = gen_config.get('channel_theme', 'интересные факты')
        lang = gen_config.get('target_language', 'русский')
        text_provider = gen_config.get('text_provider', 'gemini')

        sub_topic_prompt = f"Придумай одну короткую, конкретную и интригующую тему для поста в Telegram-канале. Основная тема канала: '{theme}'. Язык ответа: {lang}. В ответе дай только название темы, без лишних слов."
        sub_topic = await generate_ai_text(text_provider, sub_topic_prompt, "Ты — креативный генератор идей.")
        if not sub_topic:
            logging.error(f"[{task_name}] Не удалось сгенерировать подтему. Пропускаю пост.")
            return
        sub_topic = sub_topic.strip().strip('"')

        post_text_prompt = f"Напиши текст для поста на тему: '{sub_topic}'."
        post_text = await generate_ai_text(text_provider, post_text_prompt, base_prompt)
        if not post_text:
            logging.error(f"[{task_name}] Не удалось сгенерировать текст поста. Пропускаю.")
            return
        
        # --- Этап 2: Генерация и обработка изображения ---
        image_provider = gen_config.get('image_provider', 'none')
        if image_provider != 'none':
            image_prompt_prompt = f"Создай короткий, яркий и детальный промпт для AI-генератора изображений (например, DALL-E 3). Промпт должен быть на английском языке и описывать иллюстрацию к посту на тему '{sub_topic}'. Стиль иллюстрации: {gen_config.get('channel_style', 'digital art')}. В ответе должен быть только сам промпт, без лишних слов."
            image_prompt = await generate_ai_text(text_provider, image_prompt_prompt, "Ты — AI-художник и промпт-инженер.")
            if image_prompt:
                image_path = await generate_ai_image(image_provider, image_prompt.strip().strip('"'), task_name)
            else:
                logging.warning(f"[{task_name}] Не удалось сгенерировать промпт для изображения.")

        final_text = post_text.strip()
        target_channel = config['target_channel']
        watermark_file = config.get('watermark_file')

        if image_path and watermark_file:
            watermarked_path = await apply_watermark_async(image_path, watermark_file)
        
        final_image_path = watermarked_path or image_path

        # --- Этап 3: Публикация в Telegram ---
        if final_image_path:
            await client.send_photo(target_channel, photo=final_image_path, caption=final_text)
            media_paths_for_socials.append(final_image_path)
            logging.info(f"[{task_name}] Опубликован пост с изображением в {target_channel}.")
        else:
            await client.send_message(target_channel, final_text)
            logging.info(f"[{task_name}] Опубликован текстовый пост в {target_channel}.")

        # --- Этап 4: Публикация в соцсети ---
        await post_to_instagram(task_name, config, final_text, media_paths_for_socials)
        await post_to_x(task_name, config, final_text, media_paths_for_socials)

    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в `generate_post_job`: {e}", exc_info=True)
    finally:
        # Асинхронно удаляем все временные файлы
        await asyncio.gather(
            remove_file_async(image_path),
            remove_file_async(watermarked_path)
        )

async def daily_scheduler_job(task_name: str, config: dict, scheduler: AsyncIOScheduler):
    """Планирует посты на следующие 24 часа."""
    logging.info(f"[{task_name}] Запуск ежедневного планировщика постов.")
    try:
        for job in scheduler.get_jobs():
            if job.id.startswith(f"{task_name}_post_"):
                job.remove()
        
        gen_config = config.get('generative_config', {})
        posts_per_day = gen_config.get('posts_per_day', 1)

        if posts_per_day > 0:
            interval_minutes = int(1440 / posts_per_day) # 24 * 60 = 1440 минут в дне
            for i in range(posts_per_day):
                # Добавляем случайное смещение, чтобы посты не выходили ровно по часам
                random_offset = random.randint(-int(interval_minutes * 0.2), int(interval_minutes * 0.2))
                delay_seconds = (i * interval_minutes + random_offset) * 60
                run_date = datetime.now() + timedelta(seconds=delay_seconds)
                scheduler.add_job(generate_post_job, 'date', run_date=run_date, args=[task_name, config], id=f"{task_name}_post_{i}")
        
        logging.info(f"[{task_name}] Запланировано {posts_per_day} постов на ближайшие 24 часа.")
    except Exception as e:
        logging.error(f"[{task_name}] Ошибка в ежедневном планировщике: {e}", exc_info=True)


async def run_generative_worker(task_name: str, config: dict, bot: Bot):
    """Воркер для одной генеративной задачи. Теперь принимает bot как аргумент."""
    pyrogram_client = None
    worker_scheduler = AsyncIOScheduler(timezone="UTC")

    try:
        pyrogram_client = Client(task_name, api_id=config['api_id'], api_hash=config['api_hash'], workdir=str(SESSIONS_DIR), proxy=config.get('proxy'))
        await pyrogram_client.start()
        ACTIVE_CLIENTS[task_name] = pyrogram_client
        # Вызываем check_channel_access из utils, передавая bot
        await check_channel_access(pyrogram_client, config, bot, ADMIN_IDS[0])

        worker_scheduler.add_job(daily_scheduler_job, 'interval', days=1, args=[task_name, config, worker_scheduler], id=f"{task_name}_daily_scheduler", next_run_time=datetime.now())
        
        worker_scheduler.start()
        await bot.send_message(ADMIN_IDS[0], f"✨ Генеративный воркер <b>{task_name}</b> запущен.")
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        logging.info(f"Генеративный воркер {task_name} отменен.")
    except Exception as e:
        logging.error(f"[{task_name}] Критическая ошибка в генеративном воркере: {e}", exc_info=True)
        # Вызываем stop_worker из utils, передавая bot
        await stop_worker(task_name, bot, f"Критическая ошибка: {e}")
    finally:
        if worker_scheduler.running:
            worker_scheduler.shutdown()
        if pyrogram_client and pyrogram_client.is_connected:
            await pyrogram_client.stop()
        if task_name in ACTIVE_CLIENTS:
            ACTIVE_CLIENTS.pop(task_name)
        logging.info(f"Генеративный воркер {task_name} завершил работу.")

# --- FSM ОБРАБОТЧИКИ ---

async def cb_manage_gen_tasks(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    buttons = []
    task_files = [f for f in TASKS_DIR.glob("*.json") if not f.name.endswith("_stats.json")]
    
    for task_file in sorted(task_files):
        try:
            config = await read_json_file(task_file)
            if config and config.get("task_type") == "generative":
                buttons.append([InlineKeyboardButton(text=f"✨ {task_file.stem}", callback_data=f"task_view:{task_file.stem}")])
        except Exception: continue

    buttons.append([InlineKeyboardButton(text="➕ Добавить новую генеративную задачу", callback_data="gen_task_add")])
    buttons.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")])
    
    await callback.message.edit_text("Управление генеративными задачами:", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()

async def cb_gen_task_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(GenerativeTaskCreation.name)
    await callback.message.edit_text("<b>Шаг 1/11: Создание генеративной задачи</b>\n\nВведите уникальное название (имя сессии, только латиница без пробелов).")
    await callback.answer()

async def process_gen_task_name(message: Message, state: FSMContext):
    task_name = message.text.strip()
    if not re.match(r'^[a-zA-Z0-9_]+$', task_name):
        await message.answer("Название может содержать только латинские буквы, цифры и нижнее подчеркивание.")
        return
    if os.path.exists(TASKS_DIR / f"{task_name}.json"):
        await message.answer("Задача с таким именем уже существует. Введите другое.")
        return
    if not os.path.exists(SESSIONS_DIR / f"{task_name}.session"):
        await message.answer(f"<b>Ошибка:</b> файл сессии `sessions/{task_name}.session` не найден. Сначала создайте его.")
        return
    await state.update_data(name=task_name)
    await state.set_state(GenerativeTaskCreation.api_id)
    await message.answer("<b>Шаг 2/11:</b> Введите `api_id` вашего Telegram-аккаунта.")

async def process_gen_task_api_id(message: Message, state: FSMContext):
    try:
        await state.update_data(api_id=int(message.text))
        await state.set_state(GenerativeTaskCreation.api_hash)
        await message.answer("<b>Шаг 3/11:</b> Введите `api_hash`.")
    except ValueError:
        await message.answer("API ID должен быть числом. Попробуйте снова.")

async def process_gen_task_api_hash(message: Message, state: FSMContext):
    await state.update_data(api_hash=message.text.strip())
    await state.set_state(GenerativeTaskCreation.target_id)
    await message.answer("<b>Шаг 4/11:</b> Введите числовой ID целевого канала (куда будут публиковаться посты).")

async def process_gen_task_target_id(message: Message, state: FSMContext):
    try:
        await state.update_data(target_id=int(message.text))
        await state.set_state(GenerativeTaskCreation.target_link)
        await message.answer("<b>Шаг 5/11:</b> Введите публичную ссылку на целевой канал (например, `t.me/channelname` или `@channelname`).")
    except ValueError:
        await message.answer("ID канала должен быть числом. Попробуйте снова.")

async def process_gen_task_target_link(message: Message, state: FSMContext):
    await state.update_data(target_link=message.text.strip())
    await state.set_state(GenerativeTaskCreation.proxy)
    await message.answer("<b>Шаг 6/11:</b> Введите данные прокси в формате `scheme://user:pass@host:port` или отправьте `-`, если прокси не нужен.")

async def process_gen_task_proxy(message: Message, state: FSMContext):
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
    await state.set_state(GenerativeTaskCreation.watermark)
    await message.answer("<b>Шаг 7/11:</b> Отправьте файл `.png` в качестве водяного знака. Он будет накладываться на сгенерированные изображения. Отправьте как ДОКУМЕНТ.")

async def process_gen_task_watermark(message: Message, state: FSMContext):
    if not message.document or 'image/png' not in message.document.mime_type:
        await message.answer("Ошибка: отправьте водяной знак как ДОКУМЕНТ .png")
        return
    data = await state.get_data()
    task_name = data['name']
    file_path = WATERMARKS_DIR / f"{task_name}.png"
    await bot_instance.download(message.document, destination=file_path)
    await state.update_data(watermark_file=str(file_path))
    await state.set_state(GenerativeTaskCreation.channel_theme)
    await message.answer("<b>Шаг 8/11:</b> Водяной знак сохранен.\n\nТеперь введите основную тему для вашего канала (например, 'история Древнего Рима', 'секреты высокой кухни', 'стоическая философия на каждый день').")

async def process_gen_task_theme(message: Message, state: FSMContext):
    await state.update_data(channel_theme=message.text.strip())
    await state.set_state(GenerativeTaskCreation.channel_style)
    await message.answer("<b>Шаг 9/11:</b> Отлично. Теперь опишите желаемый стиль постов (например, 'академический и строгий', 'простой и с юмором', 'загадочный и мистический').")

async def process_gen_task_style(message: Message, state: FSMContext):
    await state.update_data(channel_style=message.text.strip())
    await state.set_state(GenerativeTaskCreation.posts_per_day)
    await message.answer("<b>Шаг 10/11:</b> Сколько постов в день публиковать? (Отправьте число, например `3`)")

async def process_gen_task_posts_per_day(message: Message, state: FSMContext):
    try:
        posts_per_day = int(message.text.strip())
        if not 1 <= posts_per_day <= 24:
            raise ValueError
        await state.update_data(posts_per_day=posts_per_day)
        await state.set_state(GenerativeTaskCreation.target_language)
        await message.answer("<b>Шаг 11/11:</b> На каком языке должны быть посты? (например, 'русский', 'английский')")
    except ValueError:
        await message.answer("Неверное число. Введите целое число от 1 до 24.")

async def process_gen_task_language(message: Message, state: FSMContext):
    await state.update_data(target_language=message.text.strip())
    msg = await message.answer("Генерирую базовый промпт для AI, это может занять до минуты...")
    data = await state.get_data()
    base_prompt = await create_base_prompt_for_channel(
        theme=data['channel_theme'],
        style=data['channel_style'],
        language=data['target_language']
    )
    await state.update_data(base_prompt=base_prompt)
    await msg.delete()
    await state.set_state(GenerativeTaskCreation.text_provider)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Gemini (Google)", callback_data="gen_text_provider:gemini")],
        [InlineKeyboardButton(text="🤖 ChatGPT (OpenAI)", callback_data="gen_text_provider:openai")],
        [InlineKeyboardButton(text="🤖 Claude (Anthropic)", callback_data="gen_text_provider:claude")]
    ])
    await message.answer("<b>Завершение: Конфигурация AI</b>\n\nБазовый промпт создан. Выберите основного AI-провайдера для генерации <b>текста</b>:", reply_markup=kb)

async def cb_gen_text_provider(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]
    await state.update_data(text_provider=provider)
    await state.set_state(GenerativeTaskCreation.wants_images)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, генерировать", callback_data="gen_wants_images:yes")],
        [InlineKeyboardButton(text="❌ Нет, только текст", callback_data="gen_wants_images:no")]
    ])
    await callback.message.edit_text(f"Текстовый провайдер: <b>{provider}</b>.\n\nГенерировать изображения для постов?", reply_markup=kb)
    await callback.answer()

async def cb_wants_images(callback: CallbackQuery, state: FSMContext):
    choice = callback.data.split(":")[1]
    if choice == 'no':
        await state.update_data(image_provider='none')
        await callback.message.edit_text("Понял, посты будут только текстовые.\n\nЗавершаю создание задачи...")
        await finalize_generative_task_creation(callback.message, state)
    else:
        await state.set_state(GenerativeTaskCreation.image_provider)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎨 DALL-E 3 (OpenAI)", callback_data="gen_image_provider:dalle3")],
            [InlineKeyboardButton(text="🎨 Stable Diffusion (Replicate)", callback_data="gen_image_provider:replicate")]
        ])
        await callback.message.edit_text("Отлично! Выберите провайдера для генерации <b>изображений</b>:", reply_markup=kb)
    await callback.answer()

async def cb_gen_image_provider(callback: CallbackQuery, state: FSMContext):
    provider = callback.data.split(":")[1]
    await state.update_data(image_provider=provider)
    await callback.message.edit_text(f"Провайдер изображений: {provider}.\n\nВсе данные собраны, создаю задачу...")
    await finalize_generative_task_creation(callback.message, state)
    await callback.answer()

async def finalize_generative_task_creation(message: Message, state: FSMContext):
    data = await state.get_data()
    task_config = {
        "task_name": data['name'],
        "task_type": "generative",
        "api_id": data['api_id'],
        "api_hash": data['api_hash'],
        "status": "inactive",
        "last_error": None,
        "target_channel": data['target_id'],
        "target_channel_link": data['target_link'],
        "proxy": data.get('proxy'),
        "watermark_file": data.get('watermark_file'),
        "generative_config": {
            "channel_theme": data['channel_theme'],
            "channel_style": data['channel_style'],
            "base_prompt": data['base_prompt'],
            "posts_per_day": data['posts_per_day'],
            "target_language": data['target_language'],
            "text_provider": data['text_provider'],
            "image_provider": data['image_provider'],
        },
        # Добавляем пустые конфиги для соцсетей для совместимости
        "instagram": {"enabled": False, "username": "", "password": "", "last_status": "Not configured"},
        "twitter": {"enabled": False, "consumer_key": "", "consumer_secret": "", "access_token": "", "access_token_secret": "", "last_status": "Not configured"}
    }
    task_lock = await get_task_lock(data['name'])
    await write_json_file(TASKS_DIR / f"{data['name']}.json", task_config, task_lock)
    await state.clear()
    await message.answer(f"✅ Генеративная задача <b>{data['name']}</b> успешно создана!", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ В главное меню", callback_data="main_menu")]]))

async def cb_generate_post_now(callback: CallbackQuery):
    task_name = callback.data.split(":")[1]
    
    if task_name not in ACTIVE_CLIENTS or not ACTIVE_CLIENTS[task_name].is_connected:
        try:
            await callback.message.edit_text(
                f"<b>Ошибка:</b> Задача <b>{task_name}</b> неактивна.\n\n"
                f"Пожалуйста, сначала запустите задачу. Кнопка ручной генерации использует ее активную сессию для отправки поста.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data=f"task_view:{task_name}")]
                ])
            )
        except TelegramBadRequest:
            pass
        await callback.answer("Задача должна быть активна!", show_alert=True)
        return

    await callback.answer(f"Запускаю принудительную генерацию для {task_name}...")
    await callback.message.edit_text(f"⏳ Генерирую пост для <b>{task_name}</b>... Это может занять несколько минут.", reply_markup=None)
    
    config = await read_json_file(TASKS_DIR / f"{task_name}.json")
    if not config:
        await callback.message.edit_text(f"Ошибка: не найдена конфигурация для {task_name}")
        return

    try:
        await generate_post_job(task_name, config)
        await callback.message.edit_text(
            f"✅ Пост для <b>{task_name}</b> успешно сгенерирован и отправлен в целевой канал.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]
            ])
        )
    except Exception as e:
        logging.error(f"Ошибка принудительной генерации для {task_name}: {e}", exc_info=True)
        await callback.message.edit_text(
            f"❌ Произошла ошибка при генерации поста для <b>{task_name}</b>.\n\n<pre>{e}</pre>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад к задаче", callback_data=f"task_view:{task_name}")]
            ])
        )
        
def register_handlers(router: Router, bot: Bot):
    """Регистрирует все FSM и callback хендлеры из этого файла."""
    global bot_instance
    bot_instance = bot

    router.callback_query.register(cb_manage_gen_tasks, F.data == "manage_gen_tasks", AdminFilter())
    router.callback_query.register(cb_gen_task_add, F.data == "gen_task_add", AdminFilter())
    router.message.register(process_gen_task_name, GenerativeTaskCreation.name, AdminFilter())
    router.message.register(process_gen_task_api_id, GenerativeTaskCreation.api_id, AdminFilter())
    router.message.register(process_gen_task_api_hash, GenerativeTaskCreation.api_hash, AdminFilter())
    router.message.register(process_gen_task_target_id, GenerativeTaskCreation.target_id, AdminFilter())
    router.message.register(process_gen_task_target_link, GenerativeTaskCreation.target_link, AdminFilter())
    router.message.register(process_gen_task_proxy, GenerativeTaskCreation.proxy, AdminFilter())
    router.message.register(process_gen_task_watermark, GenerativeTaskCreation.watermark, F.document, AdminFilter())
    router.message.register(process_gen_task_theme, GenerativeTaskCreation.channel_theme, AdminFilter())
    router.message.register(process_gen_task_style, GenerativeTaskCreation.channel_style, AdminFilter())
    router.message.register(process_gen_task_posts_per_day, GenerativeTaskCreation.posts_per_day, AdminFilter())
    router.message.register(process_gen_task_language, GenerativeTaskCreation.target_language, AdminFilter())
    router.callback_query.register(cb_gen_text_provider, F.data.startswith("gen_text_provider:"), GenerativeTaskCreation.text_provider)
    router.callback_query.register(cb_wants_images, F.data.startswith("gen_wants_images:"), GenerativeTaskCreation.wants_images)
    router.callback_query.register(cb_gen_image_provider, F.data.startswith("gen_image_provider:"), GenerativeTaskCreation.image_provider)
    router.callback_query.register(cb_generate_post_now, F.data.startswith("gen_post_now:"), AdminFilter())