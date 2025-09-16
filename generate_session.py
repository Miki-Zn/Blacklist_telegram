import asyncio
from pyrogram import Client
from pathlib import Path

# Убедимся, что папка sessions существует
Path("sessions").mkdir(exist_ok=True)

async def main():
    print("--- Создание файла сессии Pyrogram ---\n")
    
    session_name = input("Придумайте имя для этой сессии (например, my_task1). Оно должно совпадать с именем задачи в боте: ")
    if not session_name:
        print("Имя сессии не может быть пустым.")
        return

    api_id = input("Введите ваш api_id: ")
    api_hash = input("Введите ваш api_hash: ")

    # При инициализации клиента мы указываем путь к файлу сессии
    # Pyrogram автоматически создаст его, если он не существует
    app = Client(f"sessions/{session_name}", api_id=int(api_id), api_hash=api_hash)

    try:
        print("\nЗапускаю клиент... Вам нужно будет пройти авторизацию.")
        await app.start()
        
        # Получаем информацию о себе, чтобы убедиться, что вход выполнен
        me = await app.get_me()
        print(f"\n✅ УСПЕШНО! Вход выполнен для аккаунта: {me.first_name} (@{me.username})")
        print(f"Файл сессии '{session_name}.session' был успешно создан в папке 'sessions/'.")
        
        await app.stop()
        print("\nТеперь вы можете создавать задачу с этим же именем в основном боте.")

    except Exception as e:
        print(f"\nПроизошла ошибка: {e}")
    finally:
        if app.is_connected:
            await app.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("\nПроцесс прерван пользователем.")