import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

# Логирование
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not ADMIN_ID:
    raise ValueError("Пожалуйста, укажите BOT_TOKEN и ADMIN_ID в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Структура данных (в реальном проекте лучше использовать БД, например, SQLite/PostgreSQL)
# active_tickets: {user_id: ticket_id}
# tickets: {ticket_id: {"user_id": int, "messages_count": int, "status": "open"}}
active_tickets = {}
tickets = {}
ticket_counter = 1

# Состояния FSM для админ-панели
class AdminStates(StatesGroup):
    waiting_for_reply = State()

# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT ---
async def handle_ping(request):
    return web.Response(text="Stopka Payments Bot is alive!")

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")

# --- КЛАВИАТУРЫ ---
def get_main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="Оплатили? Напишите и отправьте чек!", callback_data="create_ticket")]
        ]
    )

def get_admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📩 Сообщения (Тикеты)", callback_data="admin_tickets")]
        ]
    )

def get_ticket_manage_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Ответить", callback_data=f"reply_ticket_{ticket_id}"),
                InlineKeyboardButton(text="Закрыть", callback_data=f"close_ticket_{ticket_id}")
            ],
            [InlineKeyboardButton(text="« Назад к списку", callback_data="admin_tickets")]
        ]
    )

# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 **Добро пожаловать в поддержку Stopka Payments!**\n\n"
        "Оплатили? Напишите и прикрепите чек ниже, создав обращение.",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard()
    )

@dp.callback_query(F.data == "create_ticket")
async def process_create_ticket(callback: types.CallbackQuery):
    global ticket_counter
    user_id = callback.from_user.id

    if user_id in active_tickets:
        ticket_id = active_tickets[user_id]
        await callback.message.answer(
            f"У вас уже есть активный **Тикет #{ticket_id}**.\n"
            "Вы можете отправлять сюда тексты и скриншоты — админ их получит."
        )
    else:
        ticket_id = ticket_counter
        ticket_counter += 1
        active_tickets[user_id] = ticket_id
        tickets[ticket_id] = {"user_id": user_id, "status": "open", "messages_count": 0}

        await callback.message.answer(
            f"✅ **Тикет #{ticket_id} успешно создан!**\n\n"
            "Отправьте скриншот оплаты и напишите 'оплатил' прямо в этот чат. "
            "Вы можете отправлять сообщения сколько угодно раз, пока админ не закроет тикет."
        )
    await callback.answer()

# Прием сообщений и фото от пользователя в рамках тикета
@dp.message(~F.from_user.id == ADMIN_ID)
async def handle_user_messages(message: types.Message):
    user_id = message.from_user.id

    if user_id not in active_tickets:
        await message.answer(
            "У вас нет активного обращения. Нажмите /start и создайте тикет.",
            reply_markup=get_main_keyboard()
        )
        return

    ticket_id = active_tickets[user_id]
    tickets[ticket_id]["messages_count"] += 1
    
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    # Пересылка админу
    await bot.send_message(
        ADMIN_ID,
        f"📩 **Новое сообщение в Тикете #{ticket_id}** от {user_info}:",
        parse_mode="Markdown"
    )
    
    # Репост сообщения пользователя админу
    await message.copy_to(chat_id=ADMIN_ID, reply_markup=get_ticket_manage_kb(ticket_id))
    await message.answer("Сообщение доставлено поддержке Stopka Payments! Ожидайте ответа.")

# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"), F.from_user.id == ADMIN_ID)
async def cmd_admin(message: types.Message):
    await message.answer(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )

@dp.callback_query(F.data == "admin_tickets", F.from_user.id == ADMIN_ID)
async def admin_list_tickets(callback: types.CallbackQuery):
    open_tickets = {t_id: data for t_id, data in tickets.items() if data["status"] == "open"}

    if not open_tickets:
        await callback.message.edit_text(
            "⚙️ **Админ панель**\n\nАктивных тикетов нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Обновить", callback_data="admin_tickets")]
            ])
        )
        return

    buttons = []
    for t_id in open_tickets:
        buttons.append([InlineKeyboardButton(text=f"Тикет #{t_id}", callback_data=f"view_ticket_{t_id}")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("⚙️ **Админ панель — Сообщения:**", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_ticket_"), F.from_user.id == ADMIN_ID)
async def admin_view_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("Тикет не найден или уже закрыт.")
        return

    await callback.message.edit_text(
        f"🎫 **Тикет #{ticket_id}**\n"
        f"Пользователь: `{ticket['user_id']}`\n"
        f"Всего сообщений: {ticket['messages_count']}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_ticket_manage_kb(ticket_id)
    )

@dp.callback_query(F.data.startswith("reply_ticket_"), F.from_user.id == ADMIN_ID)
async def start_reply_ticket(callback: types.CallbackQuery, state: FSMContext):
    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_for_reply)

    await callback.message.answer(
        f"✍️ Напишите ответ для **Тикета #{ticket_id}** (можно отправить текст или фото):"
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_reply, F.from_user.id == ADMIN_ID)
async def send_reply_to_user(message: types.Message, state: FSMContext):
    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        try:
            await message.send_copy(chat_id=user_id)
            await message.answer(f"✅ Ответ успешно отправлен в **Тикет #{ticket_id}**!")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить сообщение: {e}")
    else:
        await message.answer("Ошибка: тикет больше не активен.")

    await state.clear()

@dp.callback_query(F.data.startswith("close_ticket_"), F.from_user.id == ADMIN_ID)
async def close_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        ticket["status"] = "closed"
        if user_id in active_tickets:
            del active_tickets[user_id]

        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"🔒 Ваш **Тикет #{ticket_id}** был закрыт администратором. "
                "Если у вас появятся новые вопросы, вы можете создать новый тикет с помощью /start."
            )
        except Exception:
            pass

        await callback.message.edit_text(f"🔴 **Тикет #{ticket_id} закрыт.**")
    else:
        await callback.answer("Тикет уже закрыт.")

# --- ЗАПУСК ---
async def main():
    # Запускаем фоновый веб-сервер для UptimeRobot
    await start_web_server()
    # Запускаем поллинг бота
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())