import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
from aiogram.filters import Command

# Вставь сюда токен своего бота от BotFather
TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Команда /start
@dp.message(Command("start"))
async def start(message: types.Message):
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Открыть приложение",
                    web_app=WebAppInfo(url="https://toxic-skill.vercel.app")  # твой Vercel URL
                )
            ]
        ]
    )
    await message.answer("Запусти наше приложение 👇", reply_markup=keyboard)

# Обработка данных, которые WebApp отправляет обратно
@dp.message()
async def handle_webapp_data(message: types.Message):
    if message.web_app_data:
        data = message.web_app_data.data
        await message.answer(f"Получены данные из WebApp: {data}")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
