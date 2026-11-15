"""
teams.py — команды, реферальные ссылки, ранги, отчёты, напоминания в чат
и админ-панель /opcaadmin (старый алиас /opcaadm тоже работает).
Подключается к основному боту (см. инструкцию в конце файла).
"""
import asyncio
import csv
import html
import io
import logging
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
    TelegramMigrateToChat, TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile, CallbackQuery, ChatMemberUpdated, InlineKeyboardButton,
    InlineKeyboardMarkup, Message,
)
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("teams")

DB_PATH = os.getenv("DB_PATH", "vuitton.db")
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
MODEL_GROUP_ID = int(os.getenv("MODEL_GROUP_ID", "0") or 0)
SCOUT_GROUP_ID = int(os.getenv("SCOUT_GROUP_ID", "0") or 0)
# Группа для оповещений «У @user новая модель»: ALERT_GROUP_ID, иначе группа скаутов, иначе группа моделей
ALERT_GROUP_ID = int(os.getenv("ALERT_GROUP_ID", "0") or 0) or SCOUT_GROUP_ID or MODEL_GROUP_ID

MSK = timezone(timedelta(hours=3))
# Часы по МСК, в которые бот пишет в чаты. 18:00–00:00 включительно, раз в час.
REMINDER_HOURS = {18, 19, 20, 21, 22, 23, 0}
REMINDER_TEXT = "Не забудьте заполнить отчет о выполненной работе"

_MAIN_KB = None          # функция, возвращающая главную клавиатуру основного бота
_BOT_USERNAME = ""
_reminder_task: Optional[asyncio.Task] = None

# -------------------- РАНГИ --------------------
RANK_TITLES = {
    "model": "Модель", "operator": "Оператор", "scout": "Скаут",
    "jr_tl": "Младший тимлид", "sr_tl": "Старший тимлид",
    "squad": "Сквадлид", "partner": "Партнёр",
}
KIND_TITLES = {"scout": "Скаут", "operator": "Оператор", "model": "Модель"}
# Лестница повышения: (ранг, уровень). Модели и операторы вне лестницы.
LADDER = [
    ("scout", 1), ("scout", 2),
    ("jr_tl", 1), ("jr_tl", 2), ("jr_tl", 3),
    ("sr_tl", 1), ("squad", 1), ("partner", 1),
]
LEADER_RANKS = {"jr_tl", "sr_tl", "squad", "partner"}      # видят отчёты, управляют людьми
ALL_SCOPE_RANKS = {"sr_tl", "squad", "partner"}            # видят всех, а не только свою команду


def ladder_index(rank, level) -> Optional[int]:
    try:
        return LADDER.index((rank, int(level or 1)))
    except ValueError:
        return None


def rank_label(rank, level) -> str:
    if not rank:
        return "без ранга"
    title = RANK_TITLES.get(rank, rank)
    if rank in ("scout", "jr_tl"):
        return f"{title} · {level or 1} ур."
    return title


# -------------------- БАЗА --------------------
@contextmanager
def db():
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


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def msk_now() -> datetime:
    return datetime.now(MSK)


def since7() -> str:
    return (msk_now().date() - timedelta(days=6)).isoformat()


def fmt_dt(iso) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except Exception:
        return iso or "—"


def fmt_date(iso) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d.%m.%Y")
    except Exception:
        return iso or "—"


def init_team_db():
    """Вызывать после init_db() основного бота."""
    with db() as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            role TEXT DEFAULT 'user', model_interest INTEGER DEFAULT 0,
            is_blocked INTEGER DEFAULT 0,
            created_at TEXT NOT NULL, last_seen TEXT NOT NULL)""")
        cols = {r["name"] for r in con.execute("PRAGMA table_info(users)")}
        for col, ddl in (("is_blocked", "INTEGER DEFAULT 0"), ("rank", "TEXT"), ("level", "INTEGER DEFAULT 1")):
            if col not in cols:
                con.execute(f"ALTER TABLE users ADD COLUMN {col} {ddl}")
        con.execute("""
        CREATE TABLE IF NOT EXISTS teams (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE COLLATE NOCASE,
            leader_id INTEGER NOT NULL,
            ref_code TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS team_members (
            user_id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL,
            joined_at TEXT NOT NULL,
            via_ref INTEGER DEFAULT 0,
            added_by INTEGER)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            report_date TEXT NOT NULL,
            hours REAL NOT NULL, scouts INTEGER NOT NULL,
            models INTEGER NOT NULL, operators INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'approved',
            reviewed_by INTEGER, reviewed_at TEXT, review_note TEXT,
            UNIQUE(user_id, report_date))""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS model_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scout_id INTEGER NOT NULL, comment TEXT, created_by INTEGER,
            created_at TEXT NOT NULL)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS bans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, username TEXT, reason TEXT,
            created_by INTEGER, created_at TEXT NOT NULL)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS notes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_id INTEGER NOT NULL, to_id INTEGER NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS chats (
            chat_id INTEGER PRIMARY KEY, title TEXT, type TEXT,
            enabled INTEGER DEFAULT 0, added_by INTEGER, added_at TEXT NOT NULL)""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, kind TEXT NOT NULL, answers TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, reviewed_at TEXT,
            UNIQUE(user_id, kind))""")
        con.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER, event TEXT NOT NULL, created_at TEXT NOT NULL)""")
        # заявки скаутов/тимлидов на создание новой команды (решает администратор)
        con.execute("""
        CREATE TABLE IF NOT EXISTS team_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL, name TEXT NOT NULL, comment TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL, reviewed_by INTEGER, reviewed_at TEXT,
            review_note TEXT, team_id INTEGER)""")
        # приглашения работников, которые ещё не запускали бота (по личной ссылке или по @username)
        con.execute("""
        CREATE TABLE IF NOT EXISTS invites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            token TEXT NOT NULL UNIQUE, username TEXT DEFAULT '',
            kind TEXT NOT NULL, team_id INTEGER NOT NULL, added_by INTEGER NOT NULL,
            created_at TEXT NOT NULL, claimed_by INTEGER, claimed_at TEXT)""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_treq_status ON team_requests(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_treq_user ON team_requests(user_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_inv_user ON invites(username)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_inv_team ON invites(team_id)")
        rcols = {r["name"] for r in con.execute("PRAGMA table_info(reports)")}
        # старые отчёты считаются подтверждёнными (DEFAULT 'approved'); новые создаются как 'pending'
        for col, ddl in (("status", "TEXT NOT NULL DEFAULT 'approved'"), ("reviewed_by", "INTEGER"),
                         ("reviewed_at", "TEXT"), ("review_note", "TEXT")):
            if col not in rcols:
                con.execute(f"ALTER TABLE reports ADD COLUMN {col} {ddl}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_rep_status ON reports(status)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_tm_team ON team_members(team_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_rep_user ON reports(user_id, report_date)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_teams_leader ON teams(leader_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_bans_user ON bans(user_id)")


# ---- пользователи ----
def touch_user(u):
    ts = utcnow()
    with db() as con:
        con.execute("""
            INSERT INTO users(user_id,username,first_name,created_at,last_seen) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                first_name=excluded.first_name, last_seen=excluded.last_seen, is_blocked=0
        """, (u.id, u.username or "", u.first_name or "", ts, ts))


def get_user(uid):
    with db() as con:
        return con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def find_user(query: str):
    """Ищет по @username или числовому ID. Только тех, кто запускал бота."""
    q = (query or "").strip()
    if not q:
        return None
    if q.lstrip("-").isdigit():
        return get_user(int(q))
    q = q.lstrip("@").lower()
    with db() as con:
        return con.execute("SELECT * FROM users WHERE lower(username)=?", (q,)).fetchone()


def set_rank(uid, rank, level=1):
    with db() as con:
        con.execute("UPDATE users SET rank=?, level=? WHERE user_id=?", (rank, level, uid))


def esc(v) -> str:
    return html.escape(str(v if v is not None else ""))


def who(row, uid=None) -> str:
    if row is None:
        return f"ID <code>{uid}</code>"
    name = f"@{row['username']}" if row["username"] else (row["first_name"] or "без имени")
    return f"{esc(name)} (<code>{row['user_id']}</code>)"


def is_admin(uid) -> bool:
    return uid in ADMIN_IDS


# ---- команды ----
def new_ref_code() -> str:
    with db() as con:
        while True:
            code = secrets.token_hex(4)
            if not con.execute("SELECT 1 FROM teams WHERE ref_code=?", (code,)).fetchone():
                return code


def bot_link(code: str) -> str:
    return f"https://t.me/{_BOT_USERNAME}?start=ref_{code}"


def create_team(name: str, leader_id: int) -> Optional[int]:
    if team_name_taken(name):
        return None
    try:
        with db() as con:
            cur = con.execute(
                "INSERT INTO teams(name,leader_id,ref_code,created_at) VALUES(?,?,?,?)",
                (name, leader_id, new_ref_code(), utcnow()))
            return cur.lastrowid
    except sqlite3.IntegrityError:
        return None


def team_by_id(tid):
    with db() as con:
        return con.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone()


def team_by_ref(code):
    with db() as con:
        return con.execute("SELECT * FROM teams WHERE ref_code=?", (code,)).fetchone()


def teams_led_by(uid):
    with db() as con:
        return con.execute("""
            SELECT t.*, (SELECT COUNT(*) FROM team_members m WHERE m.team_id=t.id) AS members
            FROM teams t WHERE t.leader_id=? ORDER BY t.name""", (uid,)).fetchall()


def member_team(uid):
    with db() as con:
        return con.execute("""
            SELECT t.*, tm.joined_at, tm.via_ref FROM team_members tm
            JOIN teams t ON t.id=tm.team_id WHERE tm.user_id=?""", (uid,)).fetchone()


def team_members(tid):
    with db() as con:
        return con.execute("""
            SELECT u.*, tm.joined_at, tm.via_ref FROM team_members tm
            JOIN users u ON u.user_id=tm.user_id WHERE tm.team_id=?
            ORDER BY tm.joined_at DESC""", (tid,)).fetchall()


def add_member(uid, tid, via_ref=0, added_by=None):
    with db() as con:
        con.execute("""
            INSERT INTO team_members(user_id,team_id,joined_at,via_ref,added_by) VALUES(?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET team_id=excluded.team_id,
                joined_at=excluded.joined_at, via_ref=excluded.via_ref, added_by=excluded.added_by
        """, (uid, tid, utcnow(), via_ref, added_by))


def remove_member(uid, tid=None) -> bool:
    with db() as con:
        if tid is None:
            cur = con.execute("DELETE FROM team_members WHERE user_id=?", (uid,))
        else:
            cur = con.execute("DELETE FROM team_members WHERE user_id=? AND team_id=?", (uid, tid))
        return cur.rowcount > 0


def ensure_leader_rank(uid) -> bool:
    u = get_user(uid)
    if u and u["rank"] not in LEADER_RANKS:
        set_rank(uid, "jr_tl", 1)
        return True
    return False


# ---- права ----
def can_manage(viewer_id: int, target_id: int) -> bool:
    if is_admin(viewer_id):
        return True
    v = get_user(viewer_id)
    if not v or v["rank"] not in LEADER_RANKS:
        return False
    if v["rank"] in ALL_SCOPE_RANKS:
        return True
    with db() as con:
        return bool(con.execute("""
            SELECT 1 FROM team_members tm JOIN teams t ON t.id=tm.team_id
            WHERE tm.user_id=? AND t.leader_id=?""", (target_id, viewer_id)).fetchone())


def is_lead_viewer(viewer_id: int) -> bool:
    if is_admin(viewer_id):
        return True
    v = get_user(viewer_id)
    return bool(v and v["rank"] in LEADER_RANKS)


def change_rank(viewer_id, uid, direction):
    t = get_user(uid)
    if not t:
        return False, "Пользователь не найден"
    idx = ladder_index(t["rank"], t["level"])
    if idx is None:
        return False, "Ранг вне лестницы (модель, оператор или без ранга) — роль меняет администратор."
    new = idx + direction
    if new < 0 or new >= len(LADDER):
        return False, "Дальше двигать нельзя."
    if not is_admin(viewer_id):
        v = get_user(viewer_id)
        vidx = ladder_index(v["rank"], v["level"]) if v else None
        if vidx is None or v["rank"] not in LEADER_RANKS or not can_manage(viewer_id, uid):
            return False, "Нет прав."
        if idx >= vidx:
            return False, "Менять ранг можно только тем, кто ниже вас."
        if new >= vidx:
            return False, "Нельзя повысить до своего ранга или выше."
    set_rank(uid, *LADDER[new])
    return True, f"Новый ранг: {rank_label(*LADDER[new])}"


# ---- статистика ----
def _totals(where: str, params: tuple):
    with db() as con:
        r = con.execute(f"""
            SELECT COUNT(*) n, COALESCE(SUM(hours),0) h, COALESCE(SUM(scouts),0) s,
                   COALESCE(SUM(models),0) m, COALESCE(SUM(operators),0) o
            FROM reports r WHERE r.status<>'rejected' AND {where}""", params).fetchone()
    return dict(r)


def user_totals(uid, since="0000-01-01"):
    return _totals("r.user_id=? AND r.report_date>=?", (uid, since))


def team_totals(tid, since="0000-01-01"):
    return _totals("r.user_id IN (SELECT user_id FROM team_members WHERE team_id=?) AND r.report_date>=?",
                   (tid, since))


def fmt_tot(t) -> str:
    return (f"отчётов {t['n']} · {t['h']:g} ч · скаутов {t['s']} · "
            f"моделей {t['m']} · операторов {t['o']}")


def teams_with_stats():
    s7 = since7()
    with db() as con:
        rows = con.execute("""
            SELECT t.*, (SELECT COUNT(*) FROM team_members m WHERE m.team_id=t.id) AS members
            FROM teams t ORDER BY t.name""").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        tot = team_totals(r["id"], s7)
        d["brought7"] = tot["s"] + tot["m"] + tot["o"]
        out.append(d)
    return out


# ---- отчёты ----
def save_report(uid, hours, scouts, models, operators) -> int:
    """Сохраняет отчёт за сегодня (перезаписывает прежний) со статусом «на проверке». Возвращает id."""
    today = msk_now().date().isoformat()
    with db() as con:
        con.execute("DELETE FROM reports WHERE user_id=? AND report_date=?", (uid, today))
        cur = con.execute("""INSERT INTO reports(user_id,report_date,hours,scouts,models,operators,created_at,status)
                             VALUES(?,?,?,?,?,?,?,'pending')""",
                          (uid, today, hours, scouts, models, operators, utcnow()))
        return cur.lastrowid


def report_page(viewer_id: int, uid_filter: int, before_id: int = 0):
    if is_admin(viewer_id):
        scope, sp = "1=1", ()
    else:
        v = get_user(viewer_id)
        if not v or v["rank"] not in LEADER_RANKS:
            return None
        if v["rank"] in ALL_SCOPE_RANKS:
            scope, sp = "1=1", ()
        else:
            scope = ("r.user_id IN (SELECT tm.user_id FROM team_members tm "
                     "JOIN teams t ON t.id=tm.team_id WHERE t.leader_id=?)")
            sp = (viewer_id,)
    with db() as con:
        return con.execute(f"""
            SELECT r.*, u.username, u.first_name, u.rank, u.level FROM reports r
            LEFT JOIN users u ON u.user_id=r.user_id
            WHERE {scope} AND (?=0 OR r.user_id=?) AND (?=0 OR r.id<?)
            ORDER BY r.id DESC LIMIT 1""",
            (*sp, uid_filter, uid_filter, before_id, before_id)).fetchone()


# ---- баны ----
def is_banned(uid: int, username: Optional[str]) -> bool:
    uname = (username or "").lower()
    with db() as con:
        row = con.execute("""
            SELECT id, user_id FROM bans
            WHERE user_id=? OR (username<>'' AND username IS NOT NULL AND username=?) LIMIT 1""",
            (uid, uname)).fetchone()
        if row and row["user_id"] is None:
            con.execute("UPDATE bans SET user_id=? WHERE id=?", (uid, row["id"]))
    return bool(row)


def add_ban(kind: str, value, reason: str, by: int):
    uid, uname = None, None
    if kind == "id":
        uid = int(value)
        row = get_user(uid)
        uname = (row["username"] or "").lower() if row else ""
    else:
        uname = str(value).lstrip("@").lower()
        row = find_user(uname)
        uid = row["user_id"] if row else None
    if uid in ADMIN_IDS:
        return None
    with db() as con:
        ex = con.execute("""SELECT id FROM bans WHERE (user_id IS NOT NULL AND user_id=?)
                            OR (username<>'' AND username=?)""", (uid, uname or "")).fetchone()
        if ex:
            return ex["id"]
        cur = con.execute("INSERT INTO bans(user_id,username,reason,created_by,created_at) VALUES(?,?,?,?,?)",
                          (uid, uname or "", reason, by, utcnow()))
        return cur.lastrowid


class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        if user and user.id not in ADMIN_IDS and is_banned(user.id, user.username):
            try:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ Доступ к боту ограничен.", show_alert=True)
                elif isinstance(event, Message) and event.chat.type == "private":
                    await event.answer("⛔ Доступ к боту ограничен.")
            except TelegramAPIError:
                pass
            return None
        return await handler(event, data)


# -------------------- УТИЛИТЫ ОТПРАВКИ --------------------
def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data):
    return InlineKeyboardButton(text=text, callback_data=data)


async def safe_send(bot: Bot, chat_id: int, text: str, **kw) -> bool:
    for _ in range(2):
        try:
            await bot.send_message(chat_id, text, **kw)
            return True
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except (TelegramForbiddenError, TelegramBadRequest):
            return False
        except TelegramAPIError:
            log.exception("send_message failed")
            return False
    return False


async def show(target, text: str, markup=None):
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


def main_menu_kb():
    return _MAIN_KB() if _MAIN_KB else None


# -------------------- ПРОФИЛЬ --------------------
def profile_text(uid: int, show_links: bool) -> str:
    u = get_user(uid)
    if not u:
        return "Пользователь не найден."
    mem = member_team(uid)
    led = teams_led_by(uid)
    L = ["👤 <b>Профиль</b>", who(u), f"Имя: {esc(u['first_name'] or '—')}",
         f"Ранг: <b>{esc(rank_label(u['rank'], u['level']))}</b>"]
    if mem:
        L.append(f"Команда: «{esc(mem['name'])}» · лидер {who(get_user(mem['leader_id']), mem['leader_id'])}")
    else:
        L.append("Команда: —")
    for t in led:
        L.append(f"👑 Лидер команды «{esc(t['name'])}» ({t['members']} чел.)")
        if show_links:
            L.append(f"🔗 Реф. ссылка: <code>{bot_link(t['ref_code'])}</code>")
    req = latest_team_request(uid)
    if req and req["status"] == "pending":
        L.append(f"🙋 Заявка на команду «{esc(req['name'])}»: ⏳ на рассмотрении")
    L.append(f"Регистрация: {fmt_dt(u['created_at'])} МСК")
    L += ["", "<b>Статистика</b>",
          f"За всё время: {fmt_tot(user_totals(uid))}",
          f"За 7 дней: {fmt_tot(user_totals(uid, since7()))}"]
    return "\n".join(L)


def profile_kb(viewer_id: int, target_id: int):
    rows = []
    if viewer_id == target_id:
        rows.append([btn("📝 Составить отчет", "rep:new")])
        me = get_user(viewer_id)
        my_rank = me["rank"] if me else None
        if my_rank in ADDER_RANKS or is_admin(viewer_id):
            rows.append([btn("➕ Добавить работника", "aw:start")])
        if my_rank in REQUESTER_RANKS and not is_admin(viewer_id):
            rows.append([btn("🙋 Запросить команду", "tr:start")])
        if is_lead_viewer(viewer_id):
            rows.append([btn("📋 Посмотреть отчеты", "rep:open:0")])
        if teams_led_by(target_id):
            rows.append([btn("📊 Статистика моей команды", f"prof:team:{target_id}")])
    else:
        rows.append([btn("✉️ Записка", f"note:{target_id}"), btn("💸 Направить выплату", f"pay:{target_id}")])
        rows.append([btn("⬆️ Повысить", f"rk:up:{target_id}"), btn("⬇️ Понизить", f"rk:dn:{target_id}")])
        rows.append([btn("🔀 Перераспределить", f"mv:pick:{target_id}")])
        extra = [btn("📋 Отчеты", f"rep:open:{target_id}")]
        if teams_led_by(target_id):
            extra.append(btn("📊 Статистика команды", f"prof:team:{target_id}"))
        rows.append(extra)
        rows.append([btn("🏠 Мой профиль", "prof:me")])
    return kb(rows)


async def show_profile(target, viewer_id: int, uid: int):
    show_links = viewer_id == uid or is_admin(viewer_id)
    await show(target, profile_text(uid, show_links), profile_kb(viewer_id, uid))


# -------------------- РОУТЕРЫ --------------------
router = Router(name="teams")
router.message.filter(F.chat.type == "private")
admin_router = Router(name="teams_admin")
admin_router.message.filter(F.chat.type == "private", F.from_user.id.in_(ADMIN_IDS))
admin_router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))
router.include_router(admin_router)


class ReportFSM(StatesGroup):
    hours = State()
    scouts = State()
    models = State()
    operators = State()


class NoteFSM(StatesGroup):
    text = State()


class AdminFSM(StatesGroup):
    new_leader = State()
    new_name = State()
    add_member = State()
    change_leader = State()
    rename = State()
    search = State()
    ban_input = State()
    bc_text = State()
    bc_confirm = State()
    grp_text = State()
    grp_confirm = State()
    newmodel = State()
    reject_reason = State()
    nm_user = State()
    nm_confirm = State()
    rq_reason = State()
    rq_name = State()


class WorkerFSM(StatesGroup):
    ident = State()


class TeamReqFSM(StatesGroup):
    name = State()
    comment = State()


@router.message(Command("cancel"), StateFilter("*"))
async def cmd_cancel(message: Message, state: FSMContext):
    if await state.get_state() is None:
        return
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb())


# ---------- реферальный вход ----------
async def handle_start_payload(message: Message, args: Optional[str], state: FSMContext, user=None) -> bool:
    """Вызывать в начале /start. True — payload обработан, дальше ничего не делать.
    user — реальный пользователь; нужен, когда message пришло из callback (там from_user = бот)."""
    if not args:
        return False
    user = user or message.from_user
    if args == "report":
        await begin_report(message, user.id, state)
        return True
    if args.startswith("ref_"):
        await join_flow(message, args[4:], user)
        return True
    if args.startswith("inv_"):
        await redeem_invite(message, args[4:], user)
        return True
    return False


async def join_flow(message: Message, code: str, user=None):
    team = team_by_ref(code)
    if not team:
        await message.answer("Ссылка недействительна или команда расформирована.")
        return
    u = user or message.from_user
    touch_user(u)
    if u.id == team["leader_id"]:
        await message.answer("Вы лидер этой команды — по своей ссылке вступать не нужно.")
        return
    cur = member_team(u.id)
    if cur:
        if cur["id"] == team["id"]:
            await message.answer(f"Вы уже в команде «{esc(team['name'])}».", reply_markup=main_menu_kb())
        else:
            await message.answer(f"Вы уже состоите в команде «{esc(cur['name'])}». "
                                 f"Чтобы сменить команду, напишите наставнику или администратору.")
        return
    row = get_user(u.id)
    if row["rank"]:
        await finish_join(message.bot, u, team, None, message)
        return
    await message.answer(
        f"Вас пригласили в команду «{esc(team['name'])}». Кем вы хотите работать?",
        reply_markup=kb([[btn("🔎 Скаут", f"reg:{code}:scout"),
                          btn("🎧 Оператор", f"reg:{code}:operator"),
                          btn("📸 Модель", f"reg:{code}:model")]]))


async def finish_join(bot: Bot, user, team, kind: Optional[str], reply_to: Message):
    if kind:
        set_rank(user.id, kind, 1)
    add_member(user.id, team["id"], via_ref=1)
    row = get_user(user.id)
    await reply_to.answer(
        f"✅ Вы в команде «{esc(team['name'])}». Ваш ранг: {esc(rank_label(row['rank'], row['level']))}.\n"
        f"Профиль и отчёты — кнопка «👤 Профиль» или /profile.",
        reply_markup=main_menu_kb())
    await safe_send(bot, team["leader_id"],
                    f"✅ По вашей ссылке в команду «{esc(team['name'])}» вступил(а) {who(row)} — "
                    f"{esc(rank_label(row['rank'], row['level']))}.")


@router.callback_query(F.data.startswith("reg:"))
async def cb_register(cb: CallbackQuery):
    _, code, kind = cb.data.split(":")
    team = team_by_ref(code)
    if kind not in KIND_TITLES or not team:
        await cb.answer("Ссылка недействительна.", show_alert=True)
        return
    if member_team(cb.from_user.id) or cb.from_user.id == team["leader_id"]:
        await cb.answer("Вы уже в команде.", show_alert=True)
        return
    touch_user(cb.from_user)
    await cb.message.edit_reply_markup(reply_markup=None)
    await finish_join(cb.bot, cb.from_user, team, kind, cb.message)
    await cb.answer()


# ---------- профиль ----------
@router.message(Command("profile"))
@router.message(F.text == "👤 Профиль")
async def msg_profile(message: Message, state: FSMContext):
    await state.clear()
    touch_user(message.from_user)
    uid = message.from_user.id
    await show_profile(message, uid, uid)


@router.callback_query(F.data == "prof:me")
async def cb_prof_me(cb: CallbackQuery):
    await show_profile(cb, cb.from_user.id, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.startswith("prof:view:"))
async def cb_prof_view(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if uid != cb.from_user.id and not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа к этому профилю.", show_alert=True)
        return
    await show_profile(cb, cb.from_user.id, uid)
    await cb.answer()


@router.callback_query(F.data.startswith("prof:team:"))
async def cb_prof_team(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if uid != cb.from_user.id and not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    teams = teams_led_by(uid)
    if not teams:
        await cb.answer("Этот человек не лидер команды.", show_alert=True)
        return
    s7 = since7()
    L = [f"📊 <b>Статистика команд</b> — {who(get_user(uid), uid)}"]
    for t in teams:
        L += ["", f"<b>«{esc(t['name'])}»</b> · {t['members']} чел.",
              f"7 дней: {fmt_tot(team_totals(t['id'], s7))}",
              f"Всё время: {fmt_tot(team_totals(t['id']))}"]
        for m in team_members(t["id"])[:15]:
            tt = user_totals(m["user_id"], s7)
            L.append(f"• {who(m)} · {esc(rank_label(m['rank'], m['level']))} · "
                     f"7д: {tt['h']:g} ч, приведено {tt['s'] + tt['m'] + tt['o']}")
    back = "prof:me" if uid == cb.from_user.id else f"prof:view:{uid}"
    await show(cb, "\n".join(L)[:4000], kb([[btn("⬅️ Профиль", back)]]))
    await cb.answer()


@router.callback_query(F.data.startswith("pay:"))
async def cb_pay(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    if not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    # TODO: подключить способы оплаты
    await cb.answer("💸 Выплаты пока не подключены: способы оплаты ещё не настроены.", show_alert=True)


@router.callback_query(F.data.regexp(r"^rk:(up|dn):\d+$"))
async def cb_rank(cb: CallbackQuery):
    _, d, uid = cb.data.split(":")
    uid = int(uid)
    ok, msg = change_rank(cb.from_user.id, uid, 1 if d == "up" else -1)
    if not ok:
        await cb.answer(msg, show_alert=True)
        return
    row = get_user(uid)
    await safe_send(cb.bot, uid, f"🔔 Ваш ранг изменён: <b>{esc(rank_label(row['rank'], row['level']))}</b>.")
    await show_profile(cb, cb.from_user.id, uid)
    await cb.answer(msg)


# ---------- записка ----------
@router.callback_query(F.data.startswith("note:"))
async def cb_note(cb: CallbackQuery, state: FSMContext):
    uid = int(cb.data.split(":")[1])
    if not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(NoteFSM.text)
    await state.update_data(to=uid)
    await cb.message.answer(f"✉️ Напишите записку для {who(get_user(uid), uid)}. Она придёт ему в бота.\n/cancel — отмена.")
    await cb.answer()


@router.message(NoteFSM.text, F.text)
async def note_text(message: Message, state: FSMContext):
    data = await state.get_data()
    uid = data["to"]
    text = message.text.strip()[:3000]
    me = get_user(message.from_user.id)
    ok = await safe_send(message.bot, uid,
                         f"✉️ <b>Записка от</b> {who(me, message.from_user.id)} "
                         f"({esc(rank_label(me['rank'], me['level'])) if me else 'админ'}):\n\n{esc(text)}")
    with db() as con:
        con.execute("INSERT INTO notes(from_id,to_id,text,created_at) VALUES(?,?,?,?)",
                    (message.from_user.id, uid, text, utcnow()))
    await state.clear()
    await message.answer("✅ Записка отправлена." if ok else "⚠️ Не удалось доставить (человек заблокировал бота).",
                         reply_markup=kb([[btn("👤 К профилю", f"prof:view:{uid}")]]))


# ---------- перераспределение ----------
@router.callback_query(F.data.startswith("mv:pick:"))
async def cb_mv_pick(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    cur = member_team(uid)
    teams = sorted(teams_with_stats(), key=lambda x: -x["brought7"])[:40]
    rows = []
    for t in teams:
        mark = "✅ " if cur and cur["id"] == t["id"] else ""
        rows.append([btn(f"{mark}{t['name']} · {t['members']} чел · 7д: {t['brought7']}", f"mv:do:{uid}:{t['id']}")])
    rows.append([btn("⬅️ Назад", f"prof:view:{uid}")])
    text = (f"🔀 <b>Перераспределение</b>: {who(get_user(uid), uid)}\n"
            f"Сейчас: {('«' + esc(cur['name']) + '»') if cur else '—'}\n\n"
            f"Команды отсортированы по результату за 7 дней (приведено людей). "
            f"Сильных — в сильные, слабых — в слабые.")
    if not teams:
        text += "\n\nКоманд пока нет."
    await show(cb, text, kb(rows))
    await cb.answer()


async def move_user(bot: Bot, uid: int, tid: int, by: int):
    """Возвращает текст ошибки или None."""
    t = team_by_id(tid)
    if not t:
        return "Команда не найдена."
    if t["leader_id"] == uid:
        return "Этот человек — лидер выбранной команды."
    cur = member_team(uid)
    if cur and cur["id"] == tid:
        return "Он уже в этой команде."
    add_member(uid, tid, via_ref=0, added_by=by)
    await safe_send(bot, uid, f"🔀 Вы переведены в команду «{esc(t['name'])}».")
    row = get_user(uid)
    if t["leader_id"] != by:
        await safe_send(bot, t["leader_id"], f"➕ В вашу команду «{esc(t['name'])}» переведён(а) {who(row)}.")
    if cur and cur["leader_id"] not in (by, t["leader_id"]):
        await safe_send(bot, cur["leader_id"], f"➖ {who(row)} переведён(а) из вашей команды «{esc(cur['name'])}» "
                                               f"в «{esc(t['name'])}».")
    return None


@router.callback_query(F.data.startswith("mv:do:"))
async def cb_mv_do(cb: CallbackQuery):
    _, _, uid, tid = cb.data.split(":")
    uid, tid = int(uid), int(tid)
    if not can_manage(cb.from_user.id, uid):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    err = await move_user(cb.bot, uid, tid, cb.from_user.id)
    if err:
        await cb.answer(err, show_alert=True)
        return
    await show_profile(cb, cb.from_user.id, uid)
    await cb.answer("Перераспределено ✅")


# ---------- отчёты: составление ----------
def report_preview(d: dict) -> str:
    hours = f"{d['hours']:g}" if "hours" in d else "…"
    scouts = str(d.get("scouts", "…"))
    models = str(d.get("models", "…"))
    operators = str(d.get("operators", "…"))
    date = msk_now().strftime("%d.%m.%Y")
    return "\n".join([
        f"📝 <b>Отчёт за {date}</b>", "",
        f"1. Сколько часов сегодня отработано: <b>{hours}</b>",
        f"2. Сколько скаутов приведено: <b>{scouts}</b>",
        f"3. Сколько моделей приведено: <b>{models}</b>",
        f"4. Сколько операторов приведено: <b>{operators}</b>",
    ])


CANCEL_KB = kb([[btn("❌ Отмена", "rep:cancel")]])
STEPS = {
    "hours": ("Сколько часов сегодня отработано? (число, можно 5.5)", ReportFSM.scouts),
    "scouts": ("Сколько скаутов приведено? (число)", ReportFSM.models),
    "models": ("Сколько моделей приведено? (число)", ReportFSM.operators),
    "operators": ("", None),
}


async def begin_report(message: Message, user_id: int, state: FSMContext):
    u = get_user(user_id)
    if not u or not u["rank"]:
        await message.answer("Отчёты доступны участникам команды. Попросите наставника дать реферальную ссылку.")
        return
    await state.clear()
    await state.set_state(ReportFSM.hours)
    await message.answer(report_preview({}) + "\n\n<b>Шаг 1/4.</b> Сколько часов сегодня отработано? (число, можно 5.5)",
                         reply_markup=CANCEL_KB)


@router.message(Command("report"))
async def cmd_report(message: Message, state: FSMContext):
    await begin_report(message, message.from_user.id, state)


@router.callback_query(F.data == "rep:new")
async def cb_rep_new(cb: CallbackQuery, state: FSMContext):
    await begin_report(cb.message, cb.from_user.id, state)
    await cb.answer()


@router.callback_query(F.data == "rep:cancel")
async def cb_rep_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("Отчёт отменён.")
    await cb.answer()


@router.message(ReportFSM.hours, F.text)
async def rep_hours(message: Message, state: FSMContext):
    try:
        h = round(float(message.text.replace(",", ".").strip()), 2)
        assert 0 <= h <= 24
    except (ValueError, AssertionError):
        await message.answer("Введите число часов от 0 до 24, например 6 или 5.5", reply_markup=CANCEL_KB)
        return
    await state.update_data(hours=h)
    await state.set_state(ReportFSM.scouts)
    await message.answer(report_preview(await state.get_data()) + "\n\n<b>Шаг 2/4.</b> Сколько скаутов приведено?",
                         reply_markup=CANCEL_KB)


async def _count_step(message: Message, state: FSMContext, key: str, nxt, prompt: str):
    t = message.text.strip()
    if not t.isdigit() or int(t) > 9999:
        await message.answer("Введите целое число (0 или больше).", reply_markup=CANCEL_KB)
        return
    await state.update_data(**{key: int(t)})
    await state.set_state(nxt)
    await message.answer(report_preview(await state.get_data()) + f"\n\n{prompt}", reply_markup=CANCEL_KB)


@router.message(ReportFSM.scouts, F.text)
async def rep_scouts(message: Message, state: FSMContext):
    await _count_step(message, state, "scouts", ReportFSM.models, "<b>Шаг 3/4.</b> Сколько моделей приведено?")


@router.message(ReportFSM.models, F.text)
async def rep_models(message: Message, state: FSMContext):
    await _count_step(message, state, "models", ReportFSM.operators, "<b>Шаг 4/4.</b> Сколько операторов приведено?")


@router.message(ReportFSM.operators, F.text)
async def rep_operators(message: Message, state: FSMContext):
    t = message.text.strip()
    if not t.isdigit() or int(t) > 9999:
        await message.answer("Введите целое число (0 или больше).", reply_markup=CANCEL_KB)
        return
    d = await state.get_data()
    d["operators"] = int(t)
    rid = save_report(message.from_user.id, d["hours"], d["scouts"], d["models"], d["operators"])
    await state.clear()
    await message.answer("✅ <b>Отчёт отправлен на проверку</b> — вы получите уведомление, когда админ его "
                         "подтвердит.\n\n" + report_preview(d),
                         reply_markup=kb([[btn("👤 Профиль", "prof:me")]]))
    await notify_admins_report(message.bot, rid)


# ---------- отчёты: просмотр ----------
def report_text(r) -> str:
    return "\n".join([
        f"👤 {who(r)} · {esc(rank_label(r['rank'], r['level']))}",
        "━━━━━━━━━━",
        f"📅 <b>Отчёт за {fmt_date(r['report_date'])}</b> (отправлен {fmt_dt(r['created_at'])} МСК)", "",
        f"1. Часов отработано: <b>{r['hours']:g}</b>",
        f"2. Скаутов приведено: <b>{r['scouts']}</b>",
        f"3. Моделей приведено: <b>{r['models']}</b>",
        f"4. Операторов приведено: <b>{r['operators']}</b>", "",
        report_status_line(r),
    ])


def report_status_line(r) -> str:
    line = f"Статус: {REPORT_STATUS.get(r['status'], r['status'])}"
    if r["status"] == "rejected" and r["review_note"]:
        line += f" — {esc(r['review_note'])}"
    return line


def report_kb(r, uid_filter: int, admin: bool = False):
    return kb((review_rows(r) if admin else []) + [
        [btn("🏠 Профиль", "prof:me"), btn("🔀 Перераспределить", f"mv:pick:{r['user_id']}")],
        [btn("➡️ Далее", f"rep:n:{r['id']}:{uid_filter}"), btn("👤 Перейти в профиль", f"prof:view:{r['user_id']}")],
    ])


@router.callback_query(F.data.startswith("rep:open:"))
async def cb_rep_open(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if not is_lead_viewer(cb.from_user.id):
        await cb.answer("Отчёты доступны тимлидам и выше.", show_alert=True)
        return
    r = report_page(cb.from_user.id, uid, 0)
    if not r:
        await cb.answer("Отчётов пока нет.", show_alert=True)
        return
    await show(cb, report_text(r), report_kb(r, uid, is_admin(cb.from_user.id)))
    await cb.answer()


@router.callback_query(F.data.startswith("rep:n:"))
async def cb_rep_next(cb: CallbackQuery):
    _, _, rid, uid = cb.data.split(":")
    if not is_lead_viewer(cb.from_user.id):
        await cb.answer("Нет доступа.", show_alert=True)
        return
    r = report_page(cb.from_user.id, int(uid), int(rid))
    if not r:
        await cb.answer("Это последний отчёт.", show_alert=True)
        return
    await show(cb, report_text(r), report_kb(r, int(uid), is_admin(cb.from_user.id)))
    await cb.answer()


# ---------- чаты и напоминания ----------
@router.my_chat_member()
async def on_my_chat_member(ev: ChatMemberUpdated, bot: Bot):
    chat = ev.chat
    if chat.type not in ("group", "supergroup"):
        return
    status = ev.new_chat_member.status
    if status in ("member", "administrator"):
        by_admin = ev.from_user.id in ADMIN_IDS
        with db() as con:
            existed = con.execute("SELECT 1 FROM chats WHERE chat_id=?", (chat.id,)).fetchone()
            con.execute("""
                INSERT INTO chats(chat_id,title,type,enabled,added_by,added_at) VALUES(?,?,?,?,?,?)
                ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title""",
                        (chat.id, chat.title or "", chat.type, 1 if by_admin else 0, ev.from_user.id, utcnow()))
        if not existed:
            text = (f"✅ Бот подключён к чату «{esc(chat.title)}». Напоминания включены (18:00–00:00 МСК, раз в час)."
                    if by_admin else
                    f"⚠️ Бота добавил(а) {esc(ev.from_user.full_name)} (<code>{ev.from_user.id}</code>) в чат "
                    f"«{esc(chat.title)}». Напоминания выключены — включите в /opcaadmin → Чаты.")
            for a in ADMIN_IDS:
                await safe_send(bot, a, text)
    elif status in ("left", "kicked"):
        with db() as con:
            con.execute("DELETE FROM chats WHERE chat_id=?", (chat.id,))


async def send_reminders(bot: Bot) -> int:
    with db() as con:
        chats = con.execute("SELECT chat_id FROM chats WHERE enabled=1").fetchall()
    sent = 0
    markup = None
    if _BOT_USERNAME:
        markup = kb([[InlineKeyboardButton(text="📝 Заполнить отчёт", url=f"https://t.me/{_BOT_USERNAME}?start=report")]])
    for c in chats:
        chat_id = c["chat_id"]
        for _ in range(2):
            try:
                await bot.send_message(chat_id, REMINDER_TEXT, reply_markup=markup)
                sent += 1
                break
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramMigrateToChat as e:
                with db() as con:
                    con.execute("UPDATE chats SET chat_id=? WHERE chat_id=?", (e.migrate_to_chat_id, chat_id))
                chat_id = e.migrate_to_chat_id
            except (TelegramForbiddenError, TelegramBadRequest):
                with db() as con:
                    con.execute("UPDATE chats SET enabled=0 WHERE chat_id=?", (chat_id,))
                break
            except TelegramAPIError:
                log.exception("reminder failed for %s", chat_id)
                break
        await asyncio.sleep(0.05)
    return sent


async def reminder_loop(bot: Bot):
    while True:
        try:
            now = msk_now()
            nxt = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            await asyncio.sleep(max(1.0, (nxt - now).total_seconds() + 1))
            if nxt.hour in REMINDER_HOURS:
                await send_reminders(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reminder loop error")
            await asyncio.sleep(30)


# ======================= АДМИН-ПАНЕЛЬ /opcaadm =======================
def admin_menu_kb():
    n_req = count_pending_team_requests()
    return kb([
        [btn("📥 Заявки", "adm:apps:pending"), btn("🧾 Отчёты на проверку", "adm:reports")],
        [btn("🙋 Запросы команд" + (f" ({n_req})" if n_req else ""), "adm:rqs")],
        [btn("📊 Статистика", "adm:stats"), btn("🎉 Разослать модель", "adm:nm")],
        [btn("👥 Команды", "adm:teams"), btn("🙍 Пользователи", "adm:users")],
        [btn("🆕 Новые участники", "adm:joins"), btn("📸 Отметить новую модель", "adm:newmodel")],
        [btn("📨 Рассылка", "adm:bc")],
        [btn("📝 Кто не сдал отчёт", "adm:noreport"), btn("🏆 Рейтинг", "adm:top")],
        [btn("📣 В группу моделей", "adm:grp:model"), btn("📣 В группу скаутов", "adm:grp:scout")],
        [btn("🚫 Блокировки", "adm:bans"), btn("💬 Чаты и напоминания", "adm:chats")],
        [btn("📄 Выгрузить пользователей CSV", "adm:csv")],
    ])


@admin_router.message(Command("opcaadmin", "opcaadm"))
async def cmd_opcaadm(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=admin_menu_kb())


@admin_router.callback_query(F.data == "adm:home")
async def adm_home(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(cb, "🛠 <b>Админ-панель</b>", admin_menu_kb())
    await cb.answer()


# ---------- команды ----------
@admin_router.callback_query(F.data == "adm:teams")
async def adm_teams(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    teams = teams_with_stats()[:60]
    rows = [[btn(f"{t['name']} · {t['members']} чел", f"adm:t:{t['id']}")] for t in teams]
    rows.append([btn("➕ Создать новую команду", "adm:tnew")])
    rows.append([btn("⬅️ Назад", "adm:home")])
    await show(cb, "👥 <b>Команды</b>" + ("" if teams else "\n\nПока нет ни одной команды."), kb(rows))
    await cb.answer()


def team_card(tid: int):
    t = team_by_id(tid)
    if not t:
        return "Команда не найдена.", kb([[btn("⬅️ Команды", "adm:teams")]])
    leader = get_user(t["leader_id"])
    members = team_members(tid)
    text = "\n".join([
        f"👥 <b>«{esc(t['name'])}»</b>",
        f"Лидер: {who(leader, t['leader_id'])} · {esc(rank_label(leader['rank'], leader['level'])) if leader else ''}",
        f"Участников: {len(members)}",
        f"Создана: {fmt_dt(t['created_at'])} МСК",
        f"🔗 <code>{bot_link(t['ref_code'])}</code>", "",
        f"7 дней: {fmt_tot(team_totals(tid, since7()))}",
        f"Всё время: {fmt_tot(team_totals(tid))}",
    ])
    return text, kb([
        [btn("👥 Участники", f"adm:tm:{tid}"), btn("➕ Добавить человека", f"adm:tadd:{tid}")],
        [btn("👑 Сменить лидера", f"adm:tlead:{tid}"), btn("✏️ Переименовать", f"adm:tren:{tid}")],
        [btn("🔗 Новая ссылка", f"adm:tlink:{tid}"), btn("🗑 Расформировать", f"adm:tdel:{tid}")],
        [btn("⬅️ Команды", "adm:teams")],
    ])


@admin_router.callback_query(F.data.regexp(r"^adm:t:\d+$"))
async def adm_team(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    text, markup = team_card(int(cb.data.split(":")[2]))
    await show(cb, text, markup)
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:tm:"))
async def adm_team_members(cb: CallbackQuery):
    if await render_team_members(cb, int(cb.data.split(":")[2])):
        await cb.answer()
    else:
        await cb.answer("Команда не найдена.", show_alert=True)


async def render_team_members(cb: CallbackQuery, tid: int) -> bool:
    t = team_by_id(tid)
    if not t:
        return False
    members = team_members(tid)[:40]
    rows = []
    for m in members:
        name = f"@{m['username']}" if m["username"] else (m["first_name"] or str(m["user_id"]))
        rows.append([btn(f"{name} · {rank_label(m['rank'], m['level'])}", f"adm:u:{m['user_id']}"),
                     btn("🚪", f"adm:kick:{m['user_id']}:{tid}")])
    rows.append([btn("⬅️ К команде", f"adm:t:{tid}")])
    await show(cb, f"👥 Участники «{esc(t['name'])}» ({len(members)}). 🚪 — исключить из команды." +
               ("" if members else "\n\nПока никого."), kb(rows))
    return True


@admin_router.callback_query(F.data.startswith("adm:kick:"))
async def adm_kick(cb: CallbackQuery):
    _, _, uid, tid = cb.data.split(":")
    uid, tid = int(uid), int(tid)
    t = team_by_id(tid)
    if t and remove_member(uid, tid):
        await safe_send(cb.bot, uid, f"🚪 Вы исключены из команды «{esc(t['name'])}».")
        await safe_send(cb.bot, t["leader_id"], f"🚪 {who(get_user(uid), uid)} исключён(а) из вашей команды «{esc(t['name'])}».")
        note, alert = "Исключён(а) ✅", False
    else:
        note, alert = "Он уже не в этой команде.", True
    if t:
        await render_team_members(cb, tid)
    await cb.answer(note, show_alert=alert)


@admin_router.callback_query(F.data.startswith("adm:tlink:"))
async def adm_tlink(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    if not team_by_id(tid):
        await cb.answer("Команда не найдена.", show_alert=True)
        return
    with db() as con:
        con.execute("UPDATE teams SET ref_code=? WHERE id=?", (new_ref_code(), tid))
    text, markup = team_card(tid)
    await show(cb, text, markup)
    await cb.answer("Ссылка обновлена. Старая больше не работает.", show_alert=True)


@admin_router.callback_query(F.data.startswith("adm:tdel:"))
async def adm_tdel(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    t = team_by_id(tid)
    if not t:
        await cb.answer("Команда не найдена.", show_alert=True)
        return
    await show(cb, f"🗑 Расформировать «{esc(t['name'])}»? Участники останутся без команды, ссылка перестанет работать.",
               kb([[btn("✅ Да, расформировать", f"adm:tdelok:{tid}")], [btn("⬅️ Отмена", f"adm:t:{tid}")]]))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:tdelok:"))
async def adm_tdelok(cb: CallbackQuery):
    tid = int(cb.data.split(":")[2])
    t = team_by_id(tid)
    if not t:
        await cb.answer("Уже удалена.", show_alert=True)
        return
    members = team_members(tid)
    with db() as con:
        con.execute("DELETE FROM team_members WHERE team_id=?", (tid,))
        con.execute("DELETE FROM invites WHERE team_id=? AND claimed_by IS NULL", (tid,))
        con.execute("DELETE FROM teams WHERE id=?", (tid,))
    for uid in {t["leader_id"], *[m["user_id"] for m in members]}:
        await safe_send(cb.bot, uid, f"⚠️ Команда «{esc(t['name'])}» расформирована.")
        await asyncio.sleep(0.05)
    teams = teams_with_stats()[:60]
    rows = [[btn(f"{x['name']} · {x['members']} чел", f"adm:t:{x['id']}")] for x in teams]
    rows += [[btn("➕ Создать новую команду", "adm:tnew")], [btn("⬅️ Назад", "adm:home")]]
    await show(cb, f"✅ Команда «{esc(t['name'])}» расформирована.\n\n👥 <b>Команды</b>", kb(rows))
    await cb.answer()


# --- создание команды ---
@admin_router.callback_query(F.data == "adm:tnew")
async def adm_tnew(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.new_leader)
    await cb.message.answer("Отправьте @username или ID будущего лидера (он должен хотя бы раз запустить бота).\n/cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.new_leader, F.text)
async def adm_new_leader(message: Message, state: FSMContext):
    u = find_user(message.text)
    if not u:
        await message.answer("Не нашёл такого пользователя. Он должен был запускать бота. Попробуйте ещё раз или /cancel.")
        return
    await state.update_data(leader_id=u["user_id"])
    await state.set_state(AdminFSM.new_name)
    await message.answer(f"Лидер: {who(u)}\nТеперь введите название команды (до 40 символов).")


@admin_router.message(AdminFSM.new_name, F.text)
async def adm_new_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not 1 <= len(name) <= 40:
        await message.answer("Название должно быть от 1 до 40 символов.")
        return
    data = await state.get_data()
    leader_id = data["leader_id"]
    tid = create_team(name, leader_id)
    if tid is None:
        await message.answer("Команда с таким названием уже есть. Введите другое.")
        return
    await state.clear()
    promoted = ensure_leader_rank(leader_id)
    t = team_by_id(tid)
    ok = await safe_send(
        message.bot, leader_id,
        f"Администратор назначил вас в команду «{esc(name)}».\n\n"
        f"Ваша реферальная ссылка — по ней вы набираете людей в команду, если это нужно:\n"
        f"<code>{bot_link(t['ref_code'])}</code>")
    text, markup = team_card(tid)
    extra = ("\n\n⬆️ Лидеру выдан ранг «Младший тимлид 1 ур.»." if promoted else "")
    extra += ("" if ok else "\n⚠️ Не удалось отправить уведомление лидеру.")
    await message.answer("✅ Команда создана.\n\n" + text + extra, reply_markup=markup)


# --- добавить человека ---
@admin_router.callback_query(F.data.startswith("adm:tadd:"))
async def adm_tadd(cb: CallbackQuery, state: FSMContext):
    tid = int(cb.data.split(":")[2])
    await state.set_state(AdminFSM.add_member)
    await state.update_data(tid=tid)
    await cb.message.answer("Отправьте @username или ID человека, которого добавить в команду. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.add_member, F.text)
async def adm_add_member(message: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    u = find_user(message.text)
    if not u:
        await message.answer("Не нашёл такого пользователя (он должен был запускать бота). Ещё раз или /cancel.")
        return
    err = await move_user(message.bot, u["user_id"], tid, message.from_user.id)
    if err:
        await message.answer(f"⚠️ {err}")
        return
    await state.clear()
    rows = [[btn("⬅️ К команде", f"adm:t:{tid}")]]
    text = f"✅ {who(u)} добавлен(а) в команду."
    if not u["rank"]:
        text += "\nУ него нет роли — выберите:"
        rows.insert(0, [btn(KIND_TITLES[k], f"adm:role:{u['user_id']}:{k}:{tid}") for k in KIND_TITLES])
    await message.answer(text, reply_markup=kb(rows))


# --- сменить лидера / переименовать ---
@admin_router.callback_query(F.data.startswith("adm:tlead:"))
async def adm_tlead(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.change_leader)
    await state.update_data(tid=int(cb.data.split(":")[2]))
    await cb.message.answer("Отправьте @username или ID нового лидера. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.change_leader, F.text)
async def adm_change_leader(message: Message, state: FSMContext):
    tid = (await state.get_data())["tid"]
    t = team_by_id(tid)
    u = find_user(message.text)
    if not t or not u:
        await message.answer("Команда или пользователь не найдены. Ещё раз или /cancel.")
        return
    old = t["leader_id"]
    with db() as con:
        con.execute("UPDATE teams SET leader_id=? WHERE id=?", (u["user_id"], tid))
        con.execute("DELETE FROM team_members WHERE user_id=? AND team_id=?", (u["user_id"], tid))
    ensure_leader_rank(u["user_id"])
    await state.clear()
    await safe_send(message.bot, u["user_id"],
                    f"Администратор назначил вас лидером команды «{esc(t['name'])}».\n\n"
                    f"Ваша реферальная ссылка:\n<code>{bot_link(t['ref_code'])}</code>")
    if old != u["user_id"]:
        await safe_send(message.bot, old, f"ℹ️ Вы больше не лидер команды «{esc(t['name'])}».")
    text, markup = team_card(tid)
    await message.answer("✅ Лидер изменён.\n\n" + text, reply_markup=markup)


@admin_router.callback_query(F.data.startswith("adm:tren:"))
async def adm_tren(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.rename)
    await state.update_data(tid=int(cb.data.split(":")[2]))
    await cb.message.answer("Введите новое название команды. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.rename, F.text)
async def adm_rename(message: Message, state: FSMContext):
    name = message.text.strip()
    tid = (await state.get_data())["tid"]
    if not 1 <= len(name) <= 40:
        await message.answer("Название должно быть от 1 до 40 символов.")
        return
    try:
        if team_name_taken(name, exclude_id=tid):
            raise sqlite3.IntegrityError
        with db() as con:
            con.execute("UPDATE teams SET name=? WHERE id=?", (name, tid))
    except sqlite3.IntegrityError:
        await message.answer("Команда с таким названием уже есть.")
        return
    await state.clear()
    text, markup = team_card(tid)
    await message.answer("✅ Переименовано.\n\n" + text, reply_markup=markup)


# ---------- пользователи ----------
def admin_user_card(uid: int):
    u = get_user(uid)
    if not u:
        return "Пользователь не найден.", kb([[btn("⬅️ Назад", "adm:users")]])
    banned = is_banned(uid, u["username"])
    mem = member_team(uid)
    move_row = [btn("🔀 В другую команду", f"mv:pick:{uid}")]
    if mem:
        move_row.append(btn("🚪 Исключить из команды", f"adm:kick:{uid}:{mem['id']}"))
    rows = [
        [btn("⬆️ Ранг", f"adm:rk:up:{uid}"), btn("⬇️ Ранг", f"adm:rk:dn:{uid}")],
        [btn(KIND_TITLES[k], f"adm:role:{uid}:{k}:0") for k in KIND_TITLES],
        move_row,
        [btn("✅ Разблокировать", f"adm:unbanu:{uid}") if banned else btn("🚫 Заблокировать", f"adm:ban:{uid}")],
        [btn("📋 Отчёты", f"rep:open:{uid}"), btn("⬅️ Назад", "adm:users")],
    ]
    text = profile_text(uid, True) + ("\n\n🚫 <b>Доступ к боту ограничен</b>" if banned else "")
    return text, kb(rows)


@admin_router.callback_query(F.data == "adm:users")
async def adm_users(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    with db() as con:
        users = con.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT 15").fetchall()
        total = con.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
    rows = [[btn(f"{('@' + u['username']) if u['username'] else (u['first_name'] or u['user_id'])} · "
                 f"{rank_label(u['rank'], u['level'])}", f"adm:u:{u['user_id']}")] for u in users]
    rows.append([btn("🔎 Найти по @юзу / ID", "adm:usearch")])
    rows.append([btn("⬅️ Назад", "adm:home")])
    await show(cb, f"🙍 <b>Пользователи</b> (всего {total}). Последние 15:", kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:usearch")
async def adm_usearch(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.search)
    await cb.message.answer("Отправьте @username или ID. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.search, F.text)
async def adm_search(message: Message, state: FSMContext):
    u = find_user(message.text)
    if not u:
        await message.answer("Не найден. Ещё раз или /cancel.")
        return
    await state.clear()
    text, markup = admin_user_card(u["user_id"])
    await message.answer(text, reply_markup=markup)


@admin_router.callback_query(F.data.regexp(r"^adm:u:\d+$"))
async def adm_user(cb: CallbackQuery):
    text, markup = admin_user_card(int(cb.data.split(":")[2]))
    await show(cb, text, markup)
    await cb.answer()


@admin_router.callback_query(F.data.regexp(r"^adm:rk:(up|dn):\d+$"))
async def adm_rk(cb: CallbackQuery):
    _, _, d, uid = cb.data.split(":")
    uid = int(uid)
    ok, msg = change_rank(cb.from_user.id, uid, 1 if d == "up" else -1)
    if not ok:
        await cb.answer(msg, show_alert=True)
        return
    row = get_user(uid)
    await safe_send(cb.bot, uid, f"🔔 Ваш ранг изменён: <b>{esc(rank_label(row['rank'], row['level']))}</b>.")
    text, markup = admin_user_card(uid)
    await show(cb, text, markup)
    await cb.answer(msg)


@admin_router.callback_query(F.data.startswith("adm:role:"))
async def adm_role(cb: CallbackQuery):
    _, _, uid, kind, tid = cb.data.split(":")
    uid, tid = int(uid), int(tid)
    if kind not in KIND_TITLES or not get_user(uid):
        await cb.answer("Ошибка.", show_alert=True)
        return
    set_rank(uid, kind, 1)
    await safe_send(cb.bot, uid, f"🔔 Вам назначена роль: <b>{KIND_TITLES[kind]}</b>.")
    if tid:
        text, markup = team_card(tid)
        await show(cb, text, markup)
    else:
        text, markup = admin_user_card(uid)
        await show(cb, text, markup)
    await cb.answer("Роль назначена ✅")


@admin_router.callback_query(F.data.startswith("adm:joins"))
async def adm_joins(cb: CallbackQuery):
    with db() as con:
        rows = con.execute("""
            SELECT tm.joined_at, tm.via_ref, tm.added_by, u.user_id, u.username, u.first_name, u.rank, u.level, t.name
            FROM team_members tm JOIN users u ON u.user_id=tm.user_id JOIN teams t ON t.id=tm.team_id
            ORDER BY tm.joined_at DESC LIMIT 25""").fetchall()
    L = ["🆕 <b>Последние вступления</b>", ""]
    for r in rows:
        src = "по рефералке" if r["via_ref"] else "добавлен админом/тимлидом"
        L.append(f"{fmt_dt(r['joined_at'])} · {who(r)} · {esc(rank_label(r['rank'], r['level']))} → «{esc(r['name'])}» ({src})")
    if not rows:
        L.append("Пока никого.")
    await show(cb, "\n".join(L)[:4000], kb([[btn("⬅️ Назад", "adm:home")]]))
    await cb.answer()


# ---------- блокировки ----------
@admin_router.callback_query(F.data == "adm:bans")
async def adm_bans(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    with db() as con:
        bans = con.execute("SELECT * FROM bans ORDER BY id DESC LIMIT 40").fetchall()
    rows = []
    for b in bans:
        label = (f"@{b['username']}" if b["username"] else "") or ""
        if b["user_id"]:
            label = (label + " " if label else "") + str(b["user_id"])
        rows.append([btn(f"✅ Разблокировать {label}"[:60], f"adm:unban:{b['id']}")])
    rows.append([btn("➕ Заблокировать", "adm:bannew")])
    rows.append([btn("⬅️ Назад", "adm:home")])
    await show(cb, f"🚫 <b>Блокировки</b> ({len(bans)}). Нажмите на запись, чтобы снять ограничение.", kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:bannew")
async def adm_bannew(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.ban_input)
    await cb.message.answer("Отправьте @username или ID, можно с причиной через пробел:\n"
                            "<code>@user спам</code>\n/cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.ban_input, F.text)
async def adm_ban_input(message: Message, state: FSMContext):
    parts = message.text.strip().split(maxsplit=1)
    ident, reason = parts[0], (parts[1] if len(parts) > 1 else "")
    if ident.lstrip("-").isdigit():
        bid = add_ban("id", ident, reason, message.from_user.id)
    elif ident.lstrip("@").replace("_", "").isalnum():
        bid = add_ban("username", ident, reason, message.from_user.id)
    else:
        await message.answer("Не похоже на @username или ID. Ещё раз или /cancel.")
        return
    if bid is None:
        await message.answer("Нельзя заблокировать администратора.")
        return
    await state.clear()
    await message.answer("🚫 Доступ ограничен.", reply_markup=kb([[btn("⬅️ К блокировкам", "adm:bans")]]))


@admin_router.callback_query(F.data.startswith("adm:unban:"))
async def adm_unban(cb: CallbackQuery, state: FSMContext):
    with db() as con:
        con.execute("DELETE FROM bans WHERE id=?", (int(cb.data.split(":")[2]),))
    await adm_bans(cb, state)


@admin_router.callback_query(F.data.startswith("adm:ban:"))
async def adm_ban_user(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    if add_ban("id", uid, "", cb.from_user.id) is None:
        await cb.answer("Нельзя заблокировать администратора.", show_alert=True)
        return
    text, markup = admin_user_card(uid)
    await show(cb, text, markup)
    await cb.answer("🚫 Заблокирован")


@admin_router.callback_query(F.data.startswith("adm:unbanu:"))
async def adm_unban_user(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    u = get_user(uid)
    with db() as con:
        con.execute("DELETE FROM bans WHERE user_id=? OR (username<>'' AND username=?)",
                    (uid, (u["username"] or "").lower() if u else ""))
    text, markup = admin_user_card(uid)
    await show(cb, text, markup)
    await cb.answer("✅ Разблокирован")


# ---------- чаты ----------
@admin_router.callback_query(F.data == "adm:chats")
async def adm_chats(cb: CallbackQuery):
    with db() as con:
        chats = con.execute("SELECT * FROM chats ORDER BY added_at DESC LIMIT 40").fetchall()
    rows = [[btn(f"{'🔔' if c['enabled'] else '🔕'} {c['title'] or c['chat_id']}"[:60], f"adm:chat:{c['chat_id']}")] for c in chats]
    rows.append([btn("📣 Отправить напоминание сейчас", "adm:remind")])
    rows.append([btn("⬅️ Назад", "adm:home")])
    text = ("💬 <b>Чаты</b>\nНапоминание «" + esc(REMINDER_TEXT) + "» уходит каждый час с 18:00 до 00:00 МСК. "
            "Нажмите на чат, чтобы включить/выключить.\n\nЧтобы подключить чат — добавьте бота в группу.")
    if not chats:
        text += "\n\nПока нет подключённых чатов."
    await show(cb, text, kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data.startswith("adm:chat:"))
async def adm_chat_toggle(cb: CallbackQuery):
    chat_id = int(cb.data.split(":")[2])
    with db() as con:
        con.execute("UPDATE chats SET enabled=1-enabled WHERE chat_id=?", (chat_id,))
    await adm_chats(cb)


@admin_router.callback_query(F.data == "adm:remind")
async def adm_remind(cb: CallbackQuery):
    n = await send_reminders(cb.bot)
    await cb.answer(f"Отправлено в чатов: {n}", show_alert=True)


# ======================= ФУНКЦИИ БЫВШЕГО /admin (теперь в /opcaadm) =======================
APP_STATUS_TITLES = {"pending": "⏳ Ожидают", "approved": "✅ Одобрены", "rejected": "❌ Отклонены"}
APP_KIND_TITLES = {"operator": "Оператор", "scout": "Скаут"}
BC_SEGMENTS = {"all": "Все", "models": "Модели", "scouts": "Скауты",
               "operators": "Операторы", "members": "Участники команд"}


def apps_list(status="pending", limit=15):
    with db() as con:
        return con.execute("""
            SELECT a.*,u.username,u.first_name FROM applications a
            LEFT JOIN users u ON u.user_id=a.user_id
            WHERE a.status=? ORDER BY a.id DESC LIMIT ?""", (status, limit)).fetchall()


def app_get(app_id):
    with db() as con:
        return con.execute("""
            SELECT a.*,u.username,u.first_name FROM applications a
            LEFT JOIN users u ON u.user_id=a.user_id WHERE a.id=?""", (app_id,)).fetchone()


def app_set_status(app_id, status):
    with db() as con:
        con.execute("UPDATE applications SET status=?, reviewed_at=? WHERE id=?", (status, utcnow(), app_id))


def application_admin_kb(app_id: int):
    """Клавиатура для уведомления админам о новой заявке (вместо старых admin:* кнопок)."""
    return kb([[btn("📥 Открыть заявку", f"adm:app:{app_id}")]])


def mark_blocked_user(uid: int):
    with db() as con:
        con.execute("UPDATE users SET is_blocked=1 WHERE user_id=?", (uid,))


def recipients(segment: str):
    base = "SELECT DISTINCT u.user_id FROM users u "
    with db() as con:
        if segment == "all":
            rows = con.execute(base + "WHERE u.is_blocked=0").fetchall()
        elif segment == "models":
            rows = con.execute(base + "WHERE u.is_blocked=0 AND u.model_interest=1").fetchall()
        elif segment in ("scouts", "operators"):
            kind = "scout" if segment == "scouts" else "operator"
            rows = con.execute(base + "JOIN applications a ON a.user_id=u.user_id "
                                      "WHERE u.is_blocked=0 AND a.kind=?", (kind,)).fetchall()
        elif segment == "members":
            rows = con.execute(base + "JOIN team_members tm ON tm.user_id=u.user_id "
                                      "WHERE u.is_blocked=0").fetchall()
        else:
            rows = []
    return [r["user_id"] for r in rows]


def global_stats():
    today = utcnow()[:10]
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    month = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    with db() as con:
        u = con.execute("""
            SELECT COUNT(*) AS users,
                   COALESCE(SUM(substr(created_at,1,10)=?),0) AS today,
                   COALESCE(SUM(created_at>=?),0) AS week,
                   COALESCE(SUM(created_at>=?),0) AS month,
                   COALESCE(SUM(model_interest=1),0) AS models
            FROM users""", (today, week, month)).fetchone()
        a = con.execute("""
            SELECT COUNT(*) AS apps,
                   COALESCE(SUM(status='pending'),0)  AS pending,
                   COALESCE(SUM(status='approved'),0) AS approved,
                   COALESCE(SUM(status='rejected'),0) AS rejected,
                   COALESCE(SUM(kind='operator'),0)   AS operators,
                   COALESCE(SUM(kind='scout'),0)      AS scouts
            FROM applications""").fetchone()
        x = con.execute("""
            SELECT (SELECT COUNT(*) FROM teams) AS teams,
                   (SELECT COUNT(*) FROM team_members) AS members,
                   (SELECT COUNT(*) FROM reports WHERE report_date=?) AS reports_today,
                   (SELECT COUNT(*) FROM reports WHERE status='pending') AS reports_pending,
                   (SELECT COUNT(*) FROM bans) AS bans,
                   (SELECT COUNT(*) FROM chats WHERE enabled=1) AS chats""",
                        (msk_now().date().isoformat(),)).fetchone()
    return {**dict(u), **dict(a), **dict(x)}


def _csv_safe(v):
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_users_csv() -> bytes:
    with db() as con:
        rows = con.execute("""
            SELECT u.user_id,u.username,u.first_name,u.role,u.model_interest,u.is_blocked,
                   u.rank,u.level,
                   (SELECT t.name FROM team_members tm JOIN teams t ON t.id=tm.team_id
                    WHERE tm.user_id=u.user_id) AS team,
                   u.created_at,u.last_seen,
                   (SELECT status FROM applications a WHERE a.user_id=u.user_id AND a.kind='operator' LIMIT 1) AS operator_status,
                   (SELECT status FROM applications a WHERE a.user_id=u.user_id AND a.kind='scout' LIMIT 1) AS scout_status
            FROM users u ORDER BY u.created_at DESC""").fetchall()
    s = io.StringIO()
    w = csv.writer(s)
    w.writerow(["user_id", "username", "first_name", "role", "model_interest", "is_blocked", "rank", "level",
                "team", "created_at", "last_seen", "operator_status", "scout_status"])
    for r in rows:
        w.writerow([_csv_safe(x) for x in tuple(r)])
    return s.getvalue().encode("utf-8-sig")


async def deliver(bot: Bot, chat_id: int, text: str) -> str:
    """'ok' | 'blocked' | 'fail: ...'"""
    for _ in range(2):
        try:
            await bot.send_message(chat_id, text)
            return "ok"
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 1)
        except TelegramForbiddenError:
            return "blocked"
        except TelegramAPIError as e:
            return f"fail: {e}"
    return "fail: flood"


def group_id(seg: str) -> int:
    return MODEL_GROUP_ID if seg == "model" else SCOUT_GROUP_ID


# ---------- заявки ----------
@admin_router.callback_query(F.data.regexp(r"^adm:apps:(pending|approved|rejected)$"))
async def adm_apps(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    status = cb.data.split(":")[2]
    apps = apps_list(status)
    rows = []
    for a in apps:
        name = f"@{a['username']}" if a["username"] else (a["first_name"] or a["user_id"])
        rows.append([btn(f"#{a['id']} · {APP_KIND_TITLES.get(a['kind'], a['kind'])} · {name}"[:60], f"adm:app:{a['id']}")])
    rows.append([btn(("• " if s == status else "") + t, f"adm:apps:{s}") for s, t in APP_STATUS_TITLES.items()])
    rows.append([btn("⬅️ Назад", "adm:home")])
    text = f"📥 <b>Заявки — {APP_STATUS_TITLES[status]}</b> (последние 15)"
    if not apps:
        text += "\n\nПусто."
    await show(cb, text, kb(rows))
    await cb.answer()


def app_card(app_id: int):
    a = app_get(app_id)
    if not a:
        return "Заявка не найдена.", kb([[btn("⬅️ Заявки", "adm:apps:pending")]])
    text = "\n".join([
        f"📥 <b>Заявка #{a['id']}</b> · {esc(APP_KIND_TITLES.get(a['kind'], a['kind']))}",
        who(a),
        f"Статус: {APP_STATUS_TITLES.get(a['status'], a['status'])}",
        f"Подана: {fmt_dt(a['created_at'])} МСК", "",
        "<b>Ответы:</b>", esc(a["answers"]),
    ])[:4000]
    rows = []
    if a["status"] == "pending":
        rows.append([btn("✅ Одобрить", f"adm:appok:{a['id']}"), btn("❌ Отклонить", f"adm:appno:{a['id']}")])
    rows.append([btn("🙍 Пользователь", f"adm:u:{a['user_id']}"), btn("⬅️ Заявки", f"adm:apps:{a['status']}")])
    return text, kb(rows)


@admin_router.callback_query(F.data.regexp(r"^adm:app:\d+$"))
async def adm_app(cb: CallbackQuery):
    text, markup = app_card(int(cb.data.split(":")[2]))
    await show(cb, text, markup)
    await cb.answer()


@admin_router.callback_query(F.data.regexp(r"^adm:app(ok|no):\d+$"))
async def adm_app_decide(cb: CallbackQuery):
    _, action, aid = cb.data.split(":")
    aid = int(aid)
    a = app_get(aid)
    if not a or a["status"] != "pending":
        await cb.answer("Заявка уже обработана или не найдена.", show_alert=True)
        return
    approved = action == "appok"
    app_set_status(aid, "approved" if approved else "rejected")
    kind_title = APP_KIND_TITLES.get(a["kind"], a["kind"])
    if approved:
        text = f"✅ Ваша заявка ({esc(kind_title)}) одобрена!"
        u = get_user(a["user_id"])
        if u and not u["rank"] and a["kind"] in KIND_TITLES:
            set_rank(a["user_id"], a["kind"], 1)
            text += f"\nВам назначена роль: <b>{esc(KIND_TITLES[a['kind']])}</b>. Профиль и отчёты — кнопка «👤 Профиль»."
    else:
        text = f"❌ К сожалению, ваша заявка ({esc(kind_title)}) отклонена."
    await safe_send(cb.bot, a["user_id"], text)
    track_event(a["user_id"], f"application_{'approved' if approved else 'rejected'}")
    card, markup = app_card(aid)
    await show(cb, card, markup)
    await cb.answer("Одобрено ✅" if approved else "Отклонено ❌")


def track_event(user_id: int, event: str):
    with db() as con:
        con.execute("INSERT INTO events(user_id,event,created_at) VALUES(?,?,?)", (user_id, event, utcnow()))


# ---------- статистика ----------
@admin_router.callback_query(F.data == "adm:stats")
async def adm_stats(cb: CallbackQuery):
    s = global_stats()
    text = "\n".join([
        "📊 <b>Статистика</b>", "",
        f"👤 Пользователей: <b>{s['users']}</b> (сегодня {s['today']} · 7 дн. {s['week']} · 30 дн. {s['month']})",
        f"📥 Заявок: <b>{s['apps']}</b> (ожидают {s['pending']} · одобрено {s['approved']} · отклонено {s['rejected']})",
        f"   операторов {s['operators']} · скаутов {s['scouts']}",
        f"📸 Интерес к модели: <b>{s['models']}</b>", "",
        f"👥 Команд: <b>{s['teams']}</b> · людей в командах: <b>{s['members']}</b>",
        f"📝 Отчётов сегодня: <b>{s['reports_today']}</b> · на проверке: <b>{s['reports_pending']}</b>",
        f"💬 Чатов с напоминаниями: {s['chats']} · 🚫 блокировок: {s['bans']}",
    ])
    await show(cb, text, kb([[btn("🔄 Обновить", "adm:stats"), btn("⬅️ Назад", "adm:home")]]))
    await cb.answer()


# ---------- рассылка ----------
@admin_router.callback_query(F.data == "adm:bc")
async def adm_bc(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = [[btn(f"{title} ({len(recipients(seg))})", f"adm:bcs:{seg}")] for seg, title in BC_SEGMENTS.items()]
    rows.append([btn("⬅️ Назад", "adm:home")])
    await show(cb, "📨 <b>Рассылка</b>\nВыберите аудиторию (заблокировавшие бота исключены):", kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data.regexp(r"^adm:bcs:(all|models|scouts|operators|members)$"))
async def adm_bc_seg(cb: CallbackQuery, state: FSMContext):
    seg = cb.data.split(":")[2]
    await state.set_state(AdminFSM.bc_text)
    await state.update_data(seg=seg)
    await cb.message.answer(f"Аудитория: <b>{BC_SEGMENTS[seg]}</b> ({len(recipients(seg))}).\n"
                            f"Отправьте текст рассылки — жирный, курсив и ссылки сохранятся. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.bc_text, F.text)
async def adm_bc_text(message: Message, state: FSMContext):
    text = message.html_text
    if len(text) > 4000:
        await message.answer("Слишком длинно (максимум ~4000 символов). Сократите текст.")
        return
    seg = (await state.get_data())["seg"]
    await state.update_data(text=text)
    await state.set_state(AdminFSM.bc_confirm)
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.answer(text)
    await message.answer(f"Отправить <b>{len(recipients(seg))}</b> получателям ({BC_SEGMENTS[seg]})?",
                         reply_markup=kb([[btn("✅ Отправить", "adm:bcgo")], [btn("❌ Отмена", "adm:home")]]))


@admin_router.callback_query(AdminFSM.bc_confirm, F.data == "adm:bcgo")
async def adm_bc_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    ids = recipients(data["seg"])
    status = await cb.message.edit_text(f"📨 Отправка… 0/{len(ids)}")
    ok = blocked = failed = 0
    for i, uid in enumerate(ids, 1):
        res = await deliver(cb.bot, uid, data["text"])
        if res == "ok":
            ok += 1
        elif res == "blocked":
            blocked += 1
            mark_blocked_user(uid)
        else:
            failed += 1
        if i % 100 == 0:
            try:
                await status.edit_text(f"📨 Отправка… {i}/{len(ids)}")
            except TelegramBadRequest:
                pass
        await asyncio.sleep(0.05)
    await cb.message.answer(f"✅ Рассылка завершена.\nДоставлено: {ok}\nЗаблокировали бота: {blocked}\nОшибок: {failed}",
                            reply_markup=kb([[btn("🛠 В админ-панель", "adm:home")]]))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:bcgo")
async def adm_bc_go_expired(cb: CallbackQuery):
    await cb.answer("Сессия рассылки истекла — начните заново.", show_alert=True)


# ---------- сообщение в группу ----------
@admin_router.callback_query(F.data.regexp(r"^adm:grp:(model|scout)$"))
async def adm_grp(cb: CallbackQuery, state: FSMContext):
    seg = cb.data.split(":")[2]
    if not group_id(seg):
        await cb.answer("ID группы не задан в .env (MODEL_GROUP_ID / SCOUT_GROUP_ID).", show_alert=True)
        return
    await state.set_state(AdminFSM.grp_text)
    await state.update_data(seg=seg)
    title = "моделей" if seg == "model" else "скаутов"
    await cb.message.answer(f"Отправьте текст сообщения в группу {title}. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.grp_text, F.text)
async def adm_grp_text(message: Message, state: FSMContext):
    text = message.html_text
    if len(text) > 4000:
        await message.answer("Слишком длинно (максимум ~4000 символов).")
        return
    seg = (await state.get_data())["seg"]
    await state.update_data(text=text)
    await state.set_state(AdminFSM.grp_confirm)
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.answer(text)
    await message.answer(f"Отправить в группу {'моделей' if seg == 'model' else 'скаутов'}?",
                         reply_markup=kb([[btn("✅ Отправить", "adm:grpgo")], [btn("❌ Отмена", "adm:home")]]))


@admin_router.callback_query(AdminFSM.grp_confirm, F.data == "adm:grpgo")
async def adm_grp_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    res = await deliver(cb.bot, group_id(data["seg"]), data["text"])
    msg = "✅ Отправлено в группу." if res == "ok" else f"⚠️ Не удалось отправить: {esc(res)}\nПроверьте, что бот в группе и ID верный."
    await cb.message.edit_text(msg, reply_markup=kb([[btn("🛠 В админ-панель", "adm:home")]]))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:grpgo")
async def adm_grp_go_expired(cb: CallbackQuery):
    await cb.answer("Сессия истекла — начните заново.", show_alert=True)


# ---------- отметить новую модель ----------
@admin_router.callback_query(F.data == "adm:newmodel")
async def adm_newmodel(cb: CallbackQuery, state: FSMContext):
    await state.set_state(AdminFSM.newmodel)
    await cb.message.answer("Отправьте @username или ID человека, которого нужно отметить как новую модель. /cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.newmodel, F.text)
async def adm_newmodel_input(message: Message, state: FSMContext):
    u = find_user(message.text)
    if not u:
        await message.answer("Не нашёл (человек должен был запускать бота). Ещё раз или /cancel.")
        return
    with db() as con:
        con.execute("UPDATE users SET model_interest=1 WHERE user_id=?", (u["user_id"],))
    if not u["rank"]:
        set_rank(u["user_id"], "model", 1)
    track_event(u["user_id"], "marked_model")
    await state.clear()
    await safe_send(message.bot, u["user_id"], "📸 Вас отметили как новую модель. Добро пожаловать!")
    await message.answer(f"✅ {who(u)} отмечен(а) как модель.",
                         reply_markup=kb([[btn("🙍 Профиль", f"adm:u:{u['user_id']}"), btn("🛠 Меню", "adm:home")]]))


# ---------- CSV ----------
@admin_router.callback_query(F.data == "adm:csv")
async def adm_csv(cb: CallbackQuery):
    data = export_users_csv()
    name = f"users_{msk_now().strftime('%Y%m%d_%H%M')}.csv"
    await cb.message.answer_document(BufferedInputFile(data, filename=name), caption="📄 Выгрузка пользователей")
    await cb.answer()


# ======================= ПРОВЕРКА ОТЧЁТОВ =======================
REPORT_STATUS = {"pending": "⏳ на проверке", "approved": "✅ подтверждён", "rejected": "❌ отклонён"}


def report_by_id(rid):
    with db() as con:
        return con.execute("""
            SELECT r.*, u.username, u.first_name, u.rank, u.level FROM reports r
            LEFT JOIN users u ON u.user_id=r.user_id WHERE r.id=?""", (rid,)).fetchone()


def pending_reports(limit: int = 20):
    with db() as con:
        return con.execute("""
            SELECT r.*, u.username, u.first_name, u.rank, u.level FROM reports r
            LEFT JOIN users u ON u.user_id=r.user_id
            WHERE r.status='pending' ORDER BY r.id ASC LIMIT ?""", (limit,)).fetchall()


def count_pending_reports() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) n FROM reports WHERE status='pending'").fetchone()["n"]


def contact_url(row) -> str:
    uname = row["username"]
    return f"https://t.me/{uname}" if uname else f"tg://user?id={row['user_id']}"


def review_rows(r, with_url: bool = True):
    """Кнопки проверки отчёта: подтвердить / отклонить / связаться."""
    rid, uid = r["id"], r["user_id"]
    decide = []
    if r["status"] != "approved":
        decide.append(btn("✅ Подтвердить", f"adm:rv:ok:{rid}"))
    if r["status"] != "rejected":
        decide.append(btn("❌ Отклонить", f"adm:rv:no:{rid}"))
    contact = [btn("✉️ Написать через бота", f"note:{uid}")]
    if with_url:
        contact.append(InlineKeyboardButton(text="💬 Открыть чат", url=contact_url(r)))
    rows = [decide, contact]
    if r["models"] > 0:
        rows.append([btn("🎉 Разослать модель", f"adm:nm:{uid}")])
    return [x for x in rows if x]


def review_kb(r, with_url: bool = True):
    return kb(review_rows(r, with_url) + [[btn("🧾 Очередь", "adm:reports"), btn("🛠 Меню", "adm:home")]])


async def review_report(bot: Bot, rid: int, admin_id: int, approve: bool, note: str = ""):
    """Подтверждает/отклоняет отчёт и уведомляет автора. Возвращает (ok, сообщение)."""
    r = report_by_id(rid)
    if not r:
        return False, "Отчёт не найден."
    target = "approved" if approve else "rejected"
    if r["status"] == target:
        return False, f"Уже {REPORT_STATUS[target]}."
    note = (note or "").strip()[:500]
    with db() as con:
        con.execute("UPDATE reports SET status=?, reviewed_by=?, reviewed_at=?, review_note=? WHERE id=?",
                    (target, admin_id, utcnow(), note if not approve else "", rid))
    date = fmt_date(r["report_date"])
    if approve:
        text = f"✅ Ваш отчёт за {date} подтверждён."
    else:
        text = f"❌ Ваш отчёт за {date} отклонён."
        if note:
            text += f"\nПричина: {esc(note)}"
        text += "\nИсправьте и отправьте заново — /report."
    await safe_send(bot, r["user_id"], text)
    return True, "Подтверждён ✅" if approve else "Отклонён ❌"


async def notify_admins_report(bot: Bot, rid: int):
    r = report_by_id(rid)
    if not r:
        return
    text = "🧾 <b>Новый отчёт на проверку</b>\n\n" + report_text(r)
    for a in ADMIN_IDS:
        if a == r["user_id"]:
            continue
        # если Telegram не принял url-кнопку (приватность профиля) — шлём без неё
        if not await safe_send(bot, a, text, reply_markup=review_kb(r)):
            await safe_send(bot, a, text, reply_markup=review_kb(r, with_url=False))
        await asyncio.sleep(0.05)


@admin_router.callback_query(F.data == "adm:reports")
async def adm_reports(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows_db = pending_reports()
    total = count_pending_reports()
    rows = []
    for r in rows_db:
        name = f"@{r['username']}" if r["username"] else (r["first_name"] or r["user_id"])
        rows.append([btn(f"#{r['id']} · {name} · {fmt_date(r['report_date'])} · {r['hours']:g} ч"[:60],
                         f"adm:rv:view:{r['id']}")])
    rows.append([btn("🔄 Обновить", "adm:reports"), btn("⬅️ Назад", "adm:home")])
    text = f"🧾 <b>Отчёты на проверку</b> ({total})"
    if not rows_db:
        text += "\n\nОчередь пуста 🎉"
    elif total > len(rows_db):
        text += f"\nПоказаны первые {len(rows_db)}."
    await show(cb, text, kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data.regexp(r"^adm:rv:(ok|no|view):\d+$"))
async def adm_review(cb: CallbackQuery, state: FSMContext):
    _, _, act, rid = cb.data.split(":")
    rid = int(rid)
    if not report_by_id(rid):
        await cb.answer("Отчёт не найден.", show_alert=True)
        return
    if act == "no":
        await state.set_state(AdminFSM.reject_reason)
        await state.update_data(rid=rid)
        await cb.message.answer("Укажите причину отклонения — её увидит автор отчёта.\n"
                                "«-» — отклонить без причины. /cancel — отмена.")
        await cb.answer()
        return
    if act == "ok":
        ok, msg = await review_report(cb.bot, rid, cb.from_user.id, True)
        if not ok:
            await cb.answer(msg, show_alert=True)
            r = report_by_id(rid)
            await show(cb, report_text(r), review_kb(r))
            return
        await cb.answer(msg)
    else:
        await cb.answer()
    r = report_by_id(rid)
    await show(cb, report_text(r), review_kb(r))


@admin_router.message(AdminFSM.reject_reason, F.text)
async def adm_reject_reason(message: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    note = "" if message.text.strip() == "-" else message.text.strip()
    await state.clear()
    ok, msg = await review_report(message.bot, rid, message.from_user.id, False, note)
    r = report_by_id(rid)
    await message.answer(f"{msg}\n\n" + (report_text(r) if r else ""),
                         reply_markup=review_kb(r) if r else None)


# ======================= «У @user НОВАЯ МОДЕЛЬ» =======================
def mention_html(u) -> str:
    if u["username"]:
        return esc(f"@{u['username']}")
    return f'<a href="tg://user?id={u["user_id"]}">{esc(u["first_name"] or "участник")}</a>'


def model_alert_text(u, comment: str = "") -> str:
    text = f"🎉 У {mention_html(u)} новая модель!"
    if comment:
        text += f"\n{esc(comment)}"
    return text


def model_alert_recipients(scout_id: int):
    """Участники команд + лидеры + сам скаут (кроме заблокировавших бота)."""
    ids = set(recipients("members")) | {scout_id}
    with db() as con:
        ids |= {r["leader_id"] for r in con.execute("SELECT DISTINCT leader_id FROM teams")}
        if not ids:
            return []
        q = ",".join("?" * len(ids))
        rows = con.execute(f"SELECT user_id FROM users WHERE is_blocked=0 AND user_id IN ({q})", tuple(ids)).fetchall()
    return [r["user_id"] for r in rows]


async def send_model_alert(bot: Bot, scout_id: int, comment: str, mode: str, by: int) -> dict:
    """mode: all | group | bot. Возвращает отчёт о доставке."""
    u = get_user(scout_id)
    if not u:
        return {"error": "Пользователь не найден"}
    text = model_alert_text(u, comment)
    res = {"group": "skip", "sent": 0, "total": 0, "blocked": 0}
    if mode in ("all", "group"):
        if ALERT_GROUP_ID:
            r = await deliver(bot, ALERT_GROUP_ID, text)
            res["group"] = "ok" if r == "ok" else r
        else:
            res["group"] = "not_configured"
    if mode in ("all", "bot"):
        ids = model_alert_recipients(scout_id)
        res["total"] = len(ids)
        for uid in ids:
            r = await deliver(bot, uid, text)
            if r == "ok":
                res["sent"] += 1
            elif r == "blocked":
                res["blocked"] += 1
                mark_blocked_user(uid)
            await asyncio.sleep(0.05)
    with db() as con:
        con.execute("INSERT INTO model_alerts(scout_id,comment,created_by,created_at) VALUES(?,?,?,?)",
                    (scout_id, comment, by, utcnow()))
    track_event(scout_id, "model_alert")
    return res


def alert_result_text(res: dict) -> str:
    if "error" in res:
        return "⚠️ " + res["error"]
    L = ["✅ Оповещение отправлено."]
    g = res["group"]
    if g == "ok":
        L.append("📣 Группа: доставлено")
    elif g == "not_configured":
        L.append("📣 Группа: не задана (ALERT_GROUP_ID / SCOUT_GROUP_ID / MODEL_GROUP_ID в .env)")
    elif g != "skip":
        L.append(f"📣 Группа: ошибка — {esc(g)}\nПроверьте, что бот в группе и ID верный.")
    if res["total"]:
        L.append(f"🤖 В боте: {res['sent']} из {res['total']} (заблокировали бота: {res['blocked']})")
    return "\n".join(L)


@admin_router.callback_query(F.data.regexp(r"^adm:nm(:\d+)?$"))
async def adm_nm(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    await state.clear()
    await state.set_state(AdminFSM.nm_user)
    if len(parts) == 3:
        u = get_user(int(parts[2]))
        if not u:
            await state.clear()
            await cb.answer("Пользователь не найден.", show_alert=True)
            return
        await state.update_data(uid=u["user_id"])
        await cb.message.answer(f"🎉 Новая модель у {who(u)}.\nОтправьте комментарий (например, имя модели) "
                                f"или «-» без комментария. /cancel — отмена.")
    else:
        await cb.message.answer("🎉 <b>Разослать модель</b>\nОтправьте @username или ID того, у кого новая модель. "
                                "Комментарий — через пробел:\n<code>@user Анна, 22 года</code>\n/cancel — отмена.")
    await cb.answer()


@admin_router.message(AdminFSM.nm_user, F.text)
async def adm_nm_input(message: Message, state: FSMContext):
    data = await state.get_data()
    text = message.text.strip()
    if "uid" in data:
        u = get_user(data["uid"])
        comment = "" if text == "-" else text
    else:
        parts = text.split(maxsplit=1)
        u = find_user(parts[0]) if parts else None
        comment = parts[1] if len(parts) > 1 else ""
    if not u:
        await message.answer("Не нашёл (человек должен был запускать бота). Ещё раз или /cancel.")
        return
    comment = comment[:200]
    await state.update_data(uid=u["user_id"], comment=comment)
    await state.set_state(AdminFSM.nm_confirm)
    n_bot = len(model_alert_recipients(u["user_id"]))
    group = "настроена" if ALERT_GROUP_ID else "не задана"
    await message.answer("👀 <b>Предпросмотр:</b>")
    await message.answer(model_alert_text(u, comment))
    await message.answer(f"Куда отправить? Группа: {group} · в боте получат: {n_bot}",
                         reply_markup=kb([
                             [btn("✅ В группу и в бота", "adm:nmgo:all")],
                             [btn("📣 Только в группу", "adm:nmgo:group"), btn("🤖 Только в бота", "adm:nmgo:bot")],
                             [btn("❌ Отмена", "adm:home")]]))


@admin_router.callback_query(AdminFSM.nm_confirm, F.data.regexp(r"^adm:nmgo:(all|group|bot)$"))
async def adm_nm_go(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.clear()
    await cb.answer("Отправляю…")
    res = await send_model_alert(cb.bot, data["uid"], data.get("comment", ""), cb.data.split(":")[2], cb.from_user.id)
    await cb.message.answer(alert_result_text(res), reply_markup=kb([[btn("🛠 В админ-панель", "adm:home")]]))


@admin_router.callback_query(F.data.regexp(r"^adm:nmgo:"))
async def adm_nm_go_expired(cb: CallbackQuery):
    await cb.answer("Сессия истекла — начните заново.", show_alert=True)


# ======================= РЕЙТИНГ (/top) =======================
MEDALS = ["🥇", "🥈", "🥉"]


def top_people(since: str, limit: int = 10):
    with db() as con:
        return con.execute("""
            SELECT u.user_id, u.username, u.first_name, u.rank, u.level,
                   SUM(r.scouts + r.models + r.operators) AS brought,
                   SUM(r.hours) AS h, COUNT(*) AS days
            FROM reports r JOIN users u ON u.user_id = r.user_id
            WHERE r.report_date >= ? AND r.status <> 'rejected'
            GROUP BY u.user_id
            HAVING brought > 0 OR h > 0
            ORDER BY brought DESC, h DESC LIMIT ?""", (since, limit)).fetchall()


def pub_name(row) -> str:
    """Имя для общего рейтинга — без ID."""
    return esc(f"@{row['username']}" if row["username"] else (row["first_name"] or "участник"))


def top_text(show_ids: bool) -> str:
    s7 = since7()
    L = ["🏆 <b>Рейтинг за 7 дней</b>", "", "<b>Люди</b> (приведено · часов)"]
    people = top_people(s7)
    for i, p in enumerate(people):
        mark = MEDALS[i] if i < 3 else f"{i + 1}."
        name = who(p) if show_ids else pub_name(p)
        L.append(f"{mark} {name} — <b>{p['brought']}</b> · {p['h']:g} ч")
    if not people:
        L.append("Пока нет отчётов.")
    L += ["", "<b>Команды</b> (приведено людей)"]
    teams = sorted(teams_with_stats(), key=lambda x: -x["brought7"])[:5]
    for i, t in enumerate(teams):
        mark = MEDALS[i] if i < 3 else f"{i + 1}."
        L.append(f"{mark} «{esc(t['name'])}» — <b>{t['brought7']}</b> · {t['members']} чел.")
    if not teams:
        L.append("Команд пока нет.")
    return "\n".join(L)[:4000]


@router.message(Command("top"))
async def cmd_top(message: Message):
    touch_user(message.from_user)
    u = get_user(message.from_user.id)
    if not is_admin(message.from_user.id) and not (u and u["rank"]):
        await message.answer("Рейтинг доступен участникам команд.", reply_markup=main_menu_kb())
        return
    await message.answer(top_text(is_admin(message.from_user.id)), reply_markup=main_menu_kb())


@admin_router.callback_query(F.data == "adm:top")
async def adm_top(cb: CallbackQuery):
    await show(cb, top_text(True), kb([[btn("🔄 Обновить", "adm:top"), btn("⬅️ Назад", "adm:home")]]))
    await cb.answer()


# ======================= КТО НЕ СДАЛ ОТЧЁТ =======================
def users_without_report_today():
    """Участники команд и лидеры с рангом, не сдавшие отчёт за сегодня (МСК)."""
    today = msk_now().date().isoformat()
    with db() as con:
        return con.execute("""
            SELECT u.* FROM users u
            WHERE u.rank IS NOT NULL AND u.is_blocked = 0
              AND (u.user_id IN (SELECT user_id FROM team_members)
                   OR u.user_id IN (SELECT leader_id FROM teams))
              AND NOT EXISTS (SELECT 1 FROM reports r WHERE r.user_id = u.user_id AND r.report_date = ?
                              AND r.status <> 'rejected')
            ORDER BY u.rank, u.username""", (today,)).fetchall()


@admin_router.callback_query(F.data == "adm:noreport")
async def adm_noreport(cb: CallbackQuery):
    rows = users_without_report_today()
    L = [f"📝 <b>Не сдали отчёт сегодня</b> ({len(rows)})", ""]
    for u in rows[:40]:
        L.append(f"• {who(u)} · {esc(rank_label(u['rank'], u['level']))}")
    if len(rows) > 40:
        L.append(f"… и ещё {len(rows) - 40}")
    if not rows:
        L.append("Все сдали 🎉")
    buttons = []
    if rows:
        buttons.append([btn(f"🔔 Напомнить всем ({len(rows)})", "adm:noreportgo")])
    buttons.append([btn("🔄 Обновить", "adm:noreport"), btn("⬅️ Назад", "adm:home")])
    await show(cb, "\n".join(L)[:4000], kb(buttons))
    await cb.answer()


@admin_router.callback_query(F.data == "adm:noreportgo")
async def adm_noreport_go(cb: CallbackQuery):
    rows = users_without_report_today()
    markup = None
    if _BOT_USERNAME:
        markup = kb([[InlineKeyboardButton(text="📝 Заполнить отчёт", url=f"https://t.me/{_BOT_USERNAME}?start=report")]])
    await cb.answer(f"Отправляю {len(rows)} напоминаний…")
    sent = 0
    for u in rows:
        if await safe_send(cb.bot, u["user_id"], "🔔 Вы ещё не сдали отчёт за сегодня. " + REMINDER_TEXT + ".",
                           reply_markup=markup):
            sent += 1
        await asyncio.sleep(0.05)
    await cb.message.answer(f"✅ Напоминания доставлены: {sent} из {len(rows)}.",
                            reply_markup=kb([[btn("🛠 В админ-панель", "adm:home")]]))


# ======================= ДОБАВИТЬ РАБОТНИКА =======================
# Скауты и тимлиды добавляют скаутов / операторов / моделей в свою команду. Если человек ещё не запускал бота,
# сохраняется приглашение: он попадёт в команду сам — по личной ссылке или как только запустит бота с этим @username.
ADDER_RANKS = {"scout"} | LEADER_RANKS
REQUESTER_RANKS = {"scout"} | LEADER_RANKS
KIND_ICONS = {"scout": "🔎", "operator": "🎧", "model": "📸"}
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
MAX_OPEN_INVITES = 30
TEAM_NAME_MIN, TEAM_NAME_MAX = 2, 40


def _team_dict(t) -> dict:
    return {"id": t["id"], "name": t["name"], "leader_id": t["leader_id"]}


def addable_teams(uid: int) -> list:
    """Команды, в которые uid может добавлять людей: админ — любые, скаут/тимлид — свои (где лидер или участник)."""
    if is_admin(uid):
        with db() as con:
            return [dict(r) for r in con.execute("SELECT id,name,leader_id FROM teams ORDER BY name LIMIT 100")]
    u = get_user(uid)
    if not u or u["rank"] not in ADDER_RANKS:
        return []
    out = [_team_dict(t) for t in teams_led_by(uid)]
    mem = member_team(uid)
    if mem and all(t["id"] != mem["id"] for t in out):
        out.append(_team_dict(mem))
    return out


def parse_ident(text: str):
    """@username, t.me-ссылка или числовой ID -> ('id', int) | ('username', str) | None."""
    t = (text or "").strip()
    low = t.lower()
    for p in ("https://t.me/", "http://t.me/", "https://telegram.me/", "t.me/"):
        if low.startswith(p):
            t = t[len(p):]
            break
    t = t.split("?")[0].strip().strip("/").lstrip("@")
    if t.isdigit() and len(t) <= 15:
        return "id", int(t)
    if USERNAME_RE.match(t):
        return "username", t.lower()
    return None


def invite_link(token: str) -> str:
    return f"https://t.me/{_BOT_USERNAME}?start=inv_{token}"


def pending_invites(team_ids) -> list:
    ids = list(team_ids)
    if not ids:
        return []
    with db() as con:
        rows = con.execute(f"""
            SELECT * FROM invites WHERE claimed_by IS NULL AND team_id IN ({','.join('?' * len(ids))})
            ORDER BY id DESC LIMIT 100""", ids).fetchall()
    return [dict(r) for r in rows]


def _err(text: str, retry: bool = False) -> dict:
    return {"ok": False, "status": "error", "retry": retry, "text": text, "link": None}


async def add_worker(bot: Bot, by_id: int, team_id: int, kind: str, ident_text: str) -> dict:
    """Общая логика бота и мини-приложения.
    Возвращает {ok, status: added|invited|error, retry, text (HTML), link}. retry=True — стоит спросить ввод ещё раз."""
    if kind not in KIND_TITLES:
        return _err("Неизвестная роль.")
    team = next((t for t in addable_teams(by_id) if t["id"] == team_id), None)
    if not team:
        return _err("Нет прав добавлять людей в эту команду.")
    parsed = parse_ident(ident_text)
    if not parsed:
        return _err("Не похоже на @username или ID. Пример: <code>@ivan_petrov</code> или <code>123456789</code>.",
                    retry=True)
    adder = get_user(by_id)
    target = get_user(parsed[1]) if parsed[0] == "id" else find_user(parsed[1])
    tname = esc(team["name"])

    if target:
        tid = target["user_id"]
        if tid == by_id:
            return _err("Себя добавлять не нужно.", retry=True)
        if is_admin(tid):
            return _err("Администратора в команду добавить нельзя.")
        if is_banned(tid, target["username"]):
            return _err("Доступ этого пользователя к боту ограничен.")
        if teams_led_by(tid) or (target["rank"] in LEADER_RANKS and not is_admin(by_id)):
            return _err(f"{who(target)} — тимлид, его добавляет администратор.")
        cur = member_team(tid)
        if cur and cur["id"] == team_id:
            return _err(f"{who(target)} уже в команде «{tname}».")
        if cur:
            return _err(f"{who(target)} уже состоит в команде «{esc(cur['name'])}». "
                        f"Перевод — через наставника или администратора.")
        had_rank = bool(target["rank"])
        if not had_rank:
            set_rank(tid, kind, 1)
        add_member(tid, team_id, via_ref=0, added_by=by_id)
        row = get_user(tid)
        label = esc(rank_label(row["rank"], row["level"]))
        await safe_send(bot, tid, f"✅ {who(adder, by_id)} добавил(а) вас в команду «{tname}». "
                                  f"Ваш ранг: <b>{label}</b>.\nПрофиль и отчёты — кнопка «👤 Профиль».",
                        reply_markup=main_menu_kb())
        if team["leader_id"] not in (by_id, tid):
            await safe_send(bot, team["leader_id"],
                            f"➕ {who(adder, by_id)} добавил(а) в вашу команду «{tname}»: {who(row)} · {label}.")
        text = f"✅ {who(row)} добавлен(а) в команду «{tname}» · <b>{label}</b>."
        if had_rank and row["rank"] != kind:
            text += f"\nУ человека уже был ранг «{label}» — он сохранён."
        return {"ok": True, "status": "added", "retry": False, "text": text, "link": None,
                "user_id": tid, "rank": row["rank"]}

    # человека нет в базе — сохраняем приглашение
    uname = parsed[1] if parsed[0] == "username" else ""
    with db() as con:
        open_n = con.execute("SELECT COUNT(*) n FROM invites WHERE added_by=? AND claimed_by IS NULL",
                             (by_id,)).fetchone()["n"]
        ex = con.execute("SELECT * FROM invites WHERE username=? AND team_id=? AND claimed_by IS NULL",
                         (uname, team_id)).fetchone() if uname else None
        if ex:
            token = ex["token"]
            con.execute("UPDATE invites SET kind=? WHERE id=?", (kind, ex["id"]))
        elif open_n >= MAX_OPEN_INVITES:
            return _err(f"Слишком много неиспользованных приглашений ({MAX_OPEN_INVITES}). "
                        f"Дождитесь, пока люди запустят бота.")
        else:
            token = secrets.token_hex(6)
            con.execute("""INSERT INTO invites(token,username,kind,team_id,added_by,created_at)
                           VALUES(?,?,?,?,?,?)""", (token, uname, kind, team_id, by_id, utcnow()))
    link = invite_link(token)
    who_txt = f"@{uname}" if uname else f"ID {parsed[1]}"
    text = (f"📨 {esc(who_txt)} ещё не запускал(а) бота — приглашение сохранено.\n"
            f"Роль: <b>{KIND_TITLES[kind]}</b> · команда «{tname}».\n\n")
    if uname:
        text += "Как только человек запустит бота, он сам окажется в команде. "
    text += f"Можно отправить ему личную ссылку (одноразовая):\n<code>{link}</code>"
    return {"ok": True, "status": "invited", "retry": False, "text": text, "link": link}


async def _apply_invite(bot: Bot, inv, user, reply_to: Message, quiet: bool = False) -> bool:
    async def say(text):
        if not quiet:
            await reply_to.answer(text)

    team = team_by_id(inv["team_id"])
    if not team:
        await say("Приглашение недействительно: команда расформирована.")
        return False
    if is_admin(user.id) or user.id == team["leader_id"]:
        await say("Это приглашение для нового участника — вам оно не нужно.")
        return False
    cur = member_team(user.id)
    if cur:
        await say(f"Вы уже в команде «{esc(cur['name'])}»." if cur["id"] == team["id"] else
                  f"Вы уже состоите в команде «{esc(cur['name'])}», приглашение не применено. "
                  f"Чтобы сменить команду, напишите наставнику.")
        return False
    with db() as con:
        got = con.execute("UPDATE invites SET claimed_by=?, claimed_at=? WHERE id=? AND claimed_by IS NULL",
                          (user.id, utcnow(), inv["id"])).rowcount
    if not got:
        await say("Это приглашение уже использовано.")
        return False
    touch_user(user)
    row = get_user(user.id)
    if not row["rank"]:
        set_rank(user.id, inv["kind"], 1)
    add_member(user.id, team["id"], via_ref=0, added_by=inv["added_by"])
    row = get_user(user.id)
    label = esc(rank_label(row["rank"], row["level"]))
    await reply_to.answer(f"✅ Вы в команде «{esc(team['name'])}». Ваш ранг: <b>{label}</b>.\n"
                          f"Профиль и отчёты — кнопка «👤 Профиль» или /profile.", reply_markup=main_menu_kb())
    await safe_send(bot, inv["added_by"], f"✅ По вашему приглашению в команду «{esc(team['name'])}» "
                                          f"вступил(а) {who(row)} · {label}.")
    if team["leader_id"] != inv["added_by"]:
        await safe_send(bot, team["leader_id"], f"➕ В вашу команду «{esc(team['name'])}» вступил(а) "
                                                f"{who(row)} · {label}.")
    return True


async def redeem_invite(message: Message, token: str, user):
    with db() as con:
        inv = con.execute("SELECT * FROM invites WHERE token=?", (token,)).fetchone()
    if not inv:
        await message.answer("Ссылка-приглашение недействительна.")
        return
    if inv["claimed_by"]:
        await message.answer("Вы уже использовали это приглашение." if inv["claimed_by"] == user.id
                             else "Это приглашение уже использовано.")
        return
    touch_user(user)
    await _apply_invite(message.bot, inv, user, message)


async def claim_username_invites(bot: Bot, user, reply_to: Message) -> bool:
    """Вызывать после /start: если на этот @username есть приглашение — человек попадает в команду."""
    uname = (user.username or "").lower()
    if not uname or is_admin(user.id):
        return False
    with db() as con:
        inv = con.execute("SELECT * FROM invites WHERE username=? AND claimed_by IS NULL ORDER BY id LIMIT 1",
                          (uname,)).fetchone()
    if not inv:
        return False
    return await _apply_invite(bot, inv, user, reply_to, quiet=True)


# ---------- бот: диалог «Добавить работника» ----------
def _kind_picker(team: dict):
    tid = team["id"]
    return (f"➕ <b>Добавить работника</b>\nКоманда: «{esc(team['name'])}»\n\nКого добавляем?",
            kb([[btn(f"{KIND_ICONS[k]} {KIND_TITLES[k]}", f"aw:k:{tid}:{k}") for k in KIND_TITLES],
                [btn("⬅️ Отмена", "aw:x")]]))


async def aw_begin(target, user, state: FSMContext):
    await state.clear()
    touch_user(user)
    uid = user.id
    u = get_user(uid)
    if not is_admin(uid) and (not u or u["rank"] not in ADDER_RANKS):
        await show(target, "Добавлять работников могут скауты и тимлиды.", kb([[btn("👤 Профиль", "prof:me")]]))
        return
    teams = addable_teams(uid)
    if not teams:
        rows = [] if is_admin(uid) else [[btn("🙋 Запросить команду", "tr:start")]]
        rows.append([btn("👤 Профиль", "prof:me")])
        await show(target, "У вас пока нет команды, поэтому добавлять людей некуда."
                           + ("" if is_admin(uid) else "\nЗапросите создание команды — администратор рассмотрит заявку."),
                   kb(rows))
    elif len(teams) == 1:
        text, markup = _kind_picker(teams[0])
        await show(target, text, markup)
    else:
        rows = [[btn(t["name"][:50], f"aw:t:{t['id']}")] for t in teams[:40]]
        rows.append([btn("⬅️ Отмена", "aw:x")])
        await show(target, "➕ <b>Добавить работника</b>\nВ какую команду?", kb(rows))


@router.message(Command("addworker"))
async def cmd_addworker(message: Message, state: FSMContext):
    await aw_begin(message, message.from_user, state)


@router.callback_query(F.data == "aw:start")
async def cb_aw_start(cb: CallbackQuery, state: FSMContext):
    await aw_begin(cb, cb.from_user, state)
    await cb.answer()


@router.callback_query(F.data == "aw:x")
async def cb_aw_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_profile(cb, cb.from_user.id, cb.from_user.id)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^aw:t:\d+$"))
async def cb_aw_team(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    tid = int(cb.data.split(":")[2])
    team = next((t for t in addable_teams(cb.from_user.id) if t["id"] == tid), None)
    if not team:
        await cb.answer("Нет прав на эту команду.", show_alert=True)
        return
    text, markup = _kind_picker(team)
    await show(cb, text, markup)
    await cb.answer()


@router.callback_query(F.data.regexp(r"^aw:k:\d+:(scout|operator|model)$"))
async def cb_aw_kind(cb: CallbackQuery, state: FSMContext):
    _, _, tid, kind = cb.data.split(":")
    tid = int(tid)
    team = next((t for t in addable_teams(cb.from_user.id) if t["id"] == tid), None)
    if not team:
        await cb.answer("Нет прав на эту команду.", show_alert=True)
        return
    await state.set_state(WorkerFSM.ident)
    await state.update_data(tid=tid, kind=kind)
    await show(cb, f"➕ Добавляем: <b>{KIND_ICONS[kind]} {KIND_TITLES[kind]}</b> · команда «{esc(team['name'])}»\n\n"
                   f"Отправьте <b>@username</b> или ID человека.\n"
                   f"Если он ещё не запускал бота — сохраню приглашение и дам личную ссылку.\n/cancel — отмена.",
               kb([[btn("⬅️ Назад", f"aw:t:{tid}"), btn("❌ Отмена", "aw:x")]]))
    await cb.answer()


@router.message(WorkerFSM.ident, F.text, ~F.text.startswith("/"))
async def aw_ident(message: Message, state: FSMContext):
    d = await state.get_data()
    tid, kind = d.get("tid"), d.get("kind")
    if not tid or not kind:
        await state.clear()
        await message.answer("Сессия устарела — начните заново: /addworker")
        return
    res = await add_worker(message.bot, message.from_user.id, tid, kind, message.text)
    if res["retry"]:
        await message.answer(res["text"])
        return
    await state.clear()
    await message.answer(res["text"], reply_markup=kb([[btn("➕ Добавить ещё", f"aw:t:{tid}"),
                                                         btn("👤 Профиль", "prof:me")]]))


# ======================= ЗАПРОС НОВОЙ КОМАНДЫ =======================
TEAMREQ_STATUS = {"pending": "⏳ на рассмотрении", "approved": "✅ одобрена", "rejected": "❌ отклонена"}


def latest_team_request(uid: int):
    with db() as con:
        return con.execute("SELECT * FROM team_requests WHERE user_id=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()


def team_request_by_id(rid: int):
    with db() as con:
        return con.execute("""
            SELECT q.*, u.username, u.first_name, u.rank, u.level FROM team_requests q
            LEFT JOIN users u ON u.user_id=q.user_id WHERE q.id=?""", (rid,)).fetchone()


def pending_team_requests(limit: int = 30):
    with db() as con:
        return con.execute("""
            SELECT q.*, u.username, u.first_name, u.rank, u.level FROM team_requests q
            LEFT JOIN users u ON u.user_id=q.user_id
            WHERE q.status='pending' ORDER BY q.id ASC LIMIT ?""", (limit,)).fetchall()


def count_pending_team_requests() -> int:
    with db() as con:
        return con.execute("SELECT COUNT(*) n FROM team_requests WHERE status='pending'").fetchone()["n"]


def team_name_taken(name: str, exclude_id: int = 0) -> bool:
    # SQLite NOCASE не понимает кириллицу, поэтому регистр сравниваем в Python
    key = (name or "").strip().casefold()
    with db() as con:
        return any(r["name"].casefold() == key for r in con.execute("SELECT id,name FROM teams")
                   if r["id"] != exclude_id)


def clean_team_name(raw: str) -> str:
    return " ".join((raw or "").split())


def can_request_team(uid: int):
    """(можно, причина отказа)."""
    if is_admin(uid):
        return False, "Администратор создаёт команды в админ-панели."
    u = get_user(uid)
    if not u or u["rank"] not in REQUESTER_RANKS:
        return False, "Запросить команду могут скауты и тимлиды."
    last = latest_team_request(uid)
    if last and last["status"] == "pending":
        return False, f"Ваша заявка на команду «{last['name']}» уже на рассмотрении."
    return True, ""


def create_team_request(uid: int, name: str, comment: str):
    """-> (id | None, текст ошибки)."""
    ok, why = can_request_team(uid)
    if not ok:
        return None, why
    name = clean_team_name(name)
    if not TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX:
        return None, f"Название должно быть от {TEAM_NAME_MIN} до {TEAM_NAME_MAX} символов."
    if team_name_taken(name):
        return None, "Команда с таким названием уже есть — придумайте другое."
    with db() as con:
        cur = con.execute("INSERT INTO team_requests(user_id,name,comment,created_at) VALUES(?,?,?,?)",
                          (uid, name, (comment or "").strip()[:300], utcnow()))
        return cur.lastrowid, ""


def team_request_text(r) -> str:
    L = [f"🙋 <b>Запрос команды #{r['id']}</b>", f"От: {who(r)} · {esc(rank_label(r['rank'], r['level']))}",
         f"Название: <b>«{esc(r['name'])}»</b>"]
    if r["comment"]:
        L.append(f"Комментарий: {esc(r['comment'])}")
    mem = member_team(r["user_id"])
    if mem:
        L.append(f"Сейчас в команде: «{esc(mem['name'])}»")
    L += [f"Подана: {fmt_dt(r['created_at'])} МСК", f"Статус: {TEAMREQ_STATUS.get(r['status'], r['status'])}"]
    if r["status"] == "rejected" and r["review_note"]:
        L.append(f"Причина: {esc(r['review_note'])}")
    return "\n".join(L)


def team_request_kb(r):
    rows = []
    if r["status"] == "pending":
        rows.append([btn("✅ Создать команду", f"adm:rq:ok:{r['id']}"), btn("❌ Отклонить", f"adm:rq:no:{r['id']}")])
        rows.append([btn("✏️ Другое название и создать", f"adm:rq:ren:{r['id']}")])
    rows.append([btn("✉️ Написать через бота", f"note:{r['user_id']}")])
    rows.append([btn("🙋 Все запросы", "adm:rqs"), btn("🛠 Меню", "adm:home")])
    return kb(rows)


async def notify_admins_team_request(bot: Bot, rid: int):
    r = team_request_by_id(rid)
    if not r:
        return
    for a in ADMIN_IDS:
        await safe_send(bot, a, "🙋 <b>Новый запрос на создание команды</b>\n\n" + team_request_text(r),
                        reply_markup=team_request_kb(r))
        await asyncio.sleep(0.05)


async def decide_team_request(bot: Bot, rid: int, admin_id: int, approve: bool, name: str = "", note: str = ""):
    """Создаёт команду (или отклоняет заявку) и уведомляет автора. -> (ok, сообщение)."""
    r = team_request_by_id(rid)
    if not r:
        return False, "Заявка не найдена."
    if r["status"] != "pending":
        return False, f"Заявка уже {TEAMREQ_STATUS.get(r['status'], r['status'])}."
    uid = r["user_id"]
    if approve:
        final = clean_team_name(name) or r["name"]
        if not TEAM_NAME_MIN <= len(final) <= TEAM_NAME_MAX:
            return False, f"Название должно быть от {TEAM_NAME_MIN} до {TEAM_NAME_MAX} символов."
        tid = create_team(final, uid)
        if tid is None:
            return False, f"Название «{final}» уже занято — укажите другое."
        promoted = ensure_leader_rank(uid)
        with db() as con:
            con.execute("""UPDATE team_requests SET status='approved', reviewed_by=?, reviewed_at=?, team_id=?
                           WHERE id=?""", (admin_id, utcnow(), tid, rid))
        t = team_by_id(tid)
        text = (f"✅ Ваш запрос одобрен: команда «{esc(final)}» создана, вы её лидер.\n\n"
                f"Реферальная ссылка для набора людей:\n<code>{bot_link(t['ref_code'])}</code>\n"
                f"Добавлять людей можно и вручную: «👤 Профиль» → «➕ Добавить работника».")
        if promoted:
            text += "\n\n⬆️ Вам выдан ранг «Младший тимлид 1 ур.»."
        await safe_send(bot, uid, text)
        return True, f"Команда «{final}» создана ✅"
    note = (note or "").strip()[:300]
    with db() as con:
        con.execute("""UPDATE team_requests SET status='rejected', reviewed_by=?, reviewed_at=?, review_note=?
                       WHERE id=?""", (admin_id, utcnow(), note, rid))
    text = f"❌ Запрос на команду «{esc(r['name'])}» отклонён."
    if note:
        text += f"\nПричина: {esc(note)}"
    await safe_send(bot, uid, text + "\nЕсли что-то неясно — напишите администратору.")
    return True, "Отклонено ❌"


# ---------- бот: диалог «Запросить команду» ----------
async def tr_begin(target, user, state: FSMContext):
    await state.clear()
    touch_user(user)
    ok, why = can_request_team(user.id)
    back = kb([[btn("👤 Профиль", "prof:me")]])
    if not ok:
        await show(target, "🙋 " + esc(why), back)
        return
    await state.set_state(TeamReqFSM.name)
    await show(target, "🙋 <b>Запрос команды</b>\nАдминистратор получит заявку и создаст для вас команду — "
                       "вы станете её лидером.\n\nВведите желаемое название "
                       f"({TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов).\n/cancel — отмена.",
               kb([[btn("❌ Отмена", "tr:x")]]))


@router.message(Command("requestteam"))
async def cmd_requestteam(message: Message, state: FSMContext):
    await tr_begin(message, message.from_user, state)


@router.callback_query(F.data == "tr:start")
async def cb_tr_start(cb: CallbackQuery, state: FSMContext):
    await tr_begin(cb, cb.from_user, state)
    await cb.answer()


@router.callback_query(F.data == "tr:x")
async def cb_tr_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_profile(cb, cb.from_user.id, cb.from_user.id)
    await cb.answer()


@router.message(TeamReqFSM.name, F.text, ~F.text.startswith("/"))
async def tr_name(message: Message, state: FSMContext):
    name = clean_team_name(message.text)
    if not TEAM_NAME_MIN <= len(name) <= TEAM_NAME_MAX:
        await message.answer(f"Название должно быть от {TEAM_NAME_MIN} до {TEAM_NAME_MAX} символов. Ещё раз:")
        return
    if team_name_taken(name):
        await message.answer("Команда с таким названием уже есть — придумайте другое:")
        return
    await state.update_data(name=name)
    await state.set_state(TeamReqFSM.comment)
    await message.answer(f"Название: <b>«{esc(name)}»</b>.\nДобавьте комментарий для администратора (как планируете "
                         f"набирать людей, опыт) или отправьте «-», чтобы пропустить.")


@router.message(TeamReqFSM.comment, F.text, ~F.text.startswith("/"))
async def tr_comment(message: Message, state: FSMContext):
    name = (await state.get_data()).get("name")
    text = message.text.strip()
    comment = "" if text == "-" else text
    if not name:
        await state.clear()
        await message.answer("Сессия устарела — начните заново: /requestteam")
        return
    rid, err = create_team_request(message.from_user.id, name, comment)
    await state.clear()
    if not rid:
        await message.answer("⚠️ " + esc(err), reply_markup=kb([[btn("👤 Профиль", "prof:me")]]))
        return
    await message.answer(f"✅ Заявка на команду «{esc(name)}» отправлена администратору. "
                         f"Вы получите уведомление, когда её рассмотрят.",
                         reply_markup=kb([[btn("👤 Профиль", "prof:me")]]))
    await notify_admins_team_request(message.bot, rid)


# ---------- админ: запросы команд ----------
@admin_router.callback_query(F.data == "adm:rqs")
async def adm_rqs(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    rows_db = pending_team_requests()
    rows = []
    for r in rows_db:
        name = f"@{r['username']}" if r["username"] else (r["first_name"] or r["user_id"])
        rows.append([btn(f"#{r['id']} · «{r['name']}» · {name}"[:60], f"adm:rq:view:{r['id']}")])
    rows.append([btn("🔄 Обновить", "adm:rqs"), btn("⬅️ Назад", "adm:home")])
    text = f"🙋 <b>Запросы на создание команд</b> ({count_pending_team_requests()})"
    if not rows_db:
        text += "\n\nНовых запросов нет 🎉"
    await show(cb, text, kb(rows))
    await cb.answer()


@admin_router.callback_query(F.data.regexp(r"^adm:rq:(view|ok|no|ren):\d+$"))
async def adm_rq(cb: CallbackQuery, state: FSMContext):
    _, _, act, rid = cb.data.split(":")
    rid = int(rid)
    r = team_request_by_id(rid)
    if not r:
        await cb.answer("Заявка не найдена.", show_alert=True)
        return
    if act in ("no", "ren") and r["status"] == "pending":
        await state.set_state(AdminFSM.rq_reason if act == "no" else AdminFSM.rq_name)
        await state.update_data(rid=rid)
        await cb.message.answer("Укажите причину отказа — её увидит автор. «-» — без причины. /cancel — отмена."
                                if act == "no" else
                                f"Введите новое название команды ({TEAM_NAME_MIN}–{TEAM_NAME_MAX} символов) — "
                                f"команда будет создана сразу. /cancel — отмена.")
        await cb.answer()
        return
    if act == "ok":
        ok, msg = await decide_team_request(cb.bot, rid, cb.from_user.id, True)
        if not ok:
            await cb.answer(msg, show_alert=True)
            return
        await cb.answer(msg)
    else:
        await cb.answer()
    r = team_request_by_id(rid)
    await show(cb, team_request_text(r), team_request_kb(r))


@admin_router.message(AdminFSM.rq_reason, F.text)
async def adm_rq_reason(message: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    note = "" if message.text.strip() == "-" else message.text.strip()
    await state.clear()
    ok, msg = await decide_team_request(message.bot, rid, message.from_user.id, False, note=note)
    r = team_request_by_id(rid)
    await message.answer(msg, reply_markup=team_request_kb(r) if r else None)


@admin_router.message(AdminFSM.rq_name, F.text)
async def adm_rq_name(message: Message, state: FSMContext):
    rid = (await state.get_data())["rid"]
    ok, msg = await decide_team_request(message.bot, rid, message.from_user.id, True, name=message.text)
    if not ok and ("занято" in msg or "символов" in msg):
        await message.answer("⚠️ " + esc(msg) + "\nВведите другое название или /cancel.")
        return
    await state.clear()
    r = team_request_by_id(rid)
    await message.answer(msg, reply_markup=team_request_kb(r) if r else None)


# ======================= ПОДКЛЮЧЕНИЕ =======================
async def setup_teams(dp: Dispatcher, bot: Bot, main_kb=None):
    """Вызвать один раз в main() после init_db() и init_team_db()."""
    global _MAIN_KB, _BOT_USERNAME, _reminder_task
    _MAIN_KB = main_kb
    _BOT_USERNAME = (await bot.get_me()).username or ""
    dp.message.outer_middleware(BanMiddleware())
    dp.callback_query.outer_middleware(BanMiddleware())
    dp.include_router(router)
    _reminder_task = asyncio.create_task(reminder_loop(bot))


"""
ПОДКЛЮЧЕНИЕ К bot_fixed.py (уже сделано в готовом bot_fixed.py; схема для справки)
1) Импорт:
       from teams import init_team_db, setup_teams, handle_start_payload
2) В вашем /start (после проверки подписки):
       @dp.message(CommandStart())
       async def start(message: Message, command: CommandObject, state: FSMContext):
           upsert_user(message.from_user)
           ...проверка подписки...
           if await handle_start_payload(message, command.args, state):  # из callback: user=cb.from_user
               return
           ...ваш обычный код...
   (CommandObject: from aiogram.filters import CommandObject)
3) В main():
       init_db()
       init_team_db()
       await setup_teams(dp, bot, main_kb)
       await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
"""
