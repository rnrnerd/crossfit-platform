"""
Платформа кроссфит-ивентов — бэкенд.

Отдельный проект: своя база, свой бот. Боевой «Битва за Херсонес»
(crossfit-comp) не затрагивается никак.

Модель: результаты привязаны к ЗАЯВКЕ (entry), а не к человеку — поэтому
командные форматы работают той же логикой, что и личные.

Публичное API (для ТМА):
  GET  /                      — приложение
  GET  /healthz               — health
  GET  /api/me                — профиль текущего пользователя (по Telegram initData)
  GET  /api/events            — каталог событий
  GET  /api/events/{id}       — событие с дивизионами
  GET  /api/events/{id}/leaderboard?division=  — лидерборд
  GET  /api/photo/{user_id}   — фото атлета

Требует авторизации через Telegram initData (заголовок X-Init-Data).
"""

import os
import hmac
import json
import hashlib
import logging
from pathlib import Path
from urllib.parse import parse_qsl
from aiohttp import web
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv()

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

BOT_TOKEN    = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
WEBAPP_URL   = os.getenv("WEBAPP_URL", "").strip()
# на время локальной разработки можно войти без Telegram
DEV_USER_ID  = os.getenv("DEV_TG_ID", "").strip()

USE_DB = bool(DATABASE_URL)
db_pool = None

if USE_DB:
    import asyncpg


# ── Схема ────────────────────────────────────────────────────────────
SCHEMA = """
-- Люди: единый профиль на все события
CREATE TABLE IF NOT EXISTS users (
    id          SERIAL PRIMARY KEY,
    tg_id       BIGINT UNIQUE NOT NULL,
    username    TEXT NOT NULL DEFAULT '',
    name        TEXT NOT NULL DEFAULT '',
    gender      TEXT NOT NULL DEFAULT '',      -- М / Ж
    birth_date  DATE,
    city        TEXT NOT NULL DEFAULT '',
    gym         TEXT NOT NULL DEFAULT '',
    photo       BYTEA,
    photo_v     INTEGER NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- События
CREATE TABLE IF NOT EXISTS events (
    id            SERIAL PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    title         TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    city          TEXT NOT NULL DEFAULT '',
    venue         TEXT NOT NULL DEFAULT '',
    date_start    DATE,
    date_end      DATE,
    status        TEXT NOT NULL DEFAULT 'draft',      -- draft|registration|live|finished
    reg_opens_at  DATE,                               -- окно регистрации
    reg_closes_at DATE,
    qual_start    DATE,                               -- онлайн-отбор, если он есть
    qual_end      DATE,
    instagram     TEXT NOT NULL DEFAULT '',           -- соцсети события
    telegram      TEXT NOT NULL DEFAULT '',
    visibility    TEXT NOT NULL DEFAULT 'public',     -- public|link
    -- логотип/обложка события — это контент (картинка в карточке),
    -- оформление интерфейса всегда единое, платформенное
    logo          BYTEA,
    logo_v        INTEGER NOT NULL DEFAULT 0,
    created_by    INTEGER REFERENCES users(id),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Кто управляет событием
CREATE TABLE IF NOT EXISTS event_staff (
    event_id   INTEGER REFERENCES events(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL DEFAULT 'organizer',     -- organizer|judge
    is_creator BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (event_id, user_id)
);

-- Дивизионы события (вместо захардкоженных категорий)
CREATE TABLE IF NOT EXISTS divisions (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER REFERENCES events(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    team_size   INTEGER NOT NULL DEFAULT 1,           -- 1 = личный зачёт
    gender_rule TEXT NOT NULL DEFAULT 'any',          -- any|male|female|mixed
    age_min     INTEGER,
    age_max     INTEGER,
    max_entries INTEGER,
    -- стоимость участия за заявку: для личного зачёта это цена с человека,
    -- для командного — за команду целиком. 0 = бесплатно
    price       INTEGER NOT NULL DEFAULT 0,
    ord         INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS price INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN IF NOT EXISTS reg_opens_at DATE;
-- в первой версии схемы это был TIMESTAMPTZ: даты уезжали на день из-за часового пояса
ALTER TABLE events ALTER COLUMN reg_closes_at TYPE DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS qual_start DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS qual_end DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS instagram TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN IF NOT EXISTS telegram TEXT NOT NULL DEFAULT '';

-- Заявка: участник соревнования (один атлет или команда)
CREATE TABLE IF NOT EXISTS entries (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER REFERENCES events(id) ON DELETE CASCADE,
    division_id INTEGER REFERENCES divisions(id) ON DELETE CASCADE,
    team_name   TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'active',       -- active|withdrawn
    bib         TEXT NOT NULL DEFAULT '',
    is_finalist BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Состав заявки: 1 строка для личной, N для команды
CREATE TABLE IF NOT EXISTS entry_members (
    entry_id   INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
    is_captain BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (entry_id, user_id)
);

-- Комплексы
CREATE TABLE IF NOT EXISTS wods (
    id            SERIAL PRIMARY KEY,
    event_id      INTEGER REFERENCES events(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    ord           INTEGER NOT NULL DEFAULT 0,
    result_type   TEXT NOT NULL DEFAULT 'time',       -- time|reps|weight
    time_cap      TEXT NOT NULL DEFAULT '',
    day           DATE,
    stage         TEXT NOT NULL DEFAULT 'qualification',  -- qualification|semifinal|final
    points_table  TEXT NOT NULL DEFAULT '',           -- своя шкала баллов
    scoring_note  TEXT NOT NULL DEFAULT ''
);

-- Какие дивизионы выполняют комплекс
CREATE TABLE IF NOT EXISTS wod_divisions (
    wod_id      INTEGER REFERENCES wods(id) ON DELETE CASCADE,
    division_id INTEGER REFERENCES divisions(id) ON DELETE CASCADE,
    PRIMARY KEY (wod_id, division_id)
);

-- Результаты: привязаны к заявке
CREATE TABLE IF NOT EXISTS results (
    entry_id   INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    wod_id     INTEGER REFERENCES wods(id) ON DELETE CASCADE,
    value      DOUBLE PRECISION,
    tiebreak   DOUBLE PRECISION,
    status     TEXT NOT NULL DEFAULT 'ok',            -- ok|dnf|dns|cap
    judged_by  INTEGER REFERENCES users(id),
    judged_at  TIMESTAMPTZ,
    PRIMARY KEY (entry_id, wod_id)
);

-- Заходы
CREATE TABLE IF NOT EXISTS heats (
    id             SERIAL PRIMARY KEY,
    event_id       INTEGER REFERENCES events(id) ON DELETE CASCADE,
    wod_id         INTEGER REFERENCES wods(id) ON DELETE CASCADE,
    number         INTEGER NOT NULL DEFAULT 1,
    day            DATE,
    briefing_start TEXT NOT NULL DEFAULT '',
    briefing_end   TEXT NOT NULL DEFAULT '',
    start_time     TEXT NOT NULL DEFAULT '',
    location       TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS heat_entries (
    heat_id  INTEGER REFERENCES heats(id) ON DELETE CASCADE,
    entry_id INTEGER REFERENCES entries(id) ON DELETE CASCADE,
    lane     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (heat_id, entry_id)
);

-- Расписание
CREATE TABLE IF NOT EXISTS schedule (
    id          SERIAL PRIMARY KEY,
    event_id    INTEGER REFERENCES events(id) ON DELETE CASCADE,
    day         DATE,
    time        TEXT NOT NULL DEFAULT '',
    title       TEXT NOT NULL DEFAULT '',
    location    TEXT NOT NULL DEFAULT '',
    division_id INTEGER REFERENCES divisions(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_entries_event    ON entries(event_id);
CREATE INDEX IF NOT EXISTS idx_results_wod      ON results(wod_id);
CREATE INDEX IF NOT EXISTS idx_members_user     ON entry_members(user_id);
"""


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    async with db_pool.acquire() as c:
        await c.execute(SCHEMA)
    logger.info("Схема готова")


# ── Авторизация через Telegram ───────────────────────────────────────
def verify_init_data(init_data: str):
    """Проверяет подпись Telegram WebApp initData. Возвращает данные пользователя
    или None. Подпись гарантирует, что данные не подделаны."""
    if not init_data or not BOT_TOKEN:
        return None
    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        return None
    received = pairs.pop("hash", None)
    if not received:
        return None
    check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calc = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calc, received):
        return None
    try:
        return json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None


async def current_user(request):
    """Пользователь запроса: заводим профиль при первом входе."""
    tg = verify_init_data(request.headers.get("X-Init-Data", ""))
    tg_id = tg.get("id") if tg else (int(DEV_USER_ID) if DEV_USER_ID else None)
    if not tg_id:
        return None
    name = ""
    username = ""
    if tg:
        name = " ".join(x for x in [tg.get("first_name"), tg.get("last_name")] if x)
        username = tg.get("username") or ""
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if row:
            return dict(row)
        row = await c.fetchrow(
            """INSERT INTO users (tg_id, username, name) VALUES ($1,$2,$3)
               RETURNING *""", tg_id, username, name)
        logger.info("Новый пользователь: %s (%s)", name, tg_id)
        return dict(row)


# ── Хелперы ──────────────────────────────────────────────────────────
def _json(data, status=200):
    resp = web.json_response(data, status=status)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _need_auth():
    return _json({"error": "unauthorized"}, status=401)


def _public_user(u):
    return {"id": u["id"], "name": u["name"], "city": u["city"], "gym": u["gym"],
            "gender": u["gender"],
            "avatar": f"/api/photo/{u['id']}?v={u['photo_v']}" if u.get("photo_v") else ""}


# ── Эндпоинты ────────────────────────────────────────────────────────
async def h_health(r):
    """Показывает, поднялось ли подключение к базе: без этого весь API
    отвечает ошибкой, а по одному «ok» причину не понять."""
    return _json({"status": "ok", "db": db_pool is not None})


async def h_index(r):
    f = BASE_DIR / "index.html"
    if not f.exists():
        return web.Response(text="index.html not found", status=404)
    resp = web.Response(text=f.read_text(encoding="utf-8"), content_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


async def h_me(r):
    u = await current_user(r)
    if not u:
        return _need_auth()
    async with db_pool.acquire() as c:
        # события, где пользователь участвует или организует
        my_entries = await c.fetch(
            """SELECT e.id, e.title, e.slug, e.date_start, d.name AS division
               FROM entry_members m
               JOIN entries en ON en.id = m.entry_id
               JOIN events e   ON e.id = en.event_id
               JOIN divisions d ON d.id = en.division_id
               WHERE m.user_id=$1 AND en.status='active'
               ORDER BY e.date_start DESC NULLS LAST""", u["id"])
        staff_of = await c.fetch(
            """SELECT e.id, e.title, s.role FROM event_staff s
               JOIN events e ON e.id = s.event_id WHERE s.user_id=$1""", u["id"])
    return _json({
        "user": _public_user(u),
        "profile_complete": bool(u["name"] and u["gender"]),
        "my_events": [dict(x) | {"date_start": str(x["date_start"] or "")} for x in my_entries],
        "staff_of": [dict(x) for x in staff_of],
    })


async def h_photo(r):
    if not USE_DB:
        return web.Response(status=404)
    try:
        uid = int(r.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT photo FROM users WHERE id=$1", uid)
    if not row or not row["photo"]:
        return web.Response(status=404)
    resp = web.Response(body=bytes(row["photo"]), content_type="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def h_event_logo(r):
    """Логотип события — контент карточки, оформление интерфейса он не меняет."""
    if not USE_DB:
        return web.Response(status=404)
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT logo FROM events WHERE id=$1", eid)
    if not row or not row["logo"]:
        return web.Response(status=404)
    resp = web.Response(body=bytes(row["logo"]), content_type="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def h_events(r):
    """Каталог событий."""
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT e.id, e.slug, e.title, e.city, e.venue, e.date_start, e.date_end,
                      e.status, e.logo_v,
                      (SELECT COUNT(*) FROM entries en WHERE en.event_id=e.id AND en.status='active') AS entries,
                      (SELECT MIN(price) FROM divisions d WHERE d.event_id=e.id) AS price_min,
                      (SELECT MAX(price) FROM divisions d WHERE d.event_id=e.id) AS price_max
               FROM events e
               WHERE e.visibility='public' AND e.status <> 'draft'
               ORDER BY e.date_start DESC NULLS LAST""")
    return _json([{
        "id": x["id"], "slug": x["slug"], "title": x["title"], "city": x["city"],
        "venue": x["venue"], "status": x["status"], "logo_v": x["logo_v"], "has_logo": bool(x["logo_v"]),
        "date_start": str(x["date_start"] or ""), "date_end": str(x["date_end"] or ""),
        "entries": x["entries"],
        "price_min": x["price_min"], "price_max": x["price_max"],
    } for x in rows])


async def h_event(r):
    """Событие с дивизионами."""
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        ev = await c.fetchrow("SELECT * FROM events WHERE id=$1", eid)
        if not ev:
            return _json({"error": "not_found"}, status=404)
        divs = await c.fetch(
            """SELECT d.*, (SELECT COUNT(*) FROM entries en
                            WHERE en.division_id=d.id AND en.status='active') AS entries
               FROM divisions d WHERE d.event_id=$1 ORDER BY d.ord, d.id""", eid)
        # атлетов, а не заявок: в командном дивизионе одна заявка = несколько человек
        athletes = await c.fetchval(
            """SELECT COUNT(DISTINCT m.user_id) FROM entry_members m
               JOIN entries en ON en.id = m.entry_id
               WHERE en.event_id=$1 AND en.status='active'""", eid)
        entries = await c.fetchval(
            "SELECT COUNT(*) FROM entries WHERE event_id=$1 AND status='active'", eid)
    return _json({
        "id": ev["id"], "slug": ev["slug"], "title": ev["title"],
        "description": ev["description"], "city": ev["city"], "venue": ev["venue"],
        "date_start": str(ev["date_start"] or ""), "date_end": str(ev["date_end"] or ""),
        "reg_opens_at": str(ev["reg_opens_at"] or ""), "reg_closes_at": str(ev["reg_closes_at"] or ""),
        "qual_start": str(ev["qual_start"] or ""), "qual_end": str(ev["qual_end"] or ""),
        "instagram": ev["instagram"], "telegram": ev["telegram"],
        "has_logo": bool(ev["logo_v"]), "logo_v": ev["logo_v"],
        "athletes": athletes, "entries": entries,
        "status": ev["status"],
        "divisions": [{"id": d["id"], "name": d["name"], "team_size": d["team_size"],
                       "gender_rule": d["gender_rule"], "max_entries": d["max_entries"],
                       "price": d["price"], "entries": d["entries"]} for d in divs],
    })


# ── Сборка приложения ────────────────────────────────────────────────
@web.middleware
async def db_guard(r, handler):
    """Без базы каждый обработчик падает на db_pool=None и отдаёт голый 500,
    из которого не видно причины. Отвечаем прямо."""
    if r.path.startswith("/api/") and db_pool is None:
        return _json({"error": "database_unavailable",
                      "hint": "DATABASE_URL не задан или база недоступна"}, status=503)
    return await handler(r)


def build_web_app():
    app = web.Application(client_max_size=32 * 1024 * 1024,
                          middlewares=[db_guard])
    app.router.add_get("/", h_index)
    app.router.add_get("/healthz", h_health)
    app.router.add_get("/api/me", h_me)
    app.router.add_get("/api/photo/{id}", h_photo)
    app.router.add_get("/api/event-logo/{id}", h_event_logo)
    app.router.add_get("/api/events", h_events)
    app.router.add_get("/api/events/{id}", h_event)
    assets = BASE_DIR / "assets"
    if assets.exists():
        app.router.add_static("/assets/", assets, show_index=False)
    return app


def main():
    port = int(os.environ.get("PORT", "8080"))

    async def on_startup(app):
        if USE_DB:
            await init_db()
        else:
            logger.warning("DATABASE_URL не задан — API работать не будет")

    app = build_web_app()
    app.on_startup.append(on_startup)
    logger.info("Платформа: сервер на порту %s", port)
    web.run_app(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
