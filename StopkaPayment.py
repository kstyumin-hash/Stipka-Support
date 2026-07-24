import asyncio
import logging
import os
from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

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

# База данных тикетов в памяти
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

# --- УСТАНОВКА МЕНЮ КОМАНД ---
async def set_main_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начало"),
        BotCommand(command="tickets", description="Мои тикеты"),
    ]
    await bot.set_my_commands(commands)

# --- ПРОВЕРКА НА АДМИНА ---
def is_admin(user_id: int) -> bool:
    return user_id in ADMINS

# --- КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ ---
def get_user_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Создать новый тикет", callback_data="user_create_ticket")],
            [InlineKeyboardButton(text="🗂 Мои тикеты", callback_data="user_my_tickets")]
        ]
    )

def get_user_tickets_kb(user_id: int):
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]
    buttons = []

    for t_id in user_tickets:
        status_symbol = "🟢" if tickets[t_id]["status"] == "open" else "🔴"
        buttons.append([InlineKeyboardButton(text=f"{status_symbol} Тикет #{t_id}", callback_data=f"user_view_ticket_{t_id}")])

    buttons.append([InlineKeyboardButton(text="« Назад", callback_data="user_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# --- КЛАВИАТУРЫ АДМИНА ---
def get_admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📩 Активные тикеты", callback_data="admin_tickets_open")],
            [InlineKeyboardButton(text="📁 Архив (Закрытые)", callback_data="admin_tickets_closed")],
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

def get_admin_ticket_manage_kb(ticket_id: int):
    is_open = tickets.get(ticket_id, {}).get("status") == "open"
    buttons = [
        [InlineKeyboardButton(text="📜 Посмотреть историю", callback_data=f"history_ticket_{ticket_id}")]
    ]
    if is_open:
        buttons.append([
            InlineKeyboardButton(text="Ответить", callback_data=f"reply_ticket_{ticket_id}"),
            InlineKeyboardButton(text="Закрыть", callback_data=f"close_ticket_{ticket_id}")
        ])
    buttons.append([InlineKeyboardButton(text="« Назад к тикетам", callback_data="admin_tickets_open")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 **Добро пожаловать в поддержку Stopka Payments!**\n\n"
        "Чтобы отправить сообщение в открытый тикет, используйте команду:\n"
        "`/to текст вашего сообщения` (или прикрепите чек с подписью `/to`)",
        parse_mode="Markdown",
        reply_markup=get_user_main_kb()
    )

@dp.message(Command("tickets"))
async def cmd_tickets(message: types.Message):
    user_id = message.from_user.id
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]

    if not user_tickets:
        await message.answer("У вас пока нет созданных тикетов.", reply_markup=get_user_main_kb())
        return

    await message.answer(
        "🗂 **Ваши тикеты:**\n\n🟢 — Открыт\n🔴 — Закрыт",
        parse_mode="Markdown",
        reply_markup=get_user_tickets_kb(user_id)
    )

@dp.callback_query(F.data == "user_main_menu")
async def user_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "👋 **Главное меню поддержки Stopka Payments**",
        reply_markup=get_user_main_kb()
    )

@dp.callback_query(F.data == "user_create_ticket")
async def process_create_ticket(callback: types.CallbackQuery):
    global ticket_counter
    user_id = callback.from_user.id

    ticket_id = ticket_counter
    ticket_counter += 1

    tickets[ticket_id] = {
        "user_id": user_id,
        "status": "open",
        "messages": []
    }

    await callback.message.answer(
        f"✅ **Тикет #{ticket_id} успешно создан!**\n\n"
        "Чтобы отправить текст или скриншот оплаты, используйте команду:\n"
        f"`/to ваш текст` (или `/to` в подписи к фото)",
        parse_mode="Markdown"
    )
    await callback.answer()

@dp.callback_query(F.data == "user_my_tickets")
async def process_my_tickets(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    user_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id]

    if not user_tickets:
        await callback.message.edit_text(
            "У вас пока нет созданных тикетов.",
            reply_markup=get_user_main_kb()
        )
        return

    await callback.message.edit_text(
        "🗂 **Ваши тикеты:**\n\n🟢 — Открыт\n🔴 — Закрыт",
        parse_mode="Markdown",
        reply_markup=get_user_tickets_kb(user_id)
    )

@dp.callback_query(F.data.startswith("user_view_ticket_"))
async def user_view_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[3])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    status_str = "🟢 Открыт" if ticket["status"] == "open" else "🔴 Закрыт"
    
    msg_history = f"🎫 **Тикет #{ticket_id}** (Статус: {status_str})\n\n"
    msg_history += "📜 **История переписки:**\n"
    
    if not ticket["messages"]:
        msg_history += "_Сообщений пока нет._\n"
    else:
        for item in ticket["messages"]:
            msg_history += f"• **{item['sender']}**: {item['text']}\n"

    kb_buttons = [[InlineKeyboardButton(text="« К списку тикетов", callback_data="user_my_tickets")]]

    await callback.message.edit_text(
        msg_history,
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_buttons)
    )

# --- ОТПРАВКА СООБЩЕНИЯ ЧЕРЕЗ /to ---
@dp.message(Command("to"))
async def handle_user_to_command(message: types.Message, command: CommandObject):
    user_id = message.from_user.id

    # Находим последний открытый тикет пользователя
    open_tickets = [t_id for t_id, data in tickets.items() if data["user_id"] == user_id and data["status"] == "open"]

    if not open_tickets:
        await message.answer("У вас нет открытых тикетов! Создайте тикет через /start", reply_markup=get_user_main_kb())
        return

    ticket_id = open_tickets[-1]  # Берем самый свежий открытый тикет
    ticket = tickets[ticket_id]

    msg_text = command.args if command.args else (message.caption.replace("/to", "").strip() if message.caption else "[Чек/Скриншот]")

    if not msg_text and not message.photo:
        await message.answer("Пожалуйста, напишите текст после команды, например: `/to Оплатил 500р`", parse_mode="Markdown")
        return

    # Сохраняем в историю тикета
    ticket["messages"].append({
        "sender": "Пользователь",
        "text": msg_text
    })

    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"

    # Отправляем уведомления админам
    for admin_id in ADMINS:
        try:
            await bot.send_message(
                admin_id,
                f"📩 **Новое сообщение в Тикете #{ticket_id}** от {user_info}:\nТекст: {msg_text}",
                parse_mode="Markdown"
            )
            await message.copy_to(chat_id=admin_id, reply_markup=get_admin_ticket_manage_kb(ticket_id))
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

    await message.answer("Ваше сообщение доставлено")


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

# Управление списком администраторов
@dp.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
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
        return

    await state.set_state(AdminStates.waiting_for_remove_admin_id)
    await callback.message.answer("🗑 Введите **Telegram ID** админа, которого нужно удалить:")
    await callback.answer()

@dp.message(AdminStates.waiting_for_remove_admin_id)
async def process_remove_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр:")
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

# Списки тикетов (Открытые / Архив)
@dp.callback_query(F.data.in_({"admin_tickets_open", "admin_tickets_closed"}))
async def admin_list_tickets(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    status_filter = "open" if callback.data == "admin_tickets_open" else "closed"
    filtered_tickets = {t_id: data for t_id, data in tickets.items() if data["status"] == status_filter}

    title = "📩 **Активные тикеты:**" if status_filter == "open" else "📁 **Архив тикетов:**"

    if not filtered_tickets:
        await callback.message.edit_text(
            f"{title}\n\nСписок пуст.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")]
            ])
        )
        return

    buttons = []
    for t_id in filtered_tickets:
        buttons.append([InlineKeyboardButton(text=f"Тикет #{t_id} (Сообщений: {len(tickets[t_id]['messages'])})", callback_data=f"view_ticket_{t_id}")])

    buttons.append([InlineKeyboardButton(text="« Назад в меню", callback_data="admin_main_menu")])

    kb = InlineKeyboardMarkup(inline_keyboard=buttons)
    await callback.message.edit_text(title, parse_mode="Markdown", reply_markup=kb)

@dp.callback_query(F.data.startswith("view_ticket_"))
async def admin_view_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    status_txt = "🟢 Открыт" if ticket['status'] == 'open' else "🔴 Закрыт"

    await callback.message.edit_text(
        f"🎫 **Тикет #{ticket_id}**\n"
        f"Статус: {status_txt}\n"
        f"Пользователь ID: `{ticket['user_id']}`\n"
        f"Сообщений в истории: {len(ticket['messages'])}\n\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=get_admin_ticket_manage_kb(ticket_id)
    )

# Просмотр истории сообщений админом
@dp.callback_query(F.data.startswith("history_ticket_"))
async def admin_view_ticket_history(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    msg_history = f"📜 **История переписки — Тикет #{ticket_id}:**\n\n"
    if not ticket["messages"]:
        msg_history += "_История пуста._"
    else:
        for item in ticket["messages"]:
            msg_history += f"👤 **{item['sender']}**:\n{item['text']}\n───────────────\n"

    await callback.message.answer(msg_history, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("reply_ticket_"))
async def start_reply_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_for_reply)

    await callback.message.answer(
        f"✍️ Напишите ответ для **Тикета #{ticket_id}** (можно отправлять текст или фото):"
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

            text_content = message.text or message.caption or "[Фото/Чек]"
            ticket["messages"].append({
                "sender": f"Поддержка (Админ {message.from_user.id})",
                "text": text_content
            })

            await message.answer(f"✅ Ответ успешно отправлен в **Тикет #{ticket_id}**!")
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить сообщение пользователю: {e}")
    else:
        await message.answer("Ошибка: тикет закрыт или не найден.")

    await state.clear()

@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        user_id = ticket["user_id"]
        ticket["status"] = "closed"

        try:
            await bot.send_message(
                user_id,
                f"🔒 Ваш **Тикет #{ticket_id}** был закрыт администратором."
            )
        except Exception:
            pass

        await callback.message.edit_text(f"🔴 **Тикет #{ticket_id} перенесен в архив.**")
    else:
        await callback.answer("Тикет уже закрыт.")

# --- ЗАПУСК ---
async def main():
    await set_main_commands(bot)
    await start_web_server()
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())