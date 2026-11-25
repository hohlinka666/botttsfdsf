"""
webapp.py — Telegram Mini App («Кабинет»): профиль, отчёты, рейтинг, проверка отчётов и оповещения для админов.
Бизнес-логика общая с ботом (teams.py), здесь только API и проверка подписи Telegram initData.
Запускается из bot_fixed.py, если в .env задан WEBAPP_URL (публичный https-адрес).
"""
import hashlib
import hmac
import html as _html
import json
import logging
import re
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qsl

import teams as T

log = logging.getLogger("webapp")
INDEX_PATH = Path(__file__).parent / "webapp" / "index.html"


# -------------------- подпись Telegram --------------------
def validate_init_data(init_data: str, token: str, max_age: int = 86400):
    """Проверяет подпись initData по алгоритму Telegram. Возвращает dict пользователя или None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", None)
    if not got:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, got):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        user = json.loads(pairs["user"])
        return user if isinstance(user, dict) and "id" in user else None
    except (ValueError, KeyError):
        return None


# -------------------- API (без привязки к веб-фреймворку) --------------------
def _num(v, lo, hi, integer):
    if isinstance(v, bool):
        raise ValueError
    x = float(str(v).replace(",", ".").strip())
    if not lo <= x <= hi or (integer and x != int(x)):
        raise ValueError
    return int(x) if integer else round(x, 2)


def _name(row) -> str:
    return f"@{row['username']}" if row["username"] else (row["first_name"] or "участник")


def _report_dict(r) -> dict:
    return {"id": r["id"], "date": T.fmt_date(r["report_date"]), "hours": r["hours"], "scouts": r["scouts"],
            "models": r["models"], "operators": r["operators"], "status": r["status"],
            "note": r["review_note"] or ""}


def _plain(text: str) -> str:
    """HTML из сообщений бота -> обычный текст для мини-приложения."""
    return _html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def _int(v):
    if isinstance(v, bool):
        raise ValueError
    return int(v)


def _team_request_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "comment": r["comment"] or "", "status": r["status"],
            "note": r["review_note"] or "", "date": T.fmt_date(r["created_at"])}


def _today_report(uid):
    with T.db() as con:
        return con.execute("SELECT * FROM reports WHERE user_id=? AND report_date=?",
                           (uid, T.msk_now().date().isoformat())).fetchone()


def _totals(t) -> dict:
    return {"reports": t["n"], "hours": t["h"], "scouts": t["s"], "models": t["m"], "operators": t["o"]}


def init_agency_extensions():
    """Аддитивные таблицы Mini App: общий USD-баланс, операции, выводы, реферальные ссылки, уведомления и задачи."""
    with T.db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS agency_wallets (user_id INTEGER PRIMARY KEY, balance_cents INTEGER NOT NULL DEFAULT 0, reserved_cents INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_transactions (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, kind TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'completed', ref_key TEXT UNIQUE, balance_after_cents INTEGER NOT NULL, created_at TEXT NOT NULL, created_by INTEGER)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_agency_tx_user ON agency_transactions(user_id,id DESC)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_ref_links (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL UNIQUE, inviter_id INTEGER NOT NULL, role TEXT NOT NULL, created_at TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_referrals (id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT NOT NULL, inviter_id INTEGER NOT NULL, invited_id INTEGER NOT NULL UNIQUE, role TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'registered', created_at TEXT NOT NULL, started_at TEXT, reward_cents INTEGER NOT NULL DEFAULT 0, reward_paid INTEGER NOT NULL DEFAULT 0)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_withdrawals (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, amount_cents INTEGER NOT NULL, address TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', txid TEXT DEFAULT '', created_at TEXT NOT NULL, reviewed_by INTEGER, reviewed_at TEXT)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_notifications (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, category TEXT NOT NULL DEFAULT 'general', is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, body TEXT NOT NULL, audience TEXT NOT NULL DEFAULT 'all', target_user INTEGER, created_by INTEGER NOT NULL, created_at TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS agency_task_status (task_id INTEGER NOT NULL, user_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'new', updated_at TEXT NOT NULL, PRIMARY KEY(task_id,user_id))")


def _wallet(con, uid):
    con.execute("INSERT OR IGNORE INTO agency_wallets(user_id,updated_at) VALUES(?,?)", (uid,T.utcnow()))
    return con.execute("SELECT * FROM agency_wallets WHERE user_id=?",(uid,)).fetchone()


def _notify(con, uid, title, body, category='finance'):
    con.execute("INSERT INTO agency_notifications(user_id,title,body,category,created_at) VALUES(?,?,?,?,?)",(uid,title,body,category,T.utcnow()))


def _trc20_valid(address):
    # TRON addresses use Base58Check: 34 chars, prefix T, decoded payload 0x41 + 20 bytes + 4-byte checksum.
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    if not isinstance(address,str) or len(address)!=34 or not address.startswith('T') or any(c not in alphabet for c in address): return False
    n=0
    for c in address: n=n*58+alphabet.index(c)
    raw=n.to_bytes((n.bit_length()+7)//8,'big')
    raw=b'\x00'*(len(address)-len(address.lstrip('1')))+raw
    if len(raw)!=25 or raw[0]!=0x41: return False
    import hashlib as _hashlib
    return hmac.compare_digest(raw[-4:],_hashlib.sha256(_hashlib.sha256(raw[:-4]).digest()).digest()[:4])


async def handle_api(bot, path: str, tg_user: dict, body: dict):
    """Возвращает (http_status, json-словарь)."""
    uid = tg_user["id"]
    admin = T.is_admin(uid)
    if not admin and T.is_banned(uid, tg_user.get("username")):
        return 403, {"ok": False, "error": "Доступ ограничен"}
    T.touch_user(SimpleNamespace(id=uid, username=tg_user.get("username"), first_name=tg_user.get("first_name")))
    u = T.get_user(uid)

    if path == "/api/me":
        mem, led = T.member_team(uid), T.teams_led_by(uid)
        today = _today_report(uid)
        can_req, why = T.can_request_team(uid)
        last_req = T.latest_team_request(uid)
        is_adder = admin or u["rank"] in T.ADDER_RANKS
        return 200, {"ok": True, "me": {
            "id": uid, "name": _name(u), "rank": T.rank_label(u["rank"], u["level"]) if u["rank"] else None,
            "can_report": bool(u["rank"]), "team": mem["name"] if mem else None,
            "leads": [t["name"] for t in led], "is_admin": admin,
            "is_member": bool(u["rank"]) or admin,
            "can_add": bool(is_adder and T.addable_teams(uid)), "is_adder": bool(is_adder),
            "can_request_team": can_req, "request_block": why if not can_req else "",
            "team_request": _team_request_dict(last_req) if last_req else None},
            "week": _totals(T.user_totals(uid, T.since7())), "all": _totals(T.user_totals(uid)),
            "today": _report_dict(today) if today else None,
            "pending": T.count_pending_reports() if admin else 0,
            "pending_teams": T.count_pending_team_requests() if admin else 0}

    if path == "/api/profile":
        prof = T.profile_of(uid)
        if not prof:
            return 200, {"ok": True, "profile": None, "status": "not_found"}
        return 200, {"ok": True, "profile": {"id": prof["id"], "full_name": prof["full_name"], "birth_date": prof["birth_date"], "phone": prof["phone"], "tg_username": prof["tg_username"], "country": prof["country"], "english": prof["english"], "experience": prof["experience"], "kind": prof["kind"], "status": prof["status"], "review_note": prof["review_note"] or ""}}

    if path == "/api/profile/update":
        prof = T.profile_of(uid)
        if not prof:
            return 404, {"ok": False, "error": "Анкета не найдена"}
        if prof["status"] == "pending":
            return 409, {"ok": False, "error": "Анкета уже находится на проверке. Дождитесь решения администрации."}
        fields = {k: str(body.get(k) or "").strip() for k in ("full_name", "birth_date", "phone", "tg_username", "country", "english", "experience")}
        if not fields["full_name"] or not fields["phone"]:
            return 400, {"ok": False, "error": "Заполните имя и телефон"}
        phone = re.sub(r"\D+", "", fields["phone"])
        username = fields["tg_username"].lstrip("@").lower()
        with T.db() as con:
            others = con.execute("SELECT id,phone,tg_username FROM worker_profiles WHERE id<>? AND status<>'rejected'", (prof["id"],)).fetchall()
            dup = next((r for r in others if (phone and re.sub(r"\D+", "", r["phone"] or "") == phone) or (username and (r["tg_username"] or "").lstrip("@").lower() == username)), None)
            if dup:
                return 409, {"ok": False, "error": "Пользователь с таким username или телефоном уже существует. Проверьте корректность данных."}
            con.execute("UPDATE worker_profiles SET full_name=?,birth_date=?,phone=?,tg_username=?,country=?,english=?,experience=?,status='pending',reviewed_by=NULL,reviewed_at=NULL,review_note=NULL WHERE id=?", (fields["full_name"], fields["birth_date"], fields["phone"], username, fields["country"], fields["english"], fields["experience"], prof["id"]))
        for aid in T.ADMIN_IDS:
            await T.safe_send(bot, aid, f"🪪 Пользователь {uid} обновил анкету #{prof['id']}. Требуется повторная проверка.", reply_markup=T.kb([[T.btn("Открыть анкету", f"adm:wp:{prof['id']}")]]))
        return 200, {"ok": True, "status": "pending"}

    if path == "/api/invited":
        with T.db() as con:
            rows = con.execute("SELECT p.id,p.full_name,p.kind,p.status,p.created_at,p.phone,p.tg_username FROM worker_profiles p WHERE p.added_by=? ORDER BY p.id DESC LIMIT 100", (uid,)).fetchall()
        return 200, {"ok": True, "invited": [{"id": r["id"], "name": r["full_name"], "kind": r["kind"], "status": r["status"], "date": T.fmt_date(r["created_at"]), "phone": r["phone"], "username": r["tg_username"]} for r in rows]}

    if path == "/api/role/request":
        new_role = str(body.get("role") or "")
        if new_role not in T.KIND_TITLES:
            return 400, {"ok": False, "error": "Выберите корректную роль"}
        with T.db() as con:
            exists = con.execute("SELECT id FROM role_change_requests WHERE user_id=? AND status='pending'", (uid,)).fetchone()
            if exists: return 409, {"ok": False, "error": "У вас уже есть заявка на рассмотрении"}
            rid = con.execute("INSERT INTO role_change_requests(user_id,old_role,new_role,created_at) VALUES(?,?,?,?)", (uid, u["rank"], new_role, T.utcnow())).lastrowid
        for aid in T.ADMIN_IDS:
            await T.safe_send(bot, aid, f"🔄 Заявка на смену роли #{rid}\nПользователь: {T.esc(T.who(u, uid))}\nТекущая роль: {T.esc(T.KIND_TITLES.get(u['rank'], u['rank'] or 'не назначена'))}\nНовая роль: {T.esc(T.KIND_TITLES[new_role])}", reply_markup=T.kb([[T.btn("Открыть заявки", "adm:role_requests")]]))
        return 200, {"ok": True, "id": rid}

    if path == "/api/search":
        if not admin: return 403, {"ok": False, "error": "Нет доступа"}
        q = str(body.get("q") or "").strip()
        surname = str(body.get("surname") or "").strip()
        first_name = str(body.get("first_name") or "").strip()
        username = str(body.get("username") or "").strip().lstrip("@")
        phone = str(body.get("phone") or "").strip()
        if not any((q, surname, first_name, username, phone)): return 200, {"ok": True, "results": []}
        with T.db() as con:
            clauses=[]; params=[]
            if q:
                like="%"+q.replace("%","")+"%"; clauses.append("(p.full_name LIKE ? OR p.phone LIKE ? OR p.tg_username LIKE ? OR u.username LIKE ?)"); params += [like,like,like,like]
            if surname: clauses.append("p.full_name LIKE ?"); params.append("%"+surname+"%")
            if first_name: clauses.append("(p.full_name LIKE ? OR u.first_name LIKE ?)"); params += ["%"+first_name+"%", "%"+first_name+"%"]
            if username: clauses.append("(p.tg_username LIKE ? OR u.username LIKE ?)"); params += ["%"+username+"%","%"+username+"%"]
            if phone: clauses.append("p.phone LIKE ?"); params.append("%"+phone+"%")
            rows = con.execute("SELECT p.id,p.user_id,p.full_name,p.phone,p.tg_username,p.kind,p.status,u.username AS account_username,u.first_name FROM worker_profiles p LEFT JOIN users u ON u.user_id=p.user_id WHERE " + " AND ".join(clauses) + " ORDER BY p.id DESC LIMIT 50", params).fetchall()
        return 200, {"ok": True, "results": [{"id": r["id"], "user_id": r["user_id"], "name": r["full_name"] or r["first_name"] or "—", "username": r["tg_username"] or r["account_username"], "phone": r["phone"], "role": r["kind"], "status": r["status"]} for r in rows]}

    if path == "/api/history":
        with T.db() as con:
            rows = con.execute("SELECT * FROM reports WHERE user_id=? ORDER BY report_date DESC LIMIT 14",
                               (uid,)).fetchall()
        return 200, {"ok": True, "reports": [_report_dict(r) for r in rows]}

    if path == "/api/submit":
        if not u["rank"]:
            return 400, {"ok": False, "error": "Отчёты доступны участникам команд"}
        try:
            h = _num(body.get("hours"), 0, 24, False)
            s, m, o = (_num(body.get(k), 0, 9999, True) for k in ("scouts", "models", "operators"))
        except (ValueError, TypeError):
            return 400, {"ok": False, "error": "Проверьте числа: часы 0–24, остальное — целые от 0"}
        rid = T.save_report(uid, h, s, m, o)
        await T.notify_admins_report(bot, rid)
        return 200, {"ok": True, "id": rid}

    if path == "/api/top":
        if not admin and not u["rank"]:
            return 403, {"ok": False, "error": "Рейтинг доступен участникам команд"}
        people = [{"name": _name(p), "brought": p["brought"], "hours": p["h"]} for p in T.top_people(T.since7())]
        teams = sorted(T.teams_with_stats(), key=lambda x: -x["brought7"])[:5]
        return 200, {"ok": True, "people": people,
                     "teams": [{"name": t["name"], "brought": t["brought7"], "members": t["members"]} for t in teams]}

    if path == "/api/team":
        addable = {t["id"]: t for t in T.addable_teams(uid)}
        mem = T.member_team(uid)
        ids = list(addable) + ([mem["id"]] if mem and mem["id"] not in addable else [])
        s7 = T.since7()
        invites = T.pending_invites(addable)
        out = []
        for tid in ids:
            t = T.team_by_id(tid)
            if not t:
                continue
            leader = T.get_user(t["leader_id"])
            members = T.team_members(tid)
            d = {"id": tid, "name": t["name"], "leader": _name(leader) if leader else "—",
                 "is_leader": t["leader_id"] == uid, "can_add": tid in addable, "member_count": len(members)}
            if tid in addable:
                d["members"] = [{"name": _name(m), "rank": T.rank_label(m["rank"], m["level"]) if m["rank"] else "—",
                                 "kind": m["rank"] or "", "brought7": (lambda x: x["s"] + x["m"] + x["o"])(
                                     T.user_totals(m["user_id"], s7))} for m in members[:60]]
                d["invites"] = [{"who": f"@{i['username']}" if i["username"] else "по ссылке", "kind": i["kind"],
                                 "link": T.invite_link(i["token"]), "date": T.fmt_date(i["created_at"])}
                                for i in invites if i["team_id"] == tid]
            out.append(d)
        return 200, {"ok": True, "teams": out}

    if path == "/api/team/add":
        try:
            team_id = _int(body.get("team_id"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "Выберите команду"}
        res = await T.add_worker(bot, uid, team_id, str(body.get("kind") or ""), str(body.get("user") or ""))
        msg = _plain(res["text"])
        if res["link"]:  # ссылка показывается отдельным блоком с кнопками «Копировать / Отправить»
            msg = msg.replace(res["link"], "").rstrip(" :\n")
        return 200, {"ok": res["ok"], "status": res["status"], "retry": res["retry"],
                     "message": msg, "link": res["link"]}

    if path == "/api/team/request":
        rid, err = T.create_team_request(uid, str(body.get("name") or ""), str(body.get("comment") or ""))
        if not rid:
            return 400, {"ok": False, "error": err}
        await T.notify_admins_team_request(bot, rid)
        return 200, {"ok": True, "id": rid}

    if path == "/api/finance":
        with T.db() as con:
            w=_wallet(con,uid)
            tx=con.execute("SELECT * FROM agency_transactions WHERE user_id=? ORDER BY id DESC LIMIT 30",(uid,)).fetchall()
            wd=con.execute("SELECT * FROM agency_withdrawals WHERE user_id=? ORDER BY id DESC LIMIT 20",(uid,)).fetchall()
        return 200,{"ok":True,"balance":w["balance_cents"],"reserved":w["reserved_cents"],"available":w["balance_cents"]-w["reserved_cents"],"transactions":[{"id":r["id"],"amount":r["amount_cents"],"kind":r["kind"],"reason":r["reason"],"status":r["status"],"date":T.fmt_dt(r["created_at"])} for r in tx],"withdrawals":[{"id":r["id"],"amount":r["amount_cents"],"address":r["address"],"status":r["status"],"txid":r["txid"],"date":T.fmt_dt(r["created_at"])} for r in wd]}

    if path == "/api/withdraw":
        try: cents=int(round(float(body.get("amount"))*100))
        except (ValueError,TypeError): return 400,{"ok":False,"error":"Введите корректную сумму"}
        address=str(body.get("address") or "").strip()
        if cents<100 or not _trc20_valid(address): return 400,{"ok":False,"error":"Минимум $1.00. Проверьте адрес USDT TRC20 (Base58Check)."}
        with T.db() as con:
            w=_wallet(con,uid)
            if w["balance_cents"]-w["reserved_cents"]<cents: return 409,{"ok":False,"error":"Недостаточно доступных средств"}
            cur=con.execute("INSERT INTO agency_withdrawals(user_id,amount_cents,address,created_at) VALUES(?,?,?,?)",(uid,cents,address,T.utcnow()))
            con.execute("UPDATE agency_wallets SET reserved_cents=reserved_cents+?,updated_at=? WHERE user_id=?",(cents,T.utcnow(),uid))
            _notify(con,uid,"Заявка на вывод создана",f"${cents/100:.2f} зарезервировано до решения администрации.")
        for aid in T.ADMIN_IDS: await T.safe_send(bot,aid,f"💸 Заявка на вывод #{cur.lastrowid}: пользователь {uid}, ${cents/100:.2f}\n{T.esc(address)}")
        return 200,{"ok":True,"id":cur.lastrowid}

    if path == "/api/referrals":
        with T.db() as con:
            links=con.execute("SELECT * FROM agency_ref_links WHERE inviter_id=? AND active=1 ORDER BY id DESC",(uid,)).fetchall()
            refs=con.execute("SELECT * FROM agency_referrals WHERE inviter_id=? ORDER BY id DESC LIMIT 100",(uid,)).fetchall()
        botname=T._BOT_USERNAME
        return 200,{"ok":True,"links":[{"role":r["role"],"url":f"https://t.me/{botname}?start=agency_{r['code']}" if botname else "","code":r["code"]} for r in links],"referrals":[{"id":r["id"],"user_id":r["invited_id"],"role":r["role"],"status":r["status"],"reward":r["reward_cents"],"paid":bool(r["reward_paid"]),"date":T.fmt_date(r["created_at"])} for r in refs]}

    if path == "/api/referrals/create":
        role=str(body.get("role") or "")
        if role not in ("scout","operator"): return 400,{"ok":False,"error":"Для моделей используется заявка через @vuittonceo"}
        import secrets as _secrets
        code=_secrets.token_urlsafe(9).replace('-','a').replace('_','b')
        with T.db() as con: con.execute("INSERT INTO agency_ref_links(code,inviter_id,role,created_at) VALUES(?,?,?,?)",(code,uid,role,T.utcnow()))
        url=f"https://t.me/{T._BOT_USERNAME}?start=agency_{code}" if T._BOT_USERNAME else ""
        return 200,{"ok":True,"url":url}

    if path == "/api/notifications":
        with T.db() as con: rows=con.execute("SELECT * FROM agency_notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(uid,)).fetchall()
        return 200,{"ok":True,"items":[{"id":r["id"],"title":r["title"],"body":r["body"],"category":r["category"],"read":bool(r["is_read"]),"date":T.fmt_dt(r["created_at"])} for r in rows]}

    if path == "/api/notifications/read":
        with T.db() as con: con.execute("UPDATE agency_notifications SET is_read=1 WHERE id=? AND user_id=?",(_int(body.get("id")),uid))
        return 200,{"ok":True}

    if path == "/api/tasks":
        with T.db() as con:
            rows=con.execute("SELECT t.*,COALESCE(s.status,'new') AS status FROM agency_tasks t LEFT JOIN agency_task_status s ON s.task_id=t.id AND s.user_id=? WHERE t.audience='all' OR t.audience=? OR t.target_user=? ORDER BY t.id DESC LIMIT 100",(uid,u["rank"] or "user",uid)).fetchall()
        return 200,{"ok":True,"items":[{"id":r["id"],"title":r["title"],"body":r["body"],"status":r["status"],"date":T.fmt_date(r["created_at"])} for r in rows]}

    if path == "/api/tasks/status":
        task_id=_int(body.get("id")); status=str(body.get("status") or "")
        if status not in ("in_progress","completed"): return 400,{"ok":False,"error":"Неверный статус"}
        with T.db() as con:
            exists=con.execute("SELECT id FROM agency_tasks WHERE id=? AND (audience='all' OR audience=? OR target_user=?)",(task_id,u["rank"] or "user",uid)).fetchone()
            if not exists: return 404,{"ok":False,"error":"Задача не найдена"}
            con.execute("INSERT INTO agency_task_status(task_id,user_id,status,updated_at) VALUES(?,?,?,?) ON CONFLICT(task_id,user_id) DO UPDATE SET status=excluded.status,updated_at=excluded.updated_at",(task_id,uid,status,T.utcnow()))
        return 200,{"ok":True}

    # ---- только для админов ----
    if not admin:
        return 403, {"ok": False, "error": "Нет доступа"}

    if path == "/api/admin/referrals":
        with T.db() as con:
            rows=con.execute("SELECT r.*,u.username,u.first_name FROM agency_referrals r LEFT JOIN users u ON u.user_id=r.invited_id ORDER BY r.id DESC LIMIT 200").fetchall()
        return 200,{"ok":True,"items":[{"id":r["id"],"inviter_id":r["inviter_id"],"invited_id":r["invited_id"],"username":r["username"] or r["first_name"] or str(r["invited_id"]),"role":r["role"],"status":r["status"],"reward":r["reward_cents"],"paid":bool(r["reward_paid"])} for r in rows]}

    if path == "/api/admin/referral/start":
        rid=_int(body.get("id"))
        with T.db() as con:
            r=con.execute("SELECT * FROM agency_referrals WHERE id=?",(rid,)).fetchone()
            if not r: return 404,{"ok":False,"error":"Реферал не найден"}
            if r["status"] in ("started_work","reward_paid"): return 409,{"ok":False,"error":"Старт уже подтверждён"}
            con.execute("UPDATE agency_referrals SET status='started_work',started_at=? WHERE id=?",(T.utcnow(),rid))
            _notify(con,r["inviter_id"],"Начало работы подтверждено",f"Приглашённый пользователь #{r['invited_id']} приступил к работе. Бонус ожидает начисления.","referral")
        return 200,{"ok":True}

    if path == "/api/admin/referral/pay":
        rid=_int(body.get("id"))
        with T.db() as con:
            r=con.execute("SELECT * FROM agency_referrals WHERE id=?",(rid,)).fetchone()
            if not r or r["status"]!='started_work' or r["reward_paid"]: return 409,{"ok":False,"error":"Нельзя начислить: проверьте статус или бонус уже выплачен"}
            w=_wallet(con,r["inviter_id"]); new=w["balance_cents"]+r["reward_cents"]
            con.execute("UPDATE agency_wallets SET balance_cents=?,updated_at=? WHERE user_id=?",(new,T.utcnow(),r["inviter_id"]))
            con.execute("INSERT INTO agency_transactions(user_id,amount_cents,kind,reason,ref_key,balance_after_cents,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",(r["inviter_id"],r["reward_cents"],"credit",f"Реферальное вознаграждение — ${r['reward_cents']/100:.2f}",f"referral:{rid}",new,T.utcnow(),uid))
            con.execute("UPDATE agency_referrals SET status='reward_paid',reward_paid=1 WHERE id=?",(rid,))
            _notify(con,r["inviter_id"],"Реферальный бонус начислен",f"На баланс зачислено ${r['reward_cents']/100:.2f}.","finance")
        await T.safe_send(bot,r["inviter_id"],f"💰 Вам начислено ${r['reward_cents']/100:.2f} за приглашённого сотрудника.")
        return 200,{"ok":True}

    if path == "/api/admin/tasks/create":
        title=str(body.get("title") or "").strip()[:100]; desc=str(body.get("body") or "").strip()[:2000]; audience=str(body.get("audience") or "all")
        if not title or not desc or audience not in ("all","scout","operator","model"): return 400,{"ok":False,"error":"Заполните заголовок, текст и аудиторию"}
        with T.db() as con: cur=con.execute("INSERT INTO agency_tasks(title,body,audience,created_by,created_at) VALUES(?,?,?,?,?)",(title,desc,audience,uid,T.utcnow()))
        return 200,{"ok":True,"id":cur.lastrowid}

    if path == "/api/admin/finance/users":
        with T.db() as con:
            rows=con.execute("SELECT w.*,u.username,u.first_name FROM agency_wallets w LEFT JOIN users u ON u.user_id=w.user_id ORDER BY w.balance_cents DESC LIMIT 200").fetchall()
        return 200,{"ok":True,"items":[{"user_id":r["user_id"],"username":r["username"] or r["first_name"] or str(r["user_id"]),"balance":r["balance_cents"],"reserved":r["reserved_cents"]} for r in rows]}

    if path == "/api/admin/finance/adjust":
        try: target=_int(body.get("user_id")); cents=int(round(float(body.get("amount"))*100))
        except (ValueError,TypeError): return 400,{"ok":False,"error":"Проверьте пользователя и сумму"}
        reason=str(body.get("reason") or "").strip()[:240]
        if not cents or not reason: return 400,{"ok":False,"error":"Сумма и причина обязательны"}
        with T.db() as con:
            w=_wallet(con,target); new=w["balance_cents"]+cents
            if new<w["reserved_cents"]: return 409,{"ok":False,"error":"Нельзя списать зарезервированные средства"}
            con.execute("UPDATE agency_wallets SET balance_cents=?,updated_at=? WHERE user_id=?",(new,T.utcnow(),target))
            con.execute("INSERT INTO agency_transactions(user_id,amount_cents,kind,reason,ref_key,balance_after_cents,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",(target,cents,"credit" if cents>0 else "debit",reason,f"admin:{uid}:{T.utcnow()}:{target}",new,T.utcnow(),uid))
            _notify(con,target,"Баланс обновлён",f"{'Начислено' if cents>0 else 'Списано'} ${abs(cents)/100:.2f}. Причина: {reason}")
        await T.safe_send(bot,target,f"💳 Баланс обновлён: {'+' if cents>0 else '-'}${abs(cents)/100:.2f}. Причина: {T.esc(reason)}")
        return 200,{"ok":True,"balance":new}

    if path == "/api/admin/withdrawals":
        with T.db() as con: rows=con.execute("SELECT * FROM agency_withdrawals WHERE status='pending' ORDER BY id ASC LIMIT 100").fetchall()
        return 200,{"ok":True,"items":[{"id":r["id"],"user_id":r["user_id"],"amount":r["amount_cents"],"address":r["address"],"date":T.fmt_dt(r["created_at"])} for r in rows]}

    if path == "/api/admin/withdrawal/decide":
        wid=_int(body.get("id")); action=str(body.get("action") or ""); txid=str(body.get("txid") or "").strip()
        if action not in ("approve","reject") or (action=="approve" and not txid): return 400,{"ok":False,"error":"Для выплаты нужен TXID"}
        with T.db() as con:
            r=con.execute("SELECT * FROM agency_withdrawals WHERE id=? AND status='pending'",(wid,)).fetchone()
            if not r: return 404,{"ok":False,"error":"Заявка уже обработана"}
            w=_wallet(con,r["user_id"])
            if action=="approve":
                con.execute("UPDATE agency_wallets SET balance_cents=balance_cents-?,reserved_cents=reserved_cents-?,updated_at=? WHERE user_id=?",(r["amount_cents"],r["amount_cents"],T.utcnow(),r["user_id"]))
                con.execute("UPDATE agency_withdrawals SET status='paid',txid=?,reviewed_by=?,reviewed_at=? WHERE id=?",(txid,uid,T.utcnow(),wid))
                _notify(con,r["user_id"],"Вывод выполнен",f"Выплата ${r['amount_cents']/100:.2f}. TXID: {txid}")
            else:
                con.execute("UPDATE agency_wallets SET reserved_cents=MAX(0,reserved_cents-?),updated_at=? WHERE user_id=?",(r["amount_cents"],T.utcnow(),r["user_id"]))
                con.execute("UPDATE agency_withdrawals SET status='rejected',reviewed_by=?,reviewed_at=? WHERE id=?",(uid,T.utcnow(),wid))
                _notify(con,r["user_id"],"Вывод отклонён",f"Резерв ${r['amount_cents']/100:.2f} возвращён в доступный баланс.")
        await T.safe_send(bot,r["user_id"],"Заявка на вывод выполнена." if action=="approve" else "Заявка на вывод отклонена. Средства возвращены в доступный баланс.")
        return 200,{"ok":True}

    if path == "/api/admin/profiles":
        with T.db() as con:
            rows = con.execute("SELECT p.*,u.username,u.first_name FROM worker_profiles p LEFT JOIN users u ON u.user_id=p.user_id WHERE p.status='pending' ORDER BY p.id DESC LIMIT 50").fetchall()
        return 200, {"ok": True, "profiles": [{"id": r["id"], "user_id": r["user_id"], "name": r["full_name"], "phone": r["phone"], "username": r["tg_username"], "role": r["kind"], "date": T.fmt_date(r["created_at"])} for r in rows]}

    if path == "/api/admin/profile/decide":
        try: pid = _int(body.get("id"))
        except (TypeError, ValueError): return 400, {"ok": False, "error": "Неверный ID"}
        action = body.get("action")
        if action not in ("approve", "reject"): return 400, {"ok": False, "error": "Неверное действие"}
        p = T.worker_profile_get(pid)
        if not p or p["status"] != "pending": return 404, {"ok": False, "error": "Анкета уже обработана"}
        with T.db() as con:
            con.execute("UPDATE worker_profiles SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", ("approved" if action=="approve" else "rejected", uid, T.utcnow(), pid))
        if action == "approve" and p["user_id"]:
            current=T.get_user(p["user_id"])
            if current and not current["rank"]:
                T.set_rank(p["user_id"], p["kind"], 1)
                if p["team_id"]: T.add_member(p["user_id"], p["team_id"], via_ref=0, added_by=p["added_by"] or uid)
        if p["user_id"] and action == "approve":
            with T.db() as con:
                con.execute("UPDATE agency_referrals SET status='approved' WHERE invited_id=? AND status='registered'", (p["user_id"],))
        if p["user_id"]:
            await T.safe_send(bot, p["user_id"], "✅ Анкета одобрена. Роль активирована." if action=="approve" else "❌ Анкета отклонена. Свяжитесь с пригласившим.", reply_markup=T.main_menu_kb(p["user_id"]))
        return 200, {"ok": True}

    if path == "/api/admin/role_requests":
        with T.db() as con:
            rows = con.execute("SELECT r.*,u.username,u.first_name FROM role_change_requests r LEFT JOIN users u ON u.user_id=r.user_id WHERE r.status='pending' ORDER BY r.id DESC LIMIT 50").fetchall()
        return 200, {"ok": True, "requests": [{"id": r["id"], "user_id": r["user_id"], "user": ("@"+r["username"]) if r["username"] else r["first_name"], "old_role": r["old_role"], "new_role": r["new_role"], "date": T.fmt_date(r["created_at"])} for r in rows]}

    if path == "/api/admin/role_request/decide":
        try: rid = _int(body.get("id"))
        except (TypeError, ValueError): return 400, {"ok": False, "error": "Неверный ID"}
        action = body.get("action")
        if action not in ("approve", "reject"): return 400, {"ok": False, "error": "Неверное действие"}
        with T.db() as con:
            r = con.execute("SELECT * FROM role_change_requests WHERE id=? AND status='pending'", (rid,)).fetchone()
            if not r: return 404, {"ok": False, "error": "Заявка уже обработана"}
            con.execute("UPDATE role_change_requests SET status=?,reviewed_by=?,reviewed_at=? WHERE id=?", ("approved" if action=="approve" else "rejected", uid, T.utcnow(), rid))
        if action == "approve": T.set_rank(r["user_id"], r["new_role"], 1)
        await T.safe_send(bot, r["user_id"], "✅ Заявка на смену роли одобрена. Новая роль активирована." if action=="approve" else "❌ Заявка на смену роли отклонена.", reply_markup=T.main_menu_kb(r["user_id"]))
        return 200, {"ok": True}

    if path == "/api/admin/pending":
        out = []
        for r in T.pending_reports(30):
            d = _report_dict(r)
            d.update(user=_name(r), user_id=r["user_id"], rank=T.rank_label(r["rank"], r["level"]),
                     contact=T.contact_url(r))
            out.append(d)
        return 200, {"ok": True, "reports": out, "total": T.count_pending_reports()}

    if path == "/api/admin/review":
        action = body.get("action")
        if action not in ("approve", "reject"):
            return 400, {"ok": False, "error": "Неверное действие"}
        try:
            rid = int(body.get("id"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "Неверный id"}
        ok, msg = await T.review_report(bot, rid, uid, action == "approve", str(body.get("note") or ""))
        return 200, {"ok": ok, "message": msg}

    if path == "/api/admin/team_requests":
        out = []
        for r in T.pending_team_requests(30):
            d = _team_request_dict(r)
            d.update(user=_name(r), rank=T.rank_label(r["rank"], r["level"]) if r["rank"] else "—",
                     contact=T.contact_url(r))
            out.append(d)
        return 200, {"ok": True, "requests": out, "total": T.count_pending_team_requests()}

    if path == "/api/admin/team_request/decide":
        action = body.get("action")
        if action not in ("approve", "reject"):
            return 400, {"ok": False, "error": "Неверное действие"}
        try:
            rid = _int(body.get("id"))
        except (TypeError, ValueError):
            return 400, {"ok": False, "error": "Неверный id"}
        ok, msg = await T.decide_team_request(bot, rid, uid, action == "approve",
                                              name=str(body.get("name") or ""), note=str(body.get("note") or ""))
        return 200, {"ok": ok, "message": msg}

    if path == "/api/admin/model_alert":
        target = T.find_user(str(body.get("user") or ""))
        mode = body.get("mode")
        if not target:
            return 404, {"ok": False, "error": "Пользователь не найден (он должен был запускать бота)"}
        if mode not in ("all", "group", "bot"):
            return 400, {"ok": False, "error": "Неверный режим"}
        res = await T.send_model_alert(bot, target["user_id"], str(body.get("comment") or "")[:200], mode, uid)
        # $6 за подтверждённую администратором операцию «новая модель».
        # Ключ model-alert:<id> делает начисление идемпотентным.
        delivered = res.get("group") == "ok" or int(res.get("sent", 0)) > 0
        if delivered:
            with T.db() as con:
                alert=con.execute("SELECT id FROM model_alerts WHERE scout_id=? ORDER BY id DESC LIMIT 1",(target["user_id"],)).fetchone()
                if alert:
                    key=f"model-alert:{alert['id']}"
                    w=_wallet(con,target["user_id"])
                    new=w["balance_cents"]+600
                    cur=con.execute("INSERT OR IGNORE INTO agency_transactions(user_id,amount_cents,kind,reason,ref_key,balance_after_cents,created_at,created_by) VALUES(?,?,?,?,?,?,?,?)",(target["user_id"],600,"credit","Новая модель — $6",key,new,T.utcnow(),uid))
                    if cur.rowcount:
                        con.execute("UPDATE agency_wallets SET balance_cents=?,updated_at=? WHERE user_id=?",(new,T.utcnow(),target["user_id"]))
                        _notify(con,target["user_id"],"Начислено $6", "Подтверждена операция о новой модели. Средства зачислены на баланс.","finance")
                        await T.safe_send(bot,target["user_id"],"💳 Вам начислено $6.00 за подтверждённую операцию о новой модели.")
        return 200, {"ok": "error" not in res, "result": res, "reward_credited": bool(delivered)}

    return 404, {"ok": False, "error": "Не найдено"}


# -------------------- aiohttp --------------------
async def start_webapp(bot, host: str, port: int):
    init_agency_extensions()
    from aiohttp import web  # aiohttp ставится вместе с aiogram

    @web.middleware
    async def auth(request, handler):
        if not request.path.startswith("/api/"):
            return await handler(request)
        user = validate_init_data(request.headers.get("X-Init-Data", ""), bot.token)
        if not user:
            return web.json_response({"ok": False, "error": "Откройте приложение из Telegram"}, status=401)
        request["tg_user"] = user
        return await handler(request)

    async def index(request):
        return web.Response(text=INDEX_PATH.read_text(encoding="utf-8"), content_type="text/html",
                            headers={"Cache-Control": "no-store"})

    async def api(request):
        try:
            body = await request.json() if request.can_read_body else {}
            if not isinstance(body, dict):
                body = {}
        except ValueError:
            body = {}
        try:
            status, data = await handle_api(bot, request.path, request["tg_user"], body)
        except Exception:
            log.exception("api error %s", request.path)
            status, data = 500, {"ok": False, "error": "Ошибка сервера"}
        return web.json_response(data, status=status)

    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/healthz", lambda r: web.Response(text="ok"))
    app.router.add_post("/api/{name:.+}", api)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Mini App слушает %s:%s", host, port)
    return runner
