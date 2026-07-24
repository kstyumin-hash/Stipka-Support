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
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not SUPER_ADMIN_ID:
    raise ValueError("Пожалуйста, укажите BOT_TOKEN и ADMIN_ID в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Множество активных администраторов
ADMINS = {SUPER_ADMIN_ID}

# Структура данных тикетов
# active_tickets: {user_id: ticket_id}
# tickets: {ticket_id: {"user_id": int, "status": "open", "messages": [{"sender": str, "text": str}]}}
active_tickets = {}
tickets = {}
ticket_counter = 1

# Состояния FSM
class AdminStates(StatesGroup):
    waiting_for_reply = State()
    waiting_for_new_admin_id = State()
    waiting_for_remove_admin_id = State()

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

# --- ПРОВЕРКА НА АДМИНА ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

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
            [InlineKeyboardButton(text="📩 Сообщения (Тикеты)", callback_data="admin_tickets")],
            [InlineKeyboardButton(text="👥 Управление админами", callback_data="admin_manage_admins")]
        ]
    )

def get_admins_manage_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить админа", callback_data="admin_add_new")],
            [InlineKeyboardButton(text="➖ Удалить админа", callback_data="admin_remove_exist")],
            [InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")]
        ]
    )

def get_ticket_manage_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📜 Посмотреть сообщения", callback_data=f"history_ticket_{ticket_id}")],
            [
                InlineKeyboardButton(text="Ответить", callback_data=f"reply_ticket_{ticket_id}"),
                InlineKeyboardButton(text="Закрыть", callback_data=f"close_ticket_{ticket_id}")
            ],
            [InlineKeyboardButton(text="« Назад к тикетам", callback_data="admin_tickets")]
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
            "Вы можете отправлять сюда сообщения и скриншоты — поддержка их получит."
        )
    else:
        ticket_id = ticket_counter
        ticket_counter += 1
        active_tickets[user_id] = ticket_id
        tickets[ticket_id] = {
            "user_id": user_id,
            "status": "open",
            "messages": []
        }

        await callback.message.answer(
            f"✅ **Тикет #{ticket_id} успешно создан!**\n\n"
            "Отправьте скриншот и детали обращения прямо в этот чат. "
            "Вы можете отправлять сообщения столько раз, сколько нужно, пока админ не закроет тикет."
        )
    await callback.answer()

# Прием сообщений от пользователей в рамках тикета
@dp.message(lambda msg: not is_admin(msg.from_user.id))
async def handle_user_messages(message: types.Message):
    user_id = message.from_user.id

    if user_id not in active_tickets:
        await message.answer(
            "У вас нет активного обращения. Нажмите /start и создайте тикет.",
            reply_markup=get_main_keyboard()
        )
        return

    ticket_id = active_tickets[user_id]
    ticket = tickets[ticket_id]

    text_content = message.text or message.caption or "[Фото/Чек]"
    ticket["messages"].append({
        "sender": f"Пользователь (ID: {user_id})",
        "text": text_content
    })

    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    # Рассылка всем администраторам
    for admin_id in ADMINS:
        try:
            await bot.send_message(
                admin_id,
                f"📩 **Новое сообщение в Тикете #{ticket_id}** от {user_info}:",
                parse_mode="Markdown"
            )
            await message.copy_to(chat_id=admin_id, reply_markup=get_ticket_manage_kb(ticket_id))
        except Exception as e:
            logging.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")

    await message.answer("Сообщение доставлено поддержке Stopka Payments! Ожидайте ответа.")

# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )

@dp.callback_query(F.data == "admin_main_menu")
async def admin_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.clear()
    await callback.message.edit_text(
        "⚙️ **Админ панель — Stopka Payments**",
        parse_mode="Markdown",
        reply_markup=get_admin_main_kb()
    )

# Управление админами
@dp.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    admins_list_text = "\n".join([f"• `{a_id}`" for a_id in ADMINS])
    await callback.message.edit_text(
        f"👥 **Список текущих администраторов:**\n\n{admins_list_text}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admins_manage_kb()
    )

@dp.callback_query(F.data == "admin_add_new")
async def admin_add_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_new_admin_id)
    await callback.message.answer("📝 Введите **Telegram ID** пользователя, которого хотите сделать админом:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_new_admin_id)
async def process_add_new_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    new_admin_id = int(message.text)
    ADMINS.add(new_admin_id)
    await state.clear()
    await message.answer(f"✅ Пользователь `{new_admin_id}` успешно добавлен в список администраторов!", parse_mode="Markdown")

@dp.callback_query(F.data == "admin_remove_exist")
async def admin_remove_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    await state.set_state(AdminStates.waiting_for_remove_admin_id)
    await callback.message.answer("🗑 Введите **Telegram ID** админа, которого нужно удалить:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_admin_id)
async def process_remove_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    remove_id = int(message.text)
    if remove_id == SUPER_ADMIN_ID:
        await message.answer("❌ Нельзя удалить главного администратора!")
        await state.clear()
        return

    if remove_id in ADMINS:
        ADMINS.remove(remove_id)
        await message.answer(f"✅ Администратор `{remove_id}` удален.", parse_mode="Markdown")
    else:
        await message.answer("❌ Пользователь с таким ID не найден в списке админов.")

    await state.clear()

# Список тикетов
@dp.callback_query(F.data == "admin_tickets")
async def admin_list_tickets(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    open_tickets = {t_id: data for t_id, data in tickets.items() if data["status"] == "open"}

    if not open_tickets:
        await callback.message.edit_text(
            "⚙️ **Админ панель**\n\nАктивных тикетов нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="Обновить", callback_data="admin_tickets")],
                [InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")]
            ])
        )
        return

    buttons = []
    for t_id in open_tickets:
        buttons.append([InlineKeyboardButton(text=f"Тикет #{t_id}", callback_data=f"view_ticket_{t_id}")])

    buttons.append([InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text("⚙️ **Админ панель — Сообщения:**", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_ticket_"))
async def admin_view_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("Тикет не найден или уже закрыт.")
        return

    await callback.message.edit_text(
        f"🎫 **Тикет #{ticket_id}**\n"
        f"Пользователь: `{ticket['user_id']}`\n"
        f"Сообщений в истории: {len(ticket['messages'])}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_ticket_manage_kb(ticket_id)
    )

# Просмотр истории сообщений в тикете
@dp.callback_query(F.data.startswith("history_ticket_"))
async def admin_view_ticket_history(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    msg_history = f"📜 **История сообщений — Тикет #{ticket_id}:**\n\n"
    if not ticket["messages"]:
        msg_history += "_История пока пуста._"
    else:
        for item in ticket["messages"]:
            msg_history += f"👤 **{item['sender']}**:\n{item['text']}\n───────────────\n"

    await callback.message.answer(msg_history, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_ticket_"))
async def start_reply_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_for_reply)

    await callback.message.answer(
        f"✍️ Напишите ответ для **Тикета #{ticket_id}** (можно отправлять текст или файлы):"
    )
    await callback.answer()

@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        try:
            await message.send_copy(chat_id=user_id)
            
            # Сохраняем ответ админа в историю тикета
            admin_info = f"Админ (ID: {message.from_user.id})"
            text_content = message.text or message.caption or "[Файл/Фото]"
            ticket["messages"].append({
                "sender": admin_info,
                "text": text_content
            })

            await message.answer(f"✅ Ответ успешно отправлен в **Тикет #{ticket_id}**!")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить сообщение пользователю: {e}")
    else:
        await message.answer("Ошибка: тикет больше не активен.")

    await state.clear()

@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        ticket["status"] = "closed"
        if user_id in active_tickets:
            del active_tickets[user_id]

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
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())