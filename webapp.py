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

    # ---- только для админов ----
    if not admin:
        return 403, {"ok": False, "error": "Нет доступа"}

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
        return 200, {"ok": "error" not in res, "result": res}

    return 404, {"ok": False, "error": "Не найдено"}


# -------------------- aiohttp --------------------
async def start_webapp(bot, host: str, port: int):
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
