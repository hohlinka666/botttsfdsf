import asyncio
import html
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand, BotCommandScopeChat, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, KeyboardButton, MenuButtonWebApp, Message, ReplyKeyboardMarkup, WebAppInfo,
)
from dotenv import load_dotenv

# Вся админка (/opcaadmin), команды, ранги, отчёты, рассылки, CSV, статистика — в teams.py.
# Старых админ-хендлеров (/admin, admin:* и т.п.) здесь больше нет.
from teams import (
    ADDER_RANKS, BTN_ADMIN, BTN_HELP, BTN_PROFILE, BTN_REPORT, BTN_TEAM, BTN_TOP,
    application_admin_kb, claim_username_invites, count_pending_apps, count_pending_reports,
    count_pending_team_requests, handle_start_payload, init_team_db, latest_team_request, rank_label,
    ENGLISH_LEVELS, age_from, age_problem, parse_birth, parse_country, parse_exp, parse_fio, parse_phone,
    parse_username, setup_teams, today_line, who, years_word,
)

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("bot")

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@vuittonagency").strip()
CHANNEL_URL = f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
DB_PATH = os.getenv("DB_PATH", "vuitton.db")
MODEL_CONTACT = "https://t.me/vuittonceo"
# Mini App: публичный https-адрес, на который смотрит WEBAPP_PORT (см. README). Пусто — мини-приложение выключено.
WEBAPP_URL = os.getenv("WEBAPP_URL", "").strip().rstrip("/")
WEBAPP_HOST = os.getenv("WEBAPP_HOST", "0.0.0.0")
WEBAPP_PORT = int(os.getenv("WEBAPP_PORT", "8080") or 8080)

if not BOT_TOKEN:
    raise RuntimeError("Укажи BOT_TOKEN в .env")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()


def esc(value) -> str:
    """Экранирование пользовательского текста для parse_mode=HTML."""
    return html.escape(str(value or ""))


# -------------------- DATABASE --------------------

@contextmanager
def db():
    """Соединение, которое реально закрывается (sqlite3 `with` его не закрывает)."""
    con = sqlite3.connect(DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


def init_db():
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            role TEXT DEFAULT 'user',
            model_interest INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_seen TEXT NOT NULL
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            answers TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(user_id, kind)
        )""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""")
        # миграция для уже существующих баз
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        if "is_blocked" not in cols:
            con.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        con.execute("CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_apps_user ON applications(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_users_created ON users(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_id)")


def now() -> str:
    # UTC везде: строки сравнимы между собой и не ломаются из-за смены пояса/DST
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def track(user_id: int, event: str):
    with db() as con:
        con.execute("INSERT INTO events(user_id,event,created_at) VALUES(?,?,?)", (user_id, event, now()))


def upsert_user(user):
    ts = now()
    with db() as con:
        con.execute("""
            INSERT INTO users(user_id,username,first_name,created_at,last_seen)
            VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                first_name=excluded.first_name,
                last_seen=excluded.last_seen,
                is_blocked=0
        """, (user.id, user.username or "", user.first_name or "", ts, ts))


def get_user(user_id):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def add_application(user_id: int, kind: str, answers: list[str]) -> bool:
    """False, если заявка такого типа уже есть (UNIQUE(user_id, kind))."""
    try:
        with db() as con:
            con.execute(
                "INSERT INTO applications(user_id,kind,answers,status,created_at) VALUES(?,?,?,?,?)",
                (user_id, kind, "\n".join(answers), "pending", now())
            )
            # роль в users.role — только «витринная»; ранг назначается при одобрении (teams.py)
            con.execute("UPDATE users SET role=? WHERE user_id=? AND role='user'", (kind, user_id))
        return True
    except sqlite3.IntegrityError:
        return False


def application_for(user_id: int, kind: str):
    with db() as con:
        return con.execute("SELECT * FROM applications WHERE user_id=? AND kind=?", (user_id, kind)).fetchone()


def get_application_id(user_id: int, kind: str):
    row = application_for(user_id, kind)
    return row["id"] if row else None


def set_model_interest(user_id: int) -> bool:
    """True, если интерес отмечен впервые."""
    with db() as con:
        cur = con.execute("UPDATE users SET model_interest=1 WHERE user_id=? AND model_interest=0", (user_id,))
        return cur.rowcount > 0


# -------------------- KEYBOARDS / STATES --------------------

BTN_OPERATOR = "🎧 Оператор чата"
BTN_SCOUT = "🔎 Скаут моделей"
BTN_MODEL = "📸 Модель"
BTN_ABOUT = "ℹ️ О проекте"
BTN_APPS = "📋 Мои заявки"
BTN_APP = "📱 Кабинет"


def user_rank(user_id):
    """Ранг участника (scout/operator/…) или None, если человек ещё не в команде."""
    if not user_id:
        return None
    try:
        row = get_user(user_id)
        return row["rank"] if row else None
    except (sqlite3.Error, IndexError, KeyError):
        return None


def main_kb(user_id=None):
    """Меню зависит от того, кто его видит: гость выбирает путь, участник — работает, админ — управляет."""
    kb = []
    is_adm = user_id in ADMIN_IDS
    if user_rank(user_id):
        kb.append([KeyboardButton(text=BTN_REPORT), KeyboardButton(text=BTN_PROFILE)])
        kb.append([KeyboardButton(text=BTN_TEAM), KeyboardButton(text=BTN_TOP)])
    else:
        if is_adm:
            kb.append([KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_TOP)])
        else:
            kb.append([KeyboardButton(text=BTN_OPERATOR), KeyboardButton(text=BTN_SCOUT)])
            kb.append([KeyboardButton(text=BTN_MODEL), KeyboardButton(text=BTN_APPS)])
    tail = [KeyboardButton(text=BTN_APP)] if WEBAPP_URL else [KeyboardButton(text=BTN_ABOUT)]
    tail.append(KeyboardButton(text=BTN_HELP))
    kb.append(tail)
    if is_adm:
        kb.insert(0, [KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, input_field_placeholder="Выберите действие")


def webapp_kb():
    # Mini App получает подпись пользователя (initData) только из inline-кнопки или кнопки меню —
    # поэтому запускаем через inline, а не через reply-клавиатуру.
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Открыть кабинет", web_app=WebAppInfo(url=WEBAPP_URL))]])


def subscribe_kb(payload: str = ""):
    # payload (ref_xxx / report) едет в callback_data, чтобы реферальная ссылка не терялась
    cb = f"check_sub:{payload}" if payload else "check_sub"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=CHANNEL_URL)],
        [InlineKeyboardButton(text="🔄 Проверить подписку", callback_data=cb)]
    ])


class Apply(StatesGroup):
    answers = State()


# -------------------- ТЕКСТЫ --------------------

# Одна анкета для всех ролей: ФИО, дата рождения, телефон, Telegram, английский, опыт, наушники, страна.
APP_STEPS = ["fio", "birth", "phone", "tg", "en", "exp", "hp", "country"]
APP_PROMPTS = {
    "fio": "Фамилия и имя (например: <i>Иванов Иван</i>)",
    "birth": "Дата рождения в формате <i>ДД.ММ.ГГГГ</i>",
    "phone": "Номер телефона (например: <i>+7 900 123-45-67</i>)",
    "tg": "Ваш username в Telegram (например <i>@ivan_petrov</i>)",
    "en": "Ваш уровень английского",
    "exp": "Ваш опыт работы (коротко: где и сколько). Если опыта нет — нажмите кнопку.",
    "hp": "Есть ли у вас наушники?",
    "country": "В какой стране вы живёте?",
}
KIND_INFO = {
    "operator": {
        "title": "Оператор чата",
        "desc": ("🎧 <b>Оператор чата</b>\n\n"
                 "Ведёте переписку с подписчиками от лица моделей агентства, следуете скриптам и "
                 "стандартам качества. Обучение и поддержка наставника — с первого дня.\n"
                 "<i>Работа доступна с 18 лет.</i>"),
    },
    "scout": {
        "title": "Скаут моделей",
        "desc": ("🔎 <b>Скаут моделей</b>\n\n"
                 "Находите и приводите в агентство новых людей. Получаете скрипты, поддержку команды "
                 "и рост по рангам: от скаута до партнёра.\n"
                 "<i>Возраст не ограничен — скаутом можно стать и до 18 лет.</i>"),
    },
}
QUESTION_LABELS = {"fio": "ФИО", "birth": "Дата рождения", "phone": "Телефон", "tg": "Telegram",
                   "en": "Английский", "exp": "Опыт", "hp": "Наушники", "country": "Страна"}
STATUS_TITLES = {"pending": "⏳ на рассмотрении", "approved": "✅ одобрена", "rejected": "❌ отклонена"}

ABOUT_TEXT = (
    "ℹ️ <b>О проекте</b>\n\n"
    "Vuitton Agency — агентство по работе с моделями. Мы набираем операторов чатов и скаутов, "
    "обучаем, ставим в команды и растим по рангам.\n\n"
    "🔹 Операторы и скауты работают в командах под руководством тимлидов\n"
    "🔹 Ежедневные отчёты и прозрачная статистика\n"
    "🔹 Рост: Скаут → Младший тимлид → Старший тимлид → Сквадлид → Партнёр\n\n"
    "Новости и объявления — в нашем канале."
)

def help_text(user_id: int) -> str:
    rank = user_rank(user_id)
    L = ["📖 <b>Как пользоваться ботом</b>", ""]
    if rank:
        L += [
            "<b>Каждый день</b>",
            f"{BTN_REPORT} (/report) — сдать отчёт: часы и сколько людей привели. Кнопки со значениями — в один тап.",
            f"{BTN_PROFILE} (/profile) — ваш ранг, статистика, статус отчёта за сегодня, «Мои отчёты».",
            "",
            "<b>Команда</b>",
            f"{BTN_TEAM} (/team) — состав, ссылка для вступления, кто не сдал отчёт.",
        ]
        if rank in ADDER_RANKS:
            L.append("🆕 /addworker — привести нового работника: заполняете анкету, бот запомнит его и сам "
                     "определит в команду.")
            L.append("➕ /addmember — позвать в свою команду того, кто уже работает в боте (он подтвердит приглашение).")
        if rank == "jr_tl":
            L.append("🙋 /requestteam — запросить свою команду (если зарабатываете на своих скаутах).")
        L.append(f"{BTN_TOP} (/top) — рейтинг за 7 дней.")
    else:
        L += [
            "<b>Как начать работать</b>",
            f"1. Выберите, кем хотите быть: «{BTN_OPERATOR}» или «{BTN_SCOUT}», и ответьте на несколько вопросов.",
            f"2. Следите за статусом в «{BTN_APPS}» (/status) — я напишу, когда заявку рассмотрят.",
            "3. Если вас пригласил наставник — просто перейдите по его ссылке, заявка не нужна.",
            f"Хотите в модели — раздел «{BTN_MODEL}».",
        ]
    L += ["", "<b>Прочее</b>", "/menu — вернуть кнопки меню", "/cancel — отменить текущее действие"]
    if WEBAPP_URL:
        L.append(f"{BTN_APP} (/app) — то же самое в удобном приложении")
    if user_id in ADMIN_IDS:
        L += ["", f"🛠 {BTN_ADMIN} (/opcaadmin) — очереди, команды, рассылки"]
    return "\n".join(L)


main_router = Router(name="main")
main_router.message.filter(F.chat.type == "private")
fallback_router = Router(name="fallback")
fallback_router.message.filter(F.chat.type == "private")


# -------------------- ПОДПИСКА --------------------

async def is_subscribed(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
    except (TelegramBadRequest, TelegramForbiddenError):
        # бот не админ канала или неверный CHANNEL_USERNAME — не запираем людей из-за настройки
        log.warning("Не удалось проверить подписку на %s — пропускаю проверку", CHANNEL_USERNAME)
        return True
    except TelegramAPIError:
        log.exception("get_chat_member failed")
        return True
    return m.status in ("creator", "administrator", "member") or (
        m.status == "restricted" and getattr(m, "is_member", False))


async def gate(message: Message, payload: str = "") -> bool:
    """True — можно продолжать. Иначе показывает просьбу подписаться."""
    if await is_subscribed(message.from_user.id):
        return True
    await message.answer("Чтобы пользоваться ботом, подпишитесь на наш канал 👇",
                         reply_markup=subscribe_kb(payload))
    return False


async def welcome(message: Message, user):
    """Приветствие под роль: гость — с чего начать, участник — что сегодня, админ — что ждёт решения."""
    uid = user.id
    name = esc(user.first_name or "друг")
    rank = user_rank(uid)
    if rank:
        row = get_user(uid)
        text = (f"👋 С возвращением, <b>{name}</b>!\n"
                f"🏅 {esc(rank_label(row['rank'], row['level']))}\n\n{today_line(uid)}")
    else:
        text = (f"👋 Привет, <b>{name}</b>!\n\nМы набираем операторов чатов и скаутов моделей. "
                f"Выберите, кем хотите стать — ответить нужно на несколько вопросов.\n"
                f"Пригласил наставник? Просто перейдите по его ссылке.")
    if uid in ADMIN_IDS:
        n_rep, n_app, n_req = count_pending_reports(), count_pending_apps(), count_pending_team_requests()
        pend = [f"{t} {n}" for t, n in (("🧾 отчётов", n_rep), ("📥 заявок", n_app), ("🙋 запросов команд", n_req)) if n]
        text += "\n\n🛠 Ждёт решения: " + (" · ".join(pend) if pend else "ничего, всё разобрано ✅")
    await message.answer(text, reply_markup=main_kb(uid))


# -------------------- /start, /help, подписка --------------------

@main_router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject, state: FSMContext):
    await state.clear()
    user = message.from_user
    upsert_user(user)
    track(user.id, "start")
    payload = (command.args or "").strip()
    if not await gate(message, payload):
        return
    # человека могли заранее добавить в команду по @username («Добавить работника»)
    await claim_username_invites(bot, user, message)
    if await handle_start_payload(message, payload or None, state):
        return
    await welcome(message, user)


@main_router.callback_query(F.data.startswith("check_sub"))
async def cb_check_sub(cb: CallbackQuery, state: FSMContext):
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Подписка не найдена. Подпишитесь и нажмите ещё раз.", show_alert=True)
        return
    payload = cb.data.partition(":")[2]
    upsert_user(cb.from_user)
    try:
        await cb.message.delete()
    except TelegramBadRequest:
        pass
    await cb.answer("Подписка подтверждена ✅")
    await claim_username_invites(bot, cb.from_user, cb.message)
    # у cb.message.from_user — сам бот, поэтому пользователя передаём явно
    if await handle_start_payload(cb.message, payload or None, state, user=cb.from_user):
        return
    await welcome(cb.message, cb.from_user)


@main_router.message(Command("help"))
@main_router.message(F.text == BTN_HELP)
async def cmd_help(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(help_text(message.from_user.id), reply_markup=main_kb(message.from_user.id))


@main_router.message(Command("app"))
@main_router.message(F.text == BTN_APP)
async def cmd_app(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    if not WEBAPP_URL:
        await message.answer("Мини-приложение пока не подключено.")
        return
    await message.answer("📱 Кабинет: профиль, отчёты со статусами и рейтинг — всё в одном окне.",
                         reply_markup=webapp_kb())


@main_router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню 👇", reply_markup=main_kb(message.from_user.id))


def apps_overview(uid: int):
    """Текст и кнопки раздела «Мои заявки»: что подано, в каком статусе и что можно подать ещё."""
    lines = ["📋 <b>Мои заявки</b>", ""]
    rows, found = [], False
    for kind, info in KIND_INFO.items():
        a = application_for(uid, kind)
        if a:
            found = True
            lines.append(f"{info['title']}: {STATUS_TITLES.get(a['status'], a['status'])}")
    u = get_user(uid)
    if u and u["model_interest"]:
        found = True
        lines.append("Модель: ✅ интерес передан менеджеру")
    req = latest_team_request(uid)
    if req:
        found = True
        lines.append(f"Команда «{esc(req['name'])}»: {STATUS_TITLES.get(req['status'], req['status'])}")
    if not found:
        lines.append("Пока ничего не подано. Выберите, с чего начать 👇")
    elif any(application_for(uid, k) and application_for(uid, k)["status"] == "pending" for k in KIND_INFO):
        lines += ["", "Заявки рассматриваются вручную — как только решат, я напишу сюда."]
    for kind, info in KIND_INFO.items():
        if not application_for(uid, kind):
            rows.append([InlineKeyboardButton(text=f"📝 Подать заявку: {info['title']}", callback_data=f"apply:{kind}")])
    if not (u and u["model_interest"]):
        rows.append([InlineKeyboardButton(text="📸 Хочу в модели (18+)", callback_data="model:interest")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


@main_router.message(Command("status"))
@main_router.message(F.text == BTN_APPS)
async def cmd_status(message: Message, state: FSMContext):
    await state.clear()
    text, markup = apps_overview(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@main_router.callback_query(F.data == "my:apps")
async def cb_my_apps(cb: CallbackQuery):
    text, markup = apps_overview(cb.from_user.id)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await cb.message.answer(text, reply_markup=markup)
    await cb.answer()


# -------------------- О проекте --------------------

@main_router.message(F.text == BTN_ABOUT)
async def menu_about(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    rows = [[InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)]]
    if not user_rank(message.from_user.id):
        rows.append([InlineKeyboardButton(text="🎧 Стать оператором", callback_data="apply:operator"),
                     InlineKeyboardButton(text="🔎 Стать скаутом", callback_data="apply:scout")])
    await message.answer(ABOUT_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))


# -------------------- Заявки: оператор / скаут --------------------

async def show_kind(message: Message, kind: str):
    info = KIND_INFO[kind]
    existing = application_for(message.from_user.id, kind)
    if existing:
        await message.answer(
            f"{info['desc']}\n\nВаша заявка: {STATUS_TITLES.get(existing['status'], existing['status'])}.")
        return
    await message.answer(
        info["desc"] + "\n\nНужно заполнить анкету (8 вопросов). Нажмите кнопку, чтобы начать.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📝 Подать заявку", callback_data=f"apply:{kind}")]]))


@main_router.message(F.text == BTN_OPERATOR)
async def menu_operator(message: Message, state: FSMContext):
    await state.clear()
    if await gate(message):
        await show_kind(message, "operator")


@main_router.message(F.text == BTN_SCOUT)
async def menu_scout(message: Message, state: FSMContext):
    await state.clear()
    if await gate(message):
        await show_kind(message, "scout")


def question_text(kind: str, idx: int, err: str = "") -> str:
    key = APP_STEPS[idx]
    return (f"📝 <b>Анкета · {KIND_INFO[kind]['title']}</b>\n\n" + (f"⚠️ {err}\n\n" if err else "")
            + f"<b>Вопрос {idx + 1}/{len(APP_STEPS)}.</b> {APP_PROMPTS[key]}")


def question_kb(key: str, user, extra=None):
    rows = []
    if key == "tg":
        if user.username:
            rows.append([InlineKeyboardButton(text=f"✅ Это мой: @{user.username}", callback_data="apply:me")])
        else:
            rows.append([InlineKeyboardButton(text="У меня нет username", callback_data="apply:me")])
    if key == "en":
        rows += [[InlineKeyboardButton(text=lv, callback_data=f"apply:en:{i}")] for i, lv in enumerate(ENGLISH_LEVELS)]
    if key == "exp":
        rows.append([InlineKeyboardButton(text="Нет опыта", callback_data="apply:noexp")])
    if key == "hp":
        rows.append([InlineKeyboardButton(text="🎧 Есть", callback_data="apply:hp:1"),
                     InlineKeyboardButton(text="🚫 Нет", callback_data="apply:hp:0")])
    if extra:
        rows.append(extra)
    rows.append([InlineKeyboardButton(text="❌ Отменить заявку", callback_data="apply:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


ALL_MENU_TEXTS = {BTN_OPERATOR, BTN_SCOUT, BTN_MODEL, BTN_ABOUT, BTN_APPS, BTN_APP, BTN_HELP,
                  BTN_PROFILE, BTN_REPORT, BTN_TEAM, BTN_TOP, BTN_ADMIN}


@main_router.callback_query(F.data == "apply:x")
async def cb_apply_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await cb.message.edit_text("Заявка отменена. Вернуться к ней можно в любой момент — «📋 Мои заявки».")
    except TelegramBadRequest:
        pass
    await cb.message.answer("Главное меню 👇", reply_markup=main_kb(cb.from_user.id))
    await cb.answer()


@main_router.callback_query(F.data.regexp(r"^apply:(operator|scout)$"))
async def cb_apply(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":")[1]
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Сначала подпишитесь на канал (/start).", show_alert=True)
        return
    if application_for(cb.from_user.id, kind):
        await cb.answer("Заявка уже подана.", show_alert=True)
        return
    upsert_user(cb.from_user)
    await state.clear()
    await state.set_state(Apply.answers)
    await state.update_data(kind=kind, idx=0, answers=[])
    await cb.message.answer(question_text(kind, 0), reply_markup=question_kb("fio", cb.from_user))
    await cb.answer()


async def app_submit(message: Message, user, state: FSMContext, kind: str, answers: list):
    await state.clear()
    if not add_application(user.id, kind, answers):
        await message.answer("Заявка такого типа у вас уже есть.", reply_markup=main_kb(user.id))
        return
    track(user.id, f"application_{kind}")
    await message.answer(
        f"✅ <b>Заявка «{KIND_INFO[kind]['title']}» отправлена!</b>\n\n"
        f"Что дальше: админ рассмотрит её вручную, а я сразу напишу вам сюда. "
        f"Следить за статусом можно в «{BTN_APPS}».", reply_markup=main_kb(user.id))
    await notify_admins_new_app(user.id, kind)


async def app_next(message: Message, user, state: FSMContext, answer_line: str):
    """Сохраняет ответ и задаёт следующий вопрос (или отправляет заявку)."""
    data = await state.get_data()
    kind, idx, answers = data["kind"], data["idx"], data.get("answers", [])
    answers.append(answer_line)
    idx += 1
    if idx >= len(APP_STEPS):
        await app_submit(message, user, state, kind, answers)
        return
    await state.update_data(idx=idx, answers=answers, young=False)
    await message.answer(question_text(kind, idx), reply_markup=question_kb(APP_STEPS[idx], user))


async def app_ask_again(message: Message, user, state: FSMContext, err: str, extra=None):
    data = await state.get_data()
    await message.answer(question_text(data["kind"], data["idx"], err),
                         reply_markup=question_kb(APP_STEPS[data["idx"]], user, extra))


@main_router.message(Apply.answers, F.text, ~F.text.startswith("/"), ~F.text.in_(ALL_MENU_TEXTS))
async def apply_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    kind, idx = data.get("kind"), data.get("idx", 0)
    user = message.from_user
    if kind not in KIND_INFO or idx >= len(APP_STEPS):
        await state.clear()
        await message.answer("Сессия устарела — начните заново.", reply_markup=main_kb(user.id))
        return
    key, text = APP_STEPS[idx], message.text.strip()

    if key == "fio":
        v = parse_fio(text)
        if not v:
            await app_ask_again(message, user, state, "Нужны фамилия и имя без цифр, например: Иванов Иван.")
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: {v}")
    elif key == "birth":
        b = parse_birth(text)
        if not b:
            await app_ask_again(message, user, state, "Не понял дату. Формат: ДД.ММ.ГГГГ, например 07.03.2004.")
            return
        age = age_from(b)
        prob = age_problem(kind, age)
        if prob:
            if kind != "scout" and age >= 10:
                # оператором нельзя, а скаутом — можно: предлагаем переключиться, а не выгоняем
                await state.update_data(young=True, birth_line=f"{QUESTION_LABELS[key]}: {b.strftime('%d.%m.%Y')} "
                                                               f"({age} {years_word(age)})")
                await app_ask_again(message, user, state, prob,
                                    [InlineKeyboardButton(text="🔎 Подать заявку скаутом", callback_data="apply:sw")])
            else:
                await app_ask_again(message, user, state, prob)
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: {b.strftime('%d.%m.%Y')} ({age} {years_word(age)})")
    elif key == "phone":
        v = parse_phone(text)
        if not v:
            await app_ask_again(message, user, state, "Не похоже на номер телефона. Пример: +7 900 123-45-67.")
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: {v}")
    elif key == "tg":
        v = parse_username(text)
        if not v:
            await app_ask_again(message, user, state, "Не похоже на username. Пример: @ivan_petrov.")
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: @{v}")
    elif key == "exp":
        v = parse_exp(text)
        if not v:
            await app_ask_again(message, user, state, "Опишите опыт текстом (до 800 символов) или нажмите «Нет опыта».")
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: {v}")
    elif key == "country":
        v = parse_country(text)
        if not v:
            await app_ask_again(message, user, state, "Напишите название страны, например: Россия.")
            return
        await app_next(message, user, state, f"{QUESTION_LABELS[key]}: {v}")
    else:  # en, hp — только кнопками
        await app_ask_again(message, user, state, "Выберите ответ кнопкой.")


@main_router.callback_query(Apply.answers, F.data.regexp(r"^apply:en:\d$"))
async def cb_apply_en(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    i = int(cb.data.split(":")[2])
    if data.get("idx") != APP_STEPS.index("en") or i >= len(ENGLISH_LEVELS):
        await cb.answer()
        return
    await cb.answer()
    await app_next(cb.message, cb.from_user, state, f"{QUESTION_LABELS['en']}: {ENGLISH_LEVELS[i]}")


@main_router.callback_query(Apply.answers, F.data == "apply:noexp")
async def cb_apply_noexp(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("idx") != APP_STEPS.index("exp"):
        await cb.answer()
        return
    await cb.answer()
    await app_next(cb.message, cb.from_user, state, f"{QUESTION_LABELS['exp']}: нет опыта")


@main_router.callback_query(Apply.answers, F.data == "apply:me")
async def cb_apply_me(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("idx") != APP_STEPS.index("tg"):
        await cb.answer()
        return
    u = cb.from_user
    await cb.answer()
    await app_next(cb.message, u, state, f"{QUESTION_LABELS['tg']}: " + (f"@{u.username}" if u.username else "нет username"))


@main_router.callback_query(Apply.answers, F.data.regexp(r"^apply:hp:[01]$"))
async def cb_apply_hp(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("idx") != APP_STEPS.index("hp"):
        await cb.answer()
        return
    await cb.answer()
    await app_next(cb.message, cb.from_user, state,
                   f"{QUESTION_LABELS['hp']}: " + ("есть" if cb.data.endswith("1") else "нет"))


@main_router.callback_query(Apply.answers, F.data == "apply:sw")
async def cb_apply_switch(cb: CallbackQuery, state: FSMContext):
    """Оператору нет 18 — предлагаем подать заявку скаутом с уже введёнными данными."""
    data = await state.get_data()
    if not data.get("young") or not data.get("birth_line"):
        await cb.answer("Сначала введите дату рождения.", show_alert=True)
        return
    if application_for(cb.from_user.id, "scout"):
        await cb.answer("Заявка скаута у вас уже подана — смотрите «📋 Мои заявки».", show_alert=True)
        return
    answers = list(data.get("answers", [])) + [data["birth_line"]]
    await state.update_data(kind="scout", answers=answers, idx=APP_STEPS.index("birth") + 1, young=False)
    await cb.answer("Оформляем как скаута")
    await cb.message.answer(question_text("scout", APP_STEPS.index("birth") + 1),
                            reply_markup=question_kb("phone", cb.from_user))


@main_router.callback_query(F.data.regexp(r"^apply:(me|hp:[01]|sw|noexp|en:\d)$"))
async def cb_apply_stale(cb: CallbackQuery):
    await cb.answer("Эта анкета уже закрыта. Начните заново в меню.", show_alert=True)


async def notify_admins_new_app(user_id: int, kind: str):
    app_id = get_application_id(user_id, kind)
    if not app_id:
        return
    text = f"📥 Новая заявка #{app_id} · {KIND_INFO[kind]['title']}\n{who(get_user(user_id), user_id)}"
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=application_admin_kb(app_id))
        except TelegramAPIError:
            log.warning("Не удалось уведомить админа %s", admin)


# -------------------- Модель --------------------

MODEL_TEXT = (
    "📸 <b>Хотите стать моделью?</b>\n\n"
    "Мы поможем с запуском, продвижением и сопровождением. Работа моделью — <b>с 18 лет</b>. "
    "Нажмите кнопку — менеджер свяжется с вами, или напишите ему сами."
)


@main_router.message(F.text == BTN_MODEL)
async def menu_model(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    await message.answer(MODEL_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Мне есть 18, хочу в модели", callback_data="model:interest")],
        [InlineKeyboardButton(text="💬 Написать менеджеру", url=MODEL_CONTACT)],
    ]))


@main_router.callback_query(F.data == "model:interest")
async def cb_model_interest(cb: CallbackQuery):
    if not await is_subscribed(cb.from_user.id):
        await cb.answer("Сначала подпишитесь на канал (/start).", show_alert=True)
        return
    upsert_user(cb.from_user)
    first_time = set_model_interest(cb.from_user.id)
    if not first_time:
        await cb.answer("Ваша заявка уже передана менеджеру.", show_alert=True)
        return
    track(cb.from_user.id, "model_interest")
    await cb.answer("Готово! Менеджер скоро свяжется ✅", show_alert=True)
    text = f"📸 Интерес к модели: {who(get_user(cb.from_user.id), cb.from_user.id)}"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙍 Профиль", callback_data=f"adm:u:{cb.from_user.id}")]])
    for admin in ADMIN_IDS:
        try:
            await bot.send_message(admin, text, reply_markup=markup)
        except TelegramAPIError:
            log.warning("Не удалось уведомить админа %s", admin)


# -------------------- Fallback (подключается ПОСЛЕ teams) --------------------

@fallback_router.message(StateFilter(None), F.text)
async def fallback(message: Message):
    await message.answer("Не понял вас 🤔 Воспользуйтесь кнопками меню внизу или откройте «❓ Помощь».",
                         reply_markup=main_kb(message.from_user.id))


# -------------------- ЗАПУСК --------------------

async def set_commands():
    public = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Профиль и статистика"),
        BotCommand(command="report", description="Отчёт за сегодня"),
        BotCommand(command="top", description="Рейтинг за 7 дней"),
        BotCommand(command="addworker", description="Привести работника (анкета)"),
        BotCommand(command="addmember", description="Добавить сотрудника в команду"),
        BotCommand(command="requestteam", description="Запросить команду (мл. тимлид)"),
        BotCommand(command="app", description="Мини-приложение"),
        BotCommand(command="team", description="Моя команда"),
        BotCommand(command="status", description="Статус моих заявок"),
        BotCommand(command="help", description="Справка"),
    ]
    try:
        await bot.set_my_commands(public)
        for admin in ADMIN_IDS:
            # админам — дополнительная команда. Остальным она не показывается и не работает.
            await bot.set_my_commands(
                public + [BotCommand(command="opcaadmin", description="Админ-панель")],
                scope=BotCommandScopeChat(chat_id=admin))
    except TelegramAPIError:
        log.warning("Не удалось выставить список команд (админ мог ни разу не писать боту)")


async def main():
    init_db()
    init_team_db()
    # Аддитивные таблицы Mini App / финансов создаются поверх существующей SQLite.
    from webapp import init_agency_extensions
    init_agency_extensions()
    dp.include_router(main_router)
    await setup_teams(dp, bot, main_kb)
    dp.include_router(fallback_router)
    await set_commands()
    runner = None
    if WEBAPP_URL:
        from webapp import start_webapp
        runner = await start_webapp(bot, WEBAPP_HOST, WEBAPP_PORT)
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Кабинет", web_app=WebAppInfo(url=WEBAPP_URL)))
        except TelegramAPIError:
            log.warning("Не удалось поставить кнопку меню для Mini App")
    else:
        log.info("WEBAPP_URL не задан — мини-приложение выключено")
    log.info("Бот запущен")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if runner:
            await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
