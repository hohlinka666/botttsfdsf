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
    application_admin_kb, claim_username_invites, handle_start_payload, init_team_db, setup_teams, who,
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
BTN_PROFILE = "👤 Профиль"  # обрабатывается в teams.py
BTN_APP = "📱 Кабинет"


def main_kb():
    last = [KeyboardButton(text=BTN_PROFILE)]
    if WEBAPP_URL:
        last.append(KeyboardButton(text=BTN_APP))
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_OPERATOR), KeyboardButton(text=BTN_SCOUT)],
            [KeyboardButton(text=BTN_MODEL), KeyboardButton(text=BTN_ABOUT)],
            last,
        ], resize_keyboard=True
    )


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

KIND_INFO = {
    "operator": {
        "title": "Оператор чата",
        "desc": ("🎧 <b>Оператор чата</b>\n\n"
                 "Ведёте переписку с подписчиками от лица моделей агентства, следуете скриптам и "
                 "стандартам качества. Обучение и поддержка наставника — с первого дня."),
        "questions": [
            ("age", "Сколько вам полных лет? (число)"),
            ("name", "Как вас зовут?"),
            ("exp", "Есть ли опыт в чатах/продажах/поддержке? Расскажите коротко."),
            ("hours", "Сколько часов в день готовы работать?"),
            ("tz", "Ваш часовой пояс и удобное время смен?"),
        ],
    },
    "scout": {
        "title": "Скаут моделей",
        "desc": ("🔎 <b>Скаут моделей</b>\n\n"
                 "Находите и приводите в агентство новых моделей. Получаете скрипты, поддержку команды "
                 "и рост по рангам: от скаута до партнёра."),
        "questions": [
            ("age", "Сколько вам полных лет? (число)"),
            ("name", "Как вас зовут?"),
            ("exp", "Есть ли опыт в скаутинге или рекрутинге? Расскажите коротко."),
            ("where", "Где планируете искать моделей? (Instagram, TikTok, офлайн…)"),
            ("hours", "Сколько часов в день готовы уделять?"),
            ("plan", "Сколько моделей реально приводить в месяц?"),
        ],
    },
}
QUESTION_LABELS = {
    "age": "Возраст", "name": "Имя", "exp": "Опыт", "hours": "Часов в день",
    "tz": "Часовой пояс / смены", "where": "Где искать", "plan": "План в месяц",
}
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

HELP_TEXT = (
    "📖 <b>Команды</b>\n\n"
    "/start — главное меню\n"
    "/profile — профиль, ранг, статистика\n"
    "/report — составить отчёт за сегодня\n"
    "/top — рейтинг за 7 дней\n"
    "/addworker — добавить работника в команду (скауты и тимлиды)\n"
    "/requestteam — запросить создание новой команды\n"
    "/app — мини-приложение (кабинет)\n"
    "/status — статус моих заявок\n"
    "/cancel — отменить текущее действие\n"
    "/help — эта справка"
)

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
    name = esc(user.first_name or "друг")
    await message.answer(
        f"👋 Привет, <b>{name}</b>!\n\n"
        f"Выберите, кем вы хотите стать в нашей команде, или откройте профиль.",
        reply_markup=main_kb())


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
async def cmd_help(message: Message):
    text = HELP_TEXT
    if message.from_user.id in ADMIN_IDS:
        text += "\n\n🛠 /opcaadmin — админ-панель"
    await message.answer(text)


@main_router.message(Command("app"))
@main_router.message(F.text == BTN_APP)
async def cmd_app(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    if not WEBAPP_URL:
        await message.answer("Мини-приложение пока не подключено.")
        return
    await message.answer("Кабинет: профиль, отчёты со статусами и рейтинг.", reply_markup=webapp_kb())


@main_router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Главное меню 👇", reply_markup=main_kb())


@main_router.message(Command("status"))
async def cmd_status(message: Message):
    uid = message.from_user.id
    lines = ["📋 <b>Мои заявки</b>", ""]
    found = False
    for kind, info in KIND_INFO.items():
        a = application_for(uid, kind)
        if a:
            found = True
            lines.append(f"{info['title']}: {STATUS_TITLES.get(a['status'], a['status'])}")
    u = get_user(uid)
    if u and u["model_interest"]:
        found = True
        lines.append("Модель: ✅ интерес передан менеджеру")
    if not found:
        lines.append("Пока заявок нет. Выберите раздел в меню 👇")
    await message.answer("\n".join(lines), reply_markup=main_kb())


# -------------------- О проекте --------------------

@main_router.message(F.text == BTN_ABOUT)
async def menu_about(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    await message.answer(ABOUT_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Наш канал", url=CHANNEL_URL)]]))


# -------------------- Заявки: оператор / скаут --------------------

async def show_kind(message: Message, kind: str):
    info = KIND_INFO[kind]
    existing = application_for(message.from_user.id, kind)
    if existing:
        await message.answer(
            f"{info['desc']}\n\nВаша заявка: {STATUS_TITLES.get(existing['status'], existing['status'])}.")
        return
    await message.answer(
        info["desc"] + "\n\nЗаявка — несколько коротких вопросов. Нажмите кнопку, чтобы начать.",
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


def question_text(kind: str, idx: int) -> str:
    qs = KIND_INFO[kind]["questions"]
    return f"<b>Вопрос {idx + 1}/{len(qs)}</b>\n{qs[idx][1]}\n\n/cancel — отмена"


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
    await cb.message.answer(question_text(kind, 0))
    await cb.answer()


@main_router.message(Apply.answers, F.text, ~F.text.startswith("/"))
async def apply_answer(message: Message, state: FSMContext):
    data = await state.get_data()
    kind, idx, answers = data.get("kind"), data.get("idx", 0), data.get("answers", [])
    if kind not in KIND_INFO:
        await state.clear()
        await message.answer("Сессия устарела — начните заново.", reply_markup=main_kb())
        return
    qs = KIND_INFO[kind]["questions"]
    key = qs[idx][0]
    text = message.text.strip()

    if key == "age":
        if not text.isdigit() or not 10 <= int(text) <= 99:
            await message.answer("Введите возраст числом, например 21.")
            return
        if int(text) < 18:
            await state.clear()
            await message.answer("К сожалению, мы работаем только с совершеннолетними (18+).",
                                 reply_markup=main_kb())
            return
    elif not text or len(text) > 800:
        await message.answer("Ответ должен быть от 1 до 800 символов. Попробуйте ещё раз.")
        return

    answers.append(f"{QUESTION_LABELS[key]}: {text}")
    idx += 1
    if idx < len(qs):
        await state.update_data(idx=idx, answers=answers)
        await message.answer(question_text(kind, idx))
        return

    await state.clear()
    user = message.from_user
    if not add_application(user.id, kind, answers):
        await message.answer("Заявка такого типа у вас уже есть.", reply_markup=main_kb())
        return
    track(user.id, f"application_{kind}")
    await message.answer(
        f"✅ Заявка «{KIND_INFO[kind]['title']}» отправлена! Мы рассмотрим её и напишем вам сюда.\n"
        f"Статус — /status.", reply_markup=main_kb())
    await notify_admins_new_app(user.id, kind)


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
    "Мы поможем с запуском, продвижением и сопровождением. Нажмите «Хочу в модели» — менеджер "
    "свяжется с вами, или напишите ему сами."
)


@main_router.message(F.text == BTN_MODEL)
async def menu_model(message: Message, state: FSMContext):
    await state.clear()
    if not await gate(message):
        return
    await message.answer(MODEL_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🙋 Хочу в модели", callback_data="model:interest")],
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
    await message.answer("Не понял вас 🤔 Выберите раздел в меню или откройте /help.", reply_markup=main_kb())


# -------------------- ЗАПУСК --------------------

async def set_commands():
    public = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="profile", description="Профиль и статистика"),
        BotCommand(command="report", description="Отчёт за сегодня"),
        BotCommand(command="top", description="Рейтинг за 7 дней"),
        BotCommand(command="addworker", description="Добавить работника"),
        BotCommand(command="requestteam", description="Запросить команду"),
        BotCommand(command="app", description="Мини-приложение"),
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
