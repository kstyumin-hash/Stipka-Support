import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

from aiohttp import web
from aiogram import Bot, Dispatcher, F, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup

# Логирование
logging.basicConfig(level=logging.INFO)

# Конфигурация из переменных окружения
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPER_ADMIN_ID = int(os.getenv("ADMIN_ID", 0))  # владелец (owner id)
PORT = int(os.getenv("PORT", 8080))

if not BOT_TOKEN or not SUPER_ADMIN_ID:
    raise ValueError("Пожалуйста, укажите BOT_TOKEN и ADMIN_ID в переменных окружения!")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# Время везде по UTC+4
TZ = timezone(timedelta(hours=4))
PAGE_SIZE = 10  # обращений на странице архива

# Множество активных администраторов
ADMINS = {SUPER_ADMIN_ID}

# База данных тикетов (обращений) в памяти
tickets = {}
ticket_counter = 1

# Режим "чата" у админа: admin_id -> ticket_id, с которым он сейчас переписывается
admin_chat_mode = {}

# Сообщения открытой истории: (chat_id, ticket_id) -> [message_id, ...]
history_msgs = {}

TOPIC_LABELS = {"payment": "По поводу оплаты", "other": "Другой вопрос"}

MEDIA_LABELS = {
    "photo": "📷 Фото",
    "video": "🎥 Видео",
    "document": "📎 Файл",
    "sticker": "🎭 Стикер",
    "voice": "🎤 Голосовое",
    "video_note": "⭕ Кружок",
    "audio": "🎵 Аудио",
    "animation": "🎞 GIF",
    "other": "📦 Вложение",
}

WELCOME_TEXT = (
    "👋 <b>Добро пожаловать в Stopka Support!</b>\n\n"
    "Мы отвечаем быстро — просто напишите своё сообщение сюда.\n"
    "Закрыть текущее обращение можно командой /close."
)


# Состояния FSM
class AdminStates(StatesGroup):
    waiting_for_reply = State()
    waiting_for_new_admin_id = State()
    waiting_for_remove_admin_id = State()
    waiting_for_search = State()


# --- ВЕБ-СЕРВЕР ДЛЯ UPTIMEROBOT ---
async def handle_ping(request):
    return web.Response(text="Stopka Support Bot is alive!")


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", handle_ping)
    app.router.add_get("/ping", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logging.info(f"Веб-сервер запущен на порту {PORT}")


# --- МЕНЮ КОМАНД ---
async def set_main_commands(bot: Bot):
    commands = [
        BotCommand(command="start", description="Начало"),
        BotCommand(command="tickets", description="Мои обращения"),
        BotCommand(command="close", description="Закрыть текущее обращение"),
    ]
    await bot.set_my_commands(commands)


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS


# --- ВСПОМОГАТЕЛЬНОЕ ---
def now() -> datetime:
    return datetime.now(TZ)


def fmt_date(dt) -> str:
    return dt.strftime("%d.%m.%y") if dt else "—"


def fmt_time(dt) -> str:
    return f"{dt.hour}:{dt.minute:02d} UTC +4" if dt else "—"


def esc(s) -> str:
    return html.escape(s or "")


def btn(text: str, data: str, style: str = None) -> InlineKeyboardButton:
    """Инлайн-кнопка. style: 'success' (зелёная), 'danger' (красная), 'primary' (синяя).
    Без style — обычная (прозрачная) кнопка."""
    kwargs = {"text": text, "callback_data": data}
    if style:
        kwargs["style"] = style
    return InlineKeyboardButton(**kwargs)


async def safe_edit(callback: types.CallbackQuery, text: str, kb=None):
    """Редактирует сообщение; если это невозможно (например, там медиа) — шлёт новое."""
    try:
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await callback.message.answer(text, parse_mode="HTML", reply_markup=kb)


def extract_content(m: types.Message):
    """Возвращает (type, file_id, text) для любого типа сообщения."""
    if m.photo:
        return "photo", m.photo[-1].file_id, m.caption or ""
    if m.video:
        return "video", m.video.file_id, m.caption or ""
    if m.animation:  # проверяем до document: у GIF заполнено и document
        return "animation", m.animation.file_id, m.caption or ""
    if m.document:
        return "document", m.document.file_id, m.caption or (m.document.file_name or "")
    if m.sticker:
        return "sticker", m.sticker.file_id, m.sticker.emoji or ""
    if m.voice:
        return "voice", m.voice.file_id, m.caption or ""
    if m.video_note:
        return "video_note", m.video_note.file_id, ""
    if m.audio:
        return "audio", m.audio.file_id, m.caption or (m.audio.title or m.audio.file_name or "")
    if m.text:
        return "text", None, m.text
    return "other", None, m.caption or ""


def make_item(sender: str, message: types.Message) -> dict:
    msg_type, file_id, text = extract_content(message)
    return {"sender": sender, "type": msg_type, "text": text, "file_id": file_id, "ts": now()}


def item_preview_text(item: dict) -> str:
    if item.get("type", "text") == "text":
        return item.get("text") or ""
    label = MEDIA_LABELS.get(item["type"], MEDIA_LABELS["other"])
    return f"[{label}]" + (f" {item['text']}" if item.get("text") else "")


async def send_item(chat_id: int, item: dict, caption: str = None):
    """Отправляет сохранённое вложение по его типу."""
    fid = item.get("file_id")
    if not fid:
        return None
    t = item["type"]
    if t == "photo":
        return await bot.send_photo(chat_id, fid, caption=caption)
    if t == "video":
        return await bot.send_video(chat_id, fid, caption=caption)
    if t == "document":
        return await bot.send_document(chat_id, fid, caption=caption)
    if t == "animation":
        return await bot.send_animation(chat_id, fid, caption=caption)
    if t == "voice":
        return await bot.send_voice(chat_id, fid, caption=caption)
    if t == "audio":
        return await bot.send_audio(chat_id, fid, caption=caption)
    if t == "sticker":
        return await bot.send_sticker(chat_id, fid)
    if t == "video_note":
        return await bot.send_video_note(chat_id, fid)
    return None


# --- СТАТУС ТИКЕТА: 🟢 ждёт админа / 🟠 ответил админ / 🔴 закрыт ---
# Ответы бота (вопрос «по поводу оплаты» и т.п.) на статус не влияют:
# они не сохраняются в историю, а статус зависит только от последнего
# сообщения пользователя или админа.
def _admin_answered_last(ticket: dict) -> bool:
    if not ticket["messages"]:
        return False
    return ticket["messages"][-1]["sender"].startswith("Поддержка")


def status_symbol(ticket: dict) -> str:
    if ticket["status"] == "closed":
        return "🔴"
    return "🟠" if _admin_answered_last(ticket) else "🟢"


def status_label(ticket: dict) -> str:
    if ticket["status"] == "closed":
        return "🔴 Закрыт"
    if _admin_answered_last(ticket):
        return "🟠 Открыт (ответ отправлен, ждём клиента)"
    return "🟢 Открыт (ждёт ответа админа)"


def close_ticket_core(ticket_id: int):
    ticket = tickets[ticket_id]
    ticket["status"] = "closed"
    ticket["closed_at"] = now()
    for a_id in [a for a, t in admin_chat_mode.items() if t == ticket_id]:
        admin_chat_mode.pop(a_id, None)


def ticket_card_text(ticket_id: int) -> str:
    t = tickets[ticket_id]
    topic = TOPIC_LABELS.get(t.get("topic"))
    return (
        f"🎫 <b>Тикет #{ticket_id}</b>\n"
        f"Статус: {status_label(t)}\n"
        f"Дата обращения: {fmt_date(t.get('created_at'))}\n"
        f"Пользователь ID: <code>{t['user_id']}</code>\n"
        + (f"Тема: {topic}\n" if topic else "")
        + f"Сообщений в истории: {len(t['messages'])}\n\n"
        "Выберите действие:"
    )


# --- КЛАВИАТУРЫ ПОЛЬЗОВАТЕЛЯ ---
def get_user_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("🗂 Мои обращения", "user_my_tickets")]])


def get_back_to_main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[btn("« Назад", "user_main_menu")]])


def get_payment_question_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("✅ Да", f"pay_q_yes_{ticket_id}", "success")],
            [btn("❌ Нет", f"pay_q_no_{ticket_id}", "danger")],
        ]
    )


def get_user_tickets_kb(user_id: int):
    buttons = []
    for t_id, data in tickets.items():
        if data["user_id"] == user_id:
            buttons.append([btn(f"{status_symbol(data)} Обращение #{t_id}", f"user_view_ticket_{t_id}")])
    buttons.append([btn("« Назад", "user_main_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


USER_TICKETS_TITLE = (
    "🗂 <b>Ваши обращения:</b>\n\n"
    "🟢 — ждёт ответа поддержки\n🟠 — поддержка ответила\n🔴 — закрыто"
)


# --- КЛАВИАТУРЫ АДМИНА ---
def get_admin_main_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📩 Активные тикеты", "admin_tickets_open")],
            [btn("📁 Архив (Закрытые)", "admin_tickets_closed")],
            [btn("👥 Управление админами", "admin_manage_admins")],
        ]
    )


def get_admins_manage_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("➕ Добавить админа", "admin_add_new")],
            [btn("➖ Удалить админа", "admin_remove_exist")],
            [btn("« Назад в меню", "admin_main_menu")],
        ]
    )


def get_admin_ticket_manage_kb(ticket_id: int):
    is_open = tickets.get(ticket_id, {}).get("status") == "open"
    buttons = [[btn("📜 Посмотреть историю", f"history_ticket_{ticket_id}")]]
    if is_open:
        buttons.append([
            btn("Ответить", f"reply_ticket_{ticket_id}"),
            btn("Закрыть", f"close_ticket_{ticket_id}"),
        ])
    back = "admin_tickets_open" if is_open else "admin_tickets_closed"
    buttons.append([btn("« Назад к тикетам", back)])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_chat_mode_kb(ticket_id: int):
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [btn("📜 История", f"history_ticket_{ticket_id}"), btn("🔒 Закрыть", f"close_ticket_{ticket_id}")],
            [btn("« Назад в админ панель", "admin_exit_chat")],
        ]
    )


def get_archive_page(page: int, user_id: int):
    """Страница архива (только закрытые тикеты). Возвращает (текст, клавиатура)."""
    closed_ids = sorted([t for t, d in tickets.items() if d["status"] == "closed"], reverse=True)
    total = len(closed_ids)
    pages = max(1, -(-total // PAGE_SIZE))
    page = min(max(page, 1), pages)
    chunk = closed_ids[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    rows = [[btn("🔍 Поиск", "arch_search", "success")]]
    for t_id in chunk:
        d = tickets[t_id]
        rows.append([btn(
            f"🔴 Тикет #{t_id} · {fmt_date(d.get('created_at'))} · {len(d['messages'])} сообщ.",
            f"view_ticket_{t_id}",
        )])

    if total:
        nav = []
        if page > 1:
            nav.append(btn("⬅️", f"arch_p_{page - 1}", "success"))
        nav.append(btn(f"{page}/{pages}", "noop"))  # прозрачная, никуда не ведёт
        if page < pages:
            nav.append(btn("➡️", f"arch_p_{page + 1}", "success"))
        rows.append(nav)
        if user_id == SUPER_ADMIN_ID:  # очистка только у владельца
            rows.append([btn("🗑 Удалить архив", "arch_clear", "danger")])

    rows.append([btn("« Назад в меню", "admin_main_menu")])

    text = f"📁 <b>Архив тикетов</b>\n\n🔴 — закрыт\nВсего в архиве: {total}"
    if not total:
        text += "\n\nСписок пуст."
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


# --- ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()

    if is_admin(message.from_user.id):
        await message.answer(
            "⚙️ <b>Добро пожаловать в панель поддержки Stopka Support!</b>\n\n"
            "Используйте /admin, чтобы открыть панель администратора.\n"
            "А если хотите написать сообщение в поддержку сами (как клиент) — просто напишите его сюда в чат.",
            parse_mode="HTML",
        )
        return

    await message.answer(WELCOME_TEXT, parse_mode="HTML", reply_markup=get_user_main_kb())


@dp.message(Command("tickets"))
async def cmd_tickets(message: types.Message):
    user_id = message.from_user.id
    if not any(d["user_id"] == user_id for d in tickets.values()):
        await message.answer("У вас пока нет обращений. Просто напишите сообщение, и мы вам ответим.")
        return

    await message.answer(USER_TICKETS_TITLE, parse_mode="HTML", reply_markup=get_user_tickets_kb(user_id))


@dp.message(Command("close"))
async def cmd_close(message: types.Message):
    user_id = message.from_user.id
    open_ids = [t for t, d in tickets.items() if d["user_id"] == user_id and d["status"] == "open"]

    if not open_ids:
        await message.answer("У вас нет открытых обращений.")
        return

    ticket_id = open_ids[-1]
    close_ticket_core(ticket_id)
    await message.answer(f"🔒 Обращение #{ticket_id} закрыто.")

    for admin_id in ADMINS:
        if admin_id == user_id:
            continue
        try:
            await bot.send_message(admin_id, f"🔒 Пользователь закрыл Тикет #{ticket_id}. Он перенесён в архив.")
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")


@dp.callback_query(F.data == "noop")
async def noop_cb(callback: types.CallbackQuery):
    await callback.answer()


@dp.callback_query(F.data == "user_main_menu")
async def user_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await safe_edit(callback, WELCOME_TEXT, get_user_main_kb())
    await callback.answer()


@dp.callback_query(F.data == "user_my_tickets")
async def process_my_tickets(callback: types.CallbackQuery):
    user_id = callback.from_user.id

    if not any(d["user_id"] == user_id for d in tickets.values()):
        await safe_edit(
            callback,
            "У вас пока нет обращений. Просто напишите сообщение, и мы вам ответим.",
            get_back_to_main_kb(),
        )
        await callback.answer()
        return

    await safe_edit(callback, USER_TICKETS_TITLE, get_user_tickets_kb(user_id))
    await callback.answer()


@dp.callback_query(F.data.startswith("user_view_ticket_"))
async def user_view_ticket(callback: types.CallbackQuery):
    ticket_id = int(callback.data.split("_")[3])
    ticket = tickets.get(ticket_id)

    if not ticket or ticket["user_id"] != callback.from_user.id:
        await callback.answer("Обращение не найдено.")
        return

    lines = []
    for item in ticket["messages"]:
        who = "Вы" if item["sender"] == "Пользователь" else "Поддержка"
        lines.append(f"• <b>{who}</b>: {esc(item_preview_text(item)[:1000])}")

    while lines and len("\n".join(lines)) > 3500:
        lines.pop(0)

    msg_history = (
        f"🎫 <b>Обращение #{ticket_id}</b> (Статус: {status_label(ticket)})\n\n"
        "📜 <b>История переписки:</b>\n"
        + ("\n".join(lines) if lines else "<i>Сообщений пока нет.</i>")
    )

    await safe_edit(callback, msg_history, InlineKeyboardMarkup(
        inline_keyboard=[[btn("« К списку обращений", "user_my_tickets")]]
    ))
    await callback.answer()


# --- АДМИН-ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await message.answer(
        "⚙️ <b>Админ панель — Stopka Support</b>",
        parse_mode="HTML",
        reply_markup=get_admin_main_kb(),
    )


@dp.callback_query(F.data == "admin_main_menu")
async def admin_main_menu_cb(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    admin_chat_mode.pop(callback.from_user.id, None)
    await state.clear()
    await safe_edit(callback, "⚙️ <b>Админ панель — Stopka Support</b>", get_admin_main_kb())
    await callback.answer()


@dp.callback_query(F.data == "admin_exit_chat")
async def admin_exit_chat(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    admin_chat_mode.pop(callback.from_user.id, None)
    await state.clear()
    await safe_edit(callback, "⚙️ <b>Админ панель — Stopka Support</b>", get_admin_main_kb())
    await callback.answer()


# Управление списком администраторов
@dp.callback_query(F.data == "admin_manage_admins")
async def admin_manage_admins(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    admins_list_text = "\n".join([f"• <code>{a_id}</code>" for a_id in ADMINS])
    await safe_edit(
        callback,
        f"👥 <b>Список текущих администраторов:</b>\n\n{admins_list_text}\n\nВыберите действие:",
        get_admins_manage_kb(),
    )
    await callback.answer()


@dp.callback_query(F.data == "admin_add_new")
async def admin_add_new_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_for_new_admin_id)
    await callback.message.answer(
        "📝 Введите <b>Telegram ID</b> пользователя, которого хотите сделать админом:", parse_mode="HTML"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_new_admin_id)
async def process_add_new_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр. Попробуйте еще раз:")
        return

    new_admin_id = int(message.text)
    ADMINS.add(new_admin_id)
    await state.clear()
    await message.answer(
        f"✅ Пользователь <code>{new_admin_id}</code> успешно добавлен в список администраторов!",
        parse_mode="HTML",
    )


@dp.callback_query(F.data == "admin_remove_exist")
async def admin_remove_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_for_remove_admin_id)
    await callback.message.answer(
        "🗑 Введите <b>Telegram ID</b> админа, которого нужно удалить:", parse_mode="HTML"
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_remove_admin_id)
async def process_remove_admin(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    if not message.text or not message.text.isdigit():
        await message.answer("❌ Telegram ID должен состоять только из цифр:")
        return

    remove_id = int(message.text)
    if remove_id == SUPER_ADMIN_ID:
        await message.answer("❌ Нельзя удалить главного администратора!")
        await state.clear()
        return

    if remove_id in ADMINS:
        ADMINS.remove(remove_id)
        admin_chat_mode.pop(remove_id, None)
        await message.answer(f"✅ Администратор <code>{remove_id}</code> удален.", parse_mode="HTML")
    else:
        await message.answer("❌ Пользователь с таким ID не найден в списке админов.")

    await state.clear()


# Активные тикеты
@dp.callback_query(F.data == "admin_tickets_open")
async def admin_list_open(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    open_tickets = {t: d for t, d in tickets.items() if d["status"] == "open"}
    title = (
        "📩 <b>Активные тикеты:</b>\n\n"
        "🟢 — ждёт ответа админа\n"
        "🟠 — ответ отправлен, ждём клиента"
    )

    buttons = []
    for t_id, t_data in open_tickets.items():
        buttons.append([btn(
            f"{status_symbol(t_data)} Тикет #{t_id} · {fmt_date(t_data.get('created_at'))} · {len(t_data['messages'])} сообщ.",
            f"view_ticket_{t_id}",
        )])
    buttons.append([btn("« Назад в меню", "admin_main_menu")])

    if not open_tickets:
        title += "\n\nСписок пуст."

    await safe_edit(callback, title, InlineKeyboardMarkup(inline_keyboard=buttons))
    await callback.answer()


# Архив (только закрытые): страницы, поиск, очистка
@dp.callback_query(F.data.startswith("arch_p_") | (F.data == "admin_tickets_closed"))
async def admin_archive(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    if await state.get_state() == AdminStates.waiting_for_search.state:
        await state.clear()

    page = int(callback.data.split("_")[2]) if callback.data.startswith("arch_p_") else 1
    text, kb = get_archive_page(page, callback.from_user.id)
    await safe_edit(callback, text, kb)
    await callback.answer()


@dp.callback_query(F.data == "arch_search")
async def arch_search_start(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    await state.set_state(AdminStates.waiting_for_search)
    await callback.message.answer(
        "🔍 Введите номер тикета:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("« Отмена", "admin_tickets_closed")]]),
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_search)
async def arch_search_process(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    raw = (message.text or "").strip().lstrip("#")
    if not raw.isdigit():
        await message.answer("❌ Введите номер тикета цифрами:")
        return

    await state.clear()
    t_id = int(raw)

    if t_id not in tickets:
        await message.answer(
            f"❌ Тикета #{t_id} нет.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[btn("« К архиву", "admin_tickets_closed")]]),
        )
        return

    await message.answer(ticket_card_text(t_id), parse_mode="HTML", reply_markup=get_admin_ticket_manage_kb(t_id))


@dp.callback_query(F.data == "arch_clear")
async def arch_clear(callback: types.CallbackQuery):
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer("Очистка архива доступна только владельцу.", show_alert=True)
        return

    count = sum(1 for d in tickets.values() if d["status"] == "closed")
    if not count:
        await callback.answer("Архив пуст.", show_alert=True)
        return

    await safe_edit(
        callback,
        f"🗑 Удалить все закрытые обращения ({count})?\nЭто действие нельзя отменить.",
        InlineKeyboardMarkup(inline_keyboard=[
            [btn("🗑 Да, удалить", "arch_clear_yes", "danger")],
            [btn("« Отмена", "admin_tickets_closed")],
        ]),
    )
    await callback.answer()


@dp.callback_query(F.data == "arch_clear_yes")
async def arch_clear_yes(callback: types.CallbackQuery):
    if callback.from_user.id != SUPER_ADMIN_ID:
        await callback.answer("Очистка архива доступна только владельцу.", show_alert=True)
        return

    closed_ids = [t for t, d in tickets.items() if d["status"] == "closed"]
    for t_id in closed_ids:
        tickets.pop(t_id, None)

    await safe_edit(
        callback,
        f"✅ Архив очищен. Удалено обращений: {len(closed_ids)}",
        InlineKeyboardMarkup(inline_keyboard=[[btn("« К архиву", "admin_tickets_closed")]]),
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("view_ticket_"))
async def admin_view_ticket(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    if ticket_id not in tickets:
        await callback.answer("Тикет не найден.")
        return

    await safe_edit(callback, ticket_card_text(ticket_id), get_admin_ticket_manage_kb(ticket_id))
    await callback.answer()


# --- ИСТОРИЯ ---
def format_history_block(ticket: dict, item: dict) -> str:
    if item["sender"] == "Пользователь":
        label = f"Пользователь (<code>{ticket['user_id']}</code>)"
        topic = TOPIC_LABELS.get(ticket.get("topic"))
        if topic:
            label += f" — {topic}"  # «По поводу оплаты» / «Другой вопрос»
    else:
        label = esc(item["sender"])
    body = esc(item_preview_text(item)[:3000])
    return f"👤 {label}:\n{body} - {fmt_time(item.get('ts'))}"


def pack_blocks(header: str, blocks: list, limit: int = 3800) -> list:
    chunks, cur = [], header
    for b in blocks:
        if len(cur) + len(b) + 2 > limit:
            chunks.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    chunks.append(cur)
    return chunks


async def delete_history_msgs(chat_id: int, ticket_id: int):
    for mid in history_msgs.pop((chat_id, ticket_id), []):
        try:
            await bot.delete_message(chat_id, mid)
        except Exception:
            pass


# Просмотр истории админом: текст — общим сообщением (с датой и временем),
# вложения — отдельными сообщениями, внизу красная кнопка «Закрыть историю».
@dp.callback_query(F.data.startswith("history_ticket_"))
async def admin_view_ticket_history(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)
    if not ticket:
        await callback.answer("Тикет не найден.")
        return

    await callback.answer()
    chat_id = callback.message.chat.id
    await delete_history_msgs(chat_id, ticket_id)  # убираем прошлую открытую копию истории
    sent_ids = []

    header = (
        f"📜 <b>История переписки — Тикет #{ticket_id}</b>\n"
        f"📅 Дата обращения: {fmt_date(ticket.get('created_at'))}"
    )
    blocks = [format_history_block(ticket, item) for item in ticket["messages"]]
    if not blocks:
        blocks = ["<i>Сообщений пока нет.</i>"]

    for chunk in pack_blocks(header, blocks):
        m = await bot.send_message(chat_id, chunk, parse_mode="HTML")
        sent_ids.append(m.message_id)

    media_items = [i for i in ticket["messages"] if i.get("type") != "text" and i.get("file_id")]
    if media_items:
        m = await bot.send_message(chat_id, "📎 <b>Вложения из этого тикета:</b>", parse_mode="HTML")
        sent_ids.append(m.message_id)
        for item in media_items:
            caption = f"{item['sender']} · {fmt_time(item.get('ts'))}"
            if item.get("text"):
                caption = f"{item['sender']}:\n{item['text'][:800]}\n{fmt_time(item.get('ts'))}"
            try:
                m = await send_item(chat_id, item, caption)
                if m:
                    sent_ids.append(m.message_id)
            except Exception as e:
                logging.error(f"Ошибка отображения вложения тикета #{ticket_id}: {e}")

    m = await bot.send_message(
        chat_id,
        "⬆️ Конец истории",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[btn("🔴 Закрыть историю", f"hist_close_{ticket_id}", "danger")]]
        ),
    )
    sent_ids.append(m.message_id)
    history_msgs[(chat_id, ticket_id)] = sent_ids


@dp.callback_query(F.data.startswith("hist_close_"))
async def admin_close_history(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    chat_id = callback.message.chat.id
    await delete_history_msgs(chat_id, ticket_id)
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.answer()


# --- ОТВЕТ АДМИНА (режим чата) ---
@dp.callback_query(F.data.startswith("reply_ticket_"))
async def start_reply_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)

    if not ticket or ticket["status"] != "open":
        await callback.answer("Тикет закрыт или не найден.", show_alert=True)
        return

    admin_id = callback.from_user.id
    admin_chat_mode[admin_id] = ticket_id

    await state.update_data(reply_ticket_id=ticket_id)
    await state.set_state(AdminStates.waiting_for_reply)

    await callback.message.answer(
        f"💬 <b>Режим чата включён — Тикет #{ticket_id}</b>\n\n"
        "Просто пишите сообщения (текст, фото, видео, стикеры, файлы) — они сразу уходят пользователю, как в обычном чате.\n"
        "Чтобы выйти — нажмите «Назад в админ панель».",
        parse_mode="HTML",
        reply_markup=get_admin_chat_mode_kb(ticket_id),
    )
    await callback.answer()


@dp.message(AdminStates.waiting_for_reply)
async def send_reply_to_user(message: types.Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return

    admin_id = message.from_user.id
    data = await state.get_data()
    ticket_id = data.get("reply_ticket_id")
    ticket = tickets.get(ticket_id)

    if ticket and ticket["status"] == "open":
        try:
            await message.copy_to(chat_id=ticket["user_id"])
            ticket["messages"].append(make_item(f"Поддержка (Админ {admin_id})", message))
            # Режим чата остаётся включённым — состояние НЕ сбрасываем.
        except Exception as e:
            await message.answer(f"❌ Не удалось отправить сообщение пользователю: {e}")
    else:
        await message.answer("⚠️ Тикет закрыт или не найден — режим чата выключен.")
        admin_chat_mode.pop(admin_id, None)
        await state.clear()


@dp.callback_query(F.data.startswith("close_ticket_"))
async def close_ticket(callback: types.CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        return

    ticket_id = int(callback.data.split("_")[2])
    ticket = tickets.get(ticket_id)
    admin_id = callback.from_user.id

    if ticket and ticket["status"] == "open":
        was_in_chat = admin_chat_mode.get(admin_id) == ticket_id
        close_ticket_core(ticket_id)

        try:
            await bot.send_message(
                ticket["user_id"],
                f"🔒 Ваше <b>Обращение #{ticket_id}</b> было закрыто администратором.",
                parse_mode="HTML",
            )
        except Exception:
            pass

        if was_in_chat:
            await state.clear()

        await safe_edit(
            callback,
            f"🔴 <b>Тикет #{ticket_id} перенесен в архив.</b>",
            InlineKeyboardMarkup(inline_keyboard=[[btn("« Назад в админ панель", "admin_main_menu")]]),
        )
        await callback.answer()
    else:
        await callback.answer("Тикет уже закрыт.")


# --- ВОПРОС «ПО ПОВОДУ ОПЛАТЫ» ---
# Ответ бота пользователю одинаковый; в историю он не пишется —
# админ видит выбор пользователя рядом с его ID («По поводу оплаты» / «Другой вопрос»).
@dp.callback_query(F.data.startswith("pay_q_"))
async def payment_question(callback: types.CallbackQuery):
    _, _, answer, raw_id = callback.data.split("_")
    ticket = tickets.get(int(raw_id))

    if not ticket or ticket["user_id"] != callback.from_user.id or ticket.get("topic"):
        await callback.answer()
        return

    ticket["topic"] = "payment" if answer == "yes" else "other"
    await safe_edit(callback, "Администрация скоро вам ответит.")
    await callback.answer()


# --- ОБЫЧНЫЕ СООБЩЕНИЯ ПОЛЬЗОВАТЕЛЯ (любые типы: текст, фото, видео, стикеры, файлы...) ---
# ВАЖНО: этот хендлер должен быть зарегистрирован последним,
# чтобы не перехватывать команды и состояния FSM админа.
@dp.message(F.chat.type == "private")
async def handle_user_message(message: types.Message, state: FSMContext):
    if message.text and message.text.startswith("/"):
        return  # неизвестные команды не превращаем в обращения

    global ticket_counter
    user_id = message.from_user.id
    open_tickets = [t for t, d in tickets.items() if d["user_id"] == user_id and d["status"] == "open"]

    is_new_ticket = False
    if open_tickets:
        ticket_id = open_tickets[-1]
    else:
        ticket_id = ticket_counter
        ticket_counter += 1
        tickets[ticket_id] = {
            "user_id": user_id,
            "status": "open",
            "created_at": now(),
            "topic": None,
            "messages": [],
        }
        is_new_ticket = True

    ticket = tickets[ticket_id]
    ticket["messages"].append(make_item("Пользователь", message))

    user_info = f"@{esc(message.from_user.username)}" if message.from_user.username else f"ID: {user_id}"
    header = (
        f"🆕 <b>Новый тикет #{ticket_id}</b> от {user_info}:" if is_new_ticket
        else f"📩 <b>Новое сообщение в Тикете #{ticket_id}</b> от {user_info}:"
    )

    for admin_id in ADMINS:
        if admin_id == user_id:
            continue
        try:
            if admin_chat_mode.get(admin_id) == ticket_id:
                # Админ уже в режиме чата с этим тикетом — показываем сообщение как в живом диалоге.
                await message.copy_to(chat_id=admin_id)
            else:
                await bot.send_message(admin_id, header, parse_mode="HTML")
                await message.copy_to(chat_id=admin_id, reply_markup=get_admin_ticket_manage_kb(ticket_id))
        except Exception as e:
            logging.error(f"Ошибка отправки админу {admin_id}: {e}")

    if is_new_ticket:
        await message.answer("Вы по поводу оплаты?", reply_markup=get_payment_question_kb(ticket_id))


# --- ЗАПУСК ---
async def main():
    await set_main_commands(bot)
    await start_web_server()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
