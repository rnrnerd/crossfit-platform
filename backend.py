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
  PATCH  /api/me                    — сохранение профиля
  POST   /api/events/{id}/entry     — подать заявку
  DELETE /api/events/{id}/entry     — снять заявку
  GET  /api/events/{id}/leaderboard?division=  — протокол дивизиона
  GET  /api/events/{id}/wods        — комплексы
  GET  /api/events/{id}/schedule    — расписание
  GET  /api/events/{id}/heats       — заходы со старт-листом

Админка (заголовок X-Admin-Password, выключена без ADMIN_PASSWORD):
  GET  /admin                       — страница
  /api/admin/events[/{id}]          — события, дивизионы, афиши
  /api/admin/events/{id}/entries    — заявки
  /api/admin/news[/{id}]            — новости
  /api/admin/clubs[/{id}]           — клубы
  GET  /api/photo/{user_id}   — фото атлета
  GET  /api/event-banner/{id} — афиша события 16:9
  GET  /api/event-mark/{id}   — квадратный знак события

Требует авторизации через Telegram initData (заголовок X-Init-Data).
"""

import os
import hmac
import json
import asyncio
import base64
import hashlib
import logging
from pathlib import Path
from datetime import date
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
# Админка. Пароль не задан — она выключена целиком: пустой пароль пустил бы всех.
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()

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

-- Клубы: к какому залу относится атлет. NULL у пользователя = независимый атлет
CREATE TABLE IF NOT EXISTS clubs (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    city      TEXT NOT NULL DEFAULT '',
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

-- Новости российского кроссфита. Featured уходят в слайдер на главной
CREATE TABLE IF NOT EXISTS news (
    id           SERIAL PRIMARY KEY,
    title        TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    body         TEXT NOT NULL DEFAULT '',
    source_url   TEXT NOT NULL DEFAULT '',
    image        BYTEA,
    image_v      INTEGER NOT NULL DEFAULT 0,
    is_featured  BOOLEAN NOT NULL DEFAULT FALSE,
    -- откуда новость: ru — российский кроссфит, world — мировой.
    -- Словарь закрытый: на вкладках не должно появляться чужих формулировок
    category     TEXT NOT NULL DEFAULT '',          -- ru|world|''
    published_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Медиа-партнёр раздела новостей. Строка активна одна за раз
CREATE TABLE IF NOT EXISTS partners (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    tagline     TEXT NOT NULL DEFAULT '',      -- «Голос кроссфита»
    url         TEXT NOT NULL DEFAULT '',      -- ссылка на канал
    audience    TEXT NOT NULL DEFAULT '',      -- «9 495» — показываем как есть
    logo        BYTEA,
    logo_v      INTEGER NOT NULL DEFAULT 0,
    is_active   BOOLEAN NOT NULL DEFAULT TRUE
);

-- Переходы по каждому размещению отдельно: без этого не понять, какое
-- место работает, и не с чем идти к партнёру продлевать
CREATE TABLE IF NOT EXISTS partner_clicks (
    partner_id INTEGER REFERENCES partners(id) ON DELETE CASCADE,
    place      TEXT NOT NULL,                  -- header|article|feed
    clicks     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (partner_id, place)
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
    -- картинки события — это контент, оформление интерфейса всегда единое.
    -- banner  — широкая афиша 16:9, идёт во всю ширину экрана
    -- mark    — квадратный знак 1:1, показывается кругом рядом с названием
    banner        BYTEA,
    banner_v      INTEGER NOT NULL DEFAULT 0,
    mark          BYTEA,
    mark_v        INTEGER NOT NULL DEFAULT 0,
    -- турнир может проводиться во внешнем соревновательном модуле
    -- (crossfit-comp): тогда протокол, комплексы, расписание и заходы
    -- читаются оттуда, а не из своих таблиц
    feed_url      TEXT NOT NULL DEFAULT '',
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
    -- уровень подготовки: платформа показывает его плашкой на превью старта.
    -- Словарь закрытый, иначе на превью поедет зоопарк из текста организатора
    level       TEXT NOT NULL DEFAULT '',             -- sc|bg|rx|elite|''
    age_min     INTEGER,
    age_max     INTEGER,
    max_entries INTEGER,
    -- стоимость участия за заявку: для личного зачёта это цена с человека,
    -- для командного — за команду целиком. 0 = бесплатно
    price       INTEGER NOT NULL DEFAULT 0,
    ord         INTEGER NOT NULL DEFAULT 0
);
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS price INTEGER NOT NULL DEFAULT 0;
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS level TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN IF NOT EXISTS feed_url TEXT NOT NULL DEFAULT '';
ALTER TABLE news ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT '';
-- во внешнем модуле группа задаётся парой «категория + пол» вместо id дивизиона
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS feed_category TEXT NOT NULL DEFAULT '';
ALTER TABLE divisions ADD COLUMN IF NOT EXISTS feed_gender TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN IF NOT EXISTS reg_opens_at DATE;
-- в первой версии схемы это был TIMESTAMPTZ: даты уезжали на день из-за часового пояса
ALTER TABLE events ALTER COLUMN reg_closes_at TYPE DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS qual_start DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS qual_end DATE;
ALTER TABLE events ADD COLUMN IF NOT EXISTS instagram TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN IF NOT EXISTS telegram TEXT NOT NULL DEFAULT '';
-- одна картинка logo играла две роли сразу: и афишу, и знак. Развели:
-- широкая афиша осталась в banner, квадратный знак приехал в mark.
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns
               WHERE table_name='events' AND column_name='logo') THEN
        ALTER TABLE events RENAME COLUMN logo TO banner;
        ALTER TABLE events RENAME COLUMN logo_v TO banner_v;
    END IF;
END $$;
ALTER TABLE events ADD COLUMN IF NOT EXISTS banner BYTEA;
ALTER TABLE events ADD COLUMN IF NOT EXISTS banner_v INTEGER NOT NULL DEFAULT 0;
ALTER TABLE events ADD COLUMN IF NOT EXISTS mark BYTEA;
ALTER TABLE events ADD COLUMN IF NOT EXISTS mark_v INTEGER NOT NULL DEFAULT 0;
-- параметры атлета: нужны организатору для дивизионов и протокола
ALTER TABLE users ADD COLUMN IF NOT EXISTS height_cm INTEGER;
ALTER TABLE users ADD COLUMN IF NOT EXISTS weight_kg REAL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS club_id INTEGER REFERENCES clubs(id);

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


def _public_user(u, club=None):
    return {"id": u["id"], "name": u["name"], "city": u["city"], "gym": u["gym"],
            "gender": u["gender"],
            "height_cm": u.get("height_cm"), "weight_kg": u.get("weight_kg"),
            # club_id = NULL означает «независимый атлет», а не «не заполнено»
            "club_id": u.get("club_id"), "club": club,
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
            """SELECT e.id, e.title, e.slug, e.date_start, e.date_end, e.status,
                      e.mark_v, e.reg_closes_at, d.name AS division
               FROM entry_members m
               JOIN entries en ON en.id = m.entry_id
               JOIN events e   ON e.id = en.event_id
               JOIN divisions d ON d.id = en.division_id
               WHERE m.user_id=$1 AND en.status='active'
               ORDER BY e.date_start DESC NULLS LAST""", u["id"])
        staff_of = await c.fetch(
            """SELECT e.id, e.title, s.role FROM event_staff s
               JOIN events e ON e.id = s.event_id WHERE s.user_id=$1""", u["id"])
        club = await c.fetchval(
            "SELECT name FROM clubs WHERE id=$1", u["club_id"]) if u.get("club_id") else None
    return _json({
        "user": _public_user(u, club),
        "profile_complete": bool(u["name"] and u["gender"]),
        "my_events": [dict(x) | {
            "date_start": str(x["date_start"] or ""),
            "date_end": str(x["date_end"] or ""),
            "reg_closes_at": str(x["reg_closes_at"] or ""),
            "has_mark": bool(x["mark_v"]),
        } for x in my_entries],
        "staff_of": [dict(x) for x in staff_of],
    })


GENDERS = ("", "М", "Ж")


def _clean(v, limit):
    return " ".join(str(v or "").split())[:limit]


async def h_me_save(r):
    """Сохранение профиля. Имя и пол обязательны для заявки, поэтому
    валидируем их здесь, а не только в форме."""
    u = await current_user(r)
    if not u:
        return _need_auth()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)

    name = _clean(body.get("name"), 80)
    gender = str(body.get("gender") or "")
    city = _clean(body.get("city"), 80)
    gym = _clean(body.get("gym"), 80)

    errors = {}
    if name and len(name) < 2:
        errors["name"] = "Слишком короткое имя"
    if gender not in GENDERS:
        errors["gender"] = "Выберите пол из списка"

    def num(key, lo, hi, label, cast):
        raw = body.get(key)
        if raw in (None, "", "-"):
            return None
        try:
            v = cast(str(raw).replace(",", "."))
        except (TypeError, ValueError):
            errors[key] = f"{label} — только число"
            return None
        if not (lo <= v <= hi):
            errors[key] = f"{label} от {lo} до {hi}"
            return None
        return v

    height = num("height_cm", 120, 250, "Рост", int)
    weight = num("weight_kg", 30, 250, "Вес", float)

    club_id = body.get("club_id")
    club_id = int(club_id) if str(club_id or "").isdigit() else None

    if errors:
        return _json({"error": "invalid", "fields": errors}, status=400)

    async with db_pool.acquire() as c:
        if club_id is not None and not await c.fetchval(
                "SELECT 1 FROM clubs WHERE id=$1 AND is_active", club_id):
            return _json({"error": "invalid",
                          "fields": {"club_id": "Клуб не найден"}}, status=400)
        row = await c.fetchrow(
            """UPDATE users SET name=$2, gender=$3, city=$4, gym=$5,
                                height_cm=$6, weight_kg=$7, club_id=$8
               WHERE id=$1 RETURNING *""",
            u["id"], name, gender, city, gym, height, weight, club_id)
        club = await c.fetchval("SELECT name FROM clubs WHERE id=$1", club_id) if club_id else None
    return _json({"user": _public_user(dict(row), club),
                  "profile_complete": bool(row["name"] and row["gender"])})


async def h_me_photo(r):
    """Фото атлета. Принимаем dataURL: браузер уже ужал картинку до 512px,
    поэтому multipart тут не нужен."""
    u = await current_user(r)
    if not u:
        return _need_auth()
    try:
        body = await r.json()
        raw = str(body.get("data") or "")
        head, b64 = raw.split(",", 1)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if "image/jpeg" not in head and "image/png" not in head:
        return _json({"error": "bad_format"}, status=400)
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if not blob or len(blob) > 2_000_000:
        return _json({"error": "too_big"}, status=400)
    async with db_pool.acquire() as c:
        v = await c.fetchval(
            """UPDATE users SET photo=$2, photo_v=photo_v+1
               WHERE id=$1 RETURNING photo_v""", u["id"], blob)
    return _json({"avatar": f"/api/photo/{u['id']}?v={v}"})


PARTNER_PLACES = ("header", "article", "feed")


async def h_partner(r):
    """Активный медиа-партнёр раздела новостей."""
    async with db_pool.acquire() as c:
        x = await c.fetchrow(
            "SELECT * FROM partners WHERE is_active ORDER BY id LIMIT 1")
    if not x:
        return _json(None)
    return _json({
        "id": x["id"], "name": x["name"], "tagline": x["tagline"],
        "url": x["url"], "audience": x["audience"],
        "has_logo": bool(x["logo_v"]), "logo_v": x["logo_v"],
    })


async def h_partner_click(r):
    """Счётчик переходов. Отвечаем сразу: интерфейс не должен ждать статистику."""
    try:
        body = await r.json()
        pid = int(body.get("id"))
        place = str(body.get("place") or "")
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if place not in PARTNER_PLACES:
        return _json({"error": "bad_request"}, status=400)
    async with db_pool.acquire() as c:
        await c.execute(
            """INSERT INTO partner_clicks (partner_id, place, clicks) VALUES ($1,$2,1)
               ON CONFLICT (partner_id, place)
               DO UPDATE SET clicks = partner_clicks.clicks + 1""", pid, place)
    return _json({"ok": True})


async def h_partner_logo(r):
    if not USE_DB:
        return web.Response(status=404)
    try:
        pid = int(r.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT logo FROM partners WHERE id=$1", pid)
    if not row or not row["logo"]:
        return web.Response(status=404)
    blob = bytes(row["logo"])
    ctype = "image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    resp = web.Response(body=blob, content_type=ctype)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def a_partners(r):
    """Партнёры и переходы по размещениям — то, что показывают при продлении."""
    if not _admin_ok(r):
        return _need_admin()
    async with db_pool.acquire() as c:
        rows = await c.fetch("SELECT * FROM partners ORDER BY id")
        clicks = await c.fetch("SELECT * FROM partner_clicks")
    by_id = {}
    for x in clicks:
        by_id.setdefault(x["partner_id"], {})[x["place"]] = x["clicks"]
    return _json([{
        "id": p["id"], "name": p["name"], "tagline": p["tagline"], "url": p["url"],
        "audience": p["audience"], "is_active": p["is_active"],
        "has_logo": bool(p["logo_v"]),
        "clicks": by_id.get(p["id"], {}),
        "clicks_total": sum(by_id.get(p["id"], {}).values()),
    } for p in rows])


async def a_partner_save(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)
    pid = r.match_info.get("id")
    name = _clean(body.get("name"), 80)
    if len(name) < 2:
        return _json({"error": "invalid", "fields": {"name": "Нужно название"}}, status=400)
    tagline = _clean(body.get("tagline"), 120)
    url = _clean(body.get("url"), 300)
    audience = _clean(body.get("audience"), 40)
    active = bool(body.get("is_active", True))
    async with db_pool.acquire() as c:
        if pid:
            row = await c.fetchrow(
                """UPDATE partners SET name=$2, tagline=$3, url=$4, audience=$5, is_active=$6
                   WHERE id=$1 RETURNING id""", int(pid), name, tagline, url, audience, active)
            if not row:
                return _json({"error": "not_found"}, status=404)
            return _json({"ok": True, "id": row["id"]})
        new_id = await c.fetchval(
            """INSERT INTO partners (name, tagline, url, audience, is_active)
               VALUES ($1,$2,$3,$4,$5) RETURNING id""", name, tagline, url, audience, active)
    return _json({"ok": True, "id": new_id})


async def a_partner_logo(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        pid = int(r.match_info["id"])
        body = await r.json()
        head, b64 = str(body.get("data") or "").split(",", 1)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if "image/jpeg" not in head and "image/png" not in head:
        return _json({"error": "bad_format"}, status=400)
    blob = base64.b64decode(b64, validate=True)
    if not blob or len(blob) > 2_000_000:
        return _json({"error": "too_big"}, status=400)
    async with db_pool.acquire() as c:
        v = await c.fetchval(
            "UPDATE partners SET logo=$2, logo_v=logo_v+1 WHERE id=$1 RETURNING logo_v", pid, blob)
    if v is None:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "v": v})


async def h_clubs(r):
    """Справочник клубов. «Независимый атлет» — не строка в базе,
    а отсутствие клуба, поэтому в список его не кладём."""
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            "SELECT id, name, city FROM clubs WHERE is_active ORDER BY name")
    return _json([dict(x) for x in rows])


async def h_news(r):
    """Лента новостей. featured=1 — только те, что идут в слайдер на главной."""
    featured = r.query.get("featured") == "1"
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            f"""SELECT id, title, summary, source_url, image_v, is_featured,
                       category, published_at
                FROM news {'WHERE is_featured' if featured else ''}
                ORDER BY published_at DESC LIMIT 40""")
    return _json([{
        "id": x["id"], "title": x["title"], "summary": x["summary"],
        "source_url": x["source_url"], "is_featured": x["is_featured"],
        "category": x["category"],
        "has_image": bool(x["image_v"]), "image_v": x["image_v"],
        "published_at": x["published_at"].isoformat(),
    } for x in rows])


async def h_news_one(r):
    try:
        nid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        x = await c.fetchrow("SELECT * FROM news WHERE id=$1", nid)
    if not x:
        return _json({"error": "not_found"}, status=404)
    return _json({
        "id": x["id"], "title": x["title"], "summary": x["summary"], "body": x["body"],
        "source_url": x["source_url"], "category": x["category"],
        "has_image": bool(x["image_v"]), "image_v": x["image_v"],
        "published_at": x["published_at"].isoformat(),
    })


async def h_news_image(r):
    if not USE_DB:
        return web.Response(status=404)
    try:
        nid = int(r.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow("SELECT image FROM news WHERE id=$1", nid)
    if not row or not row["image"]:
        return web.Response(status=404)
    resp = web.Response(body=bytes(row["image"]), content_type="image/jpeg")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def _my_entry(c, event_id, user_id):
    """Активная заявка пользователя на событие, если она есть."""
    return await c.fetchrow(
        """SELECT en.id, en.division_id, en.team_name, d.name AS division
           FROM entries en
           JOIN entry_members m ON m.entry_id = en.id
           JOIN divisions d ON d.id = en.division_id
           WHERE en.event_id=$1 AND m.user_id=$2 AND en.status='active'""",
        event_id, user_id)


async def h_entry_create(r):
    """Подача заявки. Все проверки делаем на сервере: форма может врать."""
    u = await current_user(r)
    if not u:
        return _need_auth()
    if not (u["name"] and u["gender"]):
        return _json({"error": "profile_incomplete"}, status=409)
    try:
        eid = int(r.match_info["id"])
        body = await r.json()
        did = int(body.get("division_id"))
    except (ValueError, TypeError, KeyError):
        return _json({"error": "bad_request"}, status=400)

    async with db_pool.acquire() as c:
        ev = await c.fetchrow("SELECT * FROM events WHERE id=$1", eid)
        if not ev:
            return _json({"error": "not_found"}, status=404)
        if ev["status"] != "registration":
            return _json({"error": "registration_closed"}, status=409)
        if ev["reg_closes_at"] and ev["reg_closes_at"] < date.today():
            return _json({"error": "registration_closed"}, status=409)

        d = await c.fetchrow(
            "SELECT * FROM divisions WHERE id=$1 AND event_id=$2", did, eid)
        if not d:
            return _json({"error": "division_not_found"}, status=404)
        # командные форматы требуют приглашения партнёра — отдельный сценарий
        if d["team_size"] > 1:
            return _json({"error": "team_division"}, status=409)
        if d["gender_rule"] in ("male", "female"):
            want = "М" if d["gender_rule"] == "male" else "Ж"
            if u["gender"] != want:
                return _json({"error": "gender_mismatch"}, status=409)

        if await _my_entry(c, eid, u["id"]):
            return _json({"error": "already_registered"}, status=409)

        if d["max_entries"]:
            taken = await c.fetchval(
                """SELECT COUNT(*) FROM entries
                   WHERE division_id=$1 AND status='active'""", did)
            if taken >= d["max_entries"]:
                return _json({"error": "division_full"}, status=409)

        async with c.transaction():
            entry_id = await c.fetchval(
                """INSERT INTO entries (event_id, division_id) VALUES ($1,$2)
                   RETURNING id""", eid, did)
            await c.execute(
                """INSERT INTO entry_members (entry_id, user_id, is_captain)
                   VALUES ($1,$2,TRUE)""", entry_id, u["id"])
        logger.info("Заявка %s: событие %s, дивизион %s, атлет %s",
                    entry_id, eid, did, u["id"])
    return _json({"entry_id": entry_id, "division_id": did, "division": d["name"]})


async def h_entry_withdraw(r):
    """Снятие заявки. Запись не удаляем — она нужна в истории события."""
    u = await current_user(r)
    if not u:
        return _need_auth()
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_request"}, status=400)

    async with db_pool.acquire() as c:
        ev = await c.fetchrow("SELECT status FROM events WHERE id=$1", eid)
        if not ev:
            return _json({"error": "not_found"}, status=404)
        if ev["status"] != "registration":
            return _json({"error": "too_late"}, status=409)
        entry = await _my_entry(c, eid, u["id"])
        if not entry:
            return _json({"error": "no_entry"}, status=404)
        await c.execute(
            "UPDATE entries SET status='withdrawn' WHERE id=$1", entry["id"])
        logger.info("Заявка %s снята атлетом %s", entry["id"], u["id"])
    return _json({"ok": True})


# ── Внешний соревновательный модуль ──────────────────────────────────
# Турнир может идти в отдельном сервисе (crossfit-comp) со своей админкой.
# Платформа читает его по HTTP и приводит ответы к своей форме, поэтому
# экраны не знают, откуда пришли данные, и боевой модуль не трогается.
#
# Модуль односоставный: один сервис = одно соревнование, группа задаётся
# парой «категория + пол», а не id дивизиона. Сопоставление лежит
# в divisions.feed_category / feed_gender.

FEED_TTL = 15          # живой старт: чаще опрашивать модуль незачем
FEED_TIMEOUT = 6
_feed_cache = {}       # base_url → (истекает, снимок)


def _homoglyphs():
    # латинские буквы, визуально неотличимые от кириллических: в выгрузках
    # они встречаются вперемешку
    return str.maketrans({
        "a": "а", "b": "в", "c": "с", "e": "е", "h": "н", "k": "к", "m": "м",
        "o": "о", "p": "р", "t": "т", "u": "и", "x": "х", "y": "у",
    })


_HG = _homoglyphs()


def norm_name(x):
    """Нормализация ФИО для сопоставления.

    Единственное место, где приходится узнавать человека по тексту: у модуля
    нет id пользователей, атлеты в нём — строки. Внутри платформы такого слоя
    нет и быть не должно.
    """
    import unicodedata
    x = unicodedata.normalize("NFC", x or "")
    x = x.replace("\u200b", " ")
    for ch in "\u200c\u200d\u200e\u200f\ufeff\u00ad":
        x = x.replace(ch, "")
    x = x.lower().replace("ё", "е").translate(_HG)
    x = "".join(c if (c.isalpha() or c.isspace()) else " " for c in x)
    return " ".join(x.split())


def name_keys(x):
    """Ключи сопоставления: порядок слов в базах разный («Иванов Иван» и «Иван Иванов»)."""
    n = norm_name(x)
    parts = n.split()
    keys = {n}
    if len(parts) >= 2:
        keys.add(" ".join(sorted(parts[:2])))
        keys.add(" ".join(sorted(parts)))
    return keys


def _same_person(a, b):
    return bool(name_keys(a) & name_keys(b))


async def feed_snapshot(base):
    """Все четыре ответа модуля одним снимком, с общим кэшем.

    Экран события спрашивает и содержимое вкладок, и сами вкладки, поэтому
    дёргать модуль по частям на каждый запрос нельзя: во время старта в него
    смотрят все участники сразу.
    """
    import time as _t
    base = base.rstrip("/")
    hit = _feed_cache.get(base)
    if hit and hit[0] > _t.monotonic():
        return hit[1]

    import aiohttp
    paths = {"leaderboard": "/api/leaderboard", "wods": "/api/wods",
             "schedule": "/api/schedule", "heats": "/api/heats"}
    snap = {"ok": False, "error": "", "base": base, "leaderboard": [], "wods": [],
            "schedule": [], "heats": []}
    timeout = aiohttp.ClientTimeout(total=FEED_TIMEOUT)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async def one(key, path):
                async with sess.get(base + path) as r:
                    if r.status != 200:
                        raise RuntimeError(f"{path} → {r.status}")
                    return key, await r.json(content_type=None)
            got = await asyncio.gather(*(one(k, v) for k, v in paths.items()))
        for k, v in got:
            snap[k] = v if isinstance(v, list) else []
        snap["ok"] = True
    except Exception as e:
        snap["error"] = str(e)[:200]
        logger.warning("Модуль %s недоступен: %s", base, snap["error"])
        # неудачу кэшируем коротко, чтобы не выстраивать очередь к лежащему модулю
        _feed_cache[base] = (_t.monotonic() + 5, snap)
        return snap

    _feed_cache[base] = (_t.monotonic() + FEED_TTL, snap)
    return snap


def _feed_divisions(divs, category, gender):
    """Дивизионы платформы по ключу модуля.

    Ключ бывает неполным: комплекс «для категории Продвинутые» без указания
    пола относится и к мужскому, и к женскому дивизиону. Поэтому пустая часть
    ключа означает «любой», а не «пустая строка».
    Дивизионы без сопоставления не подтягиваем: они к модулю не относятся.
    """
    return [d["name"] for d in divs
            if d["feed_category"]
            and (not category or d["feed_category"] == category)
            and (not gender or d["feed_gender"] == gender)]


def _feed_group(divs, category, gender):
    """Одно название для строки расписания или старт-листа."""
    names = _feed_divisions(divs, category, gender)
    if names:
        return ", ".join(names)
    return " ".join(x for x in [category, gender] if x)


def feed_leaderboard(snap, division, me_name):
    """Протокол модуля → форма платформы.

    У модуля свой формат разбивки: список комплексов с местом и баллами.
    Идентификатора заявки там нет, поэтому «это вы» проставляем по имени.
    """
    cat, gen = division["feed_category"], division["feed_gender"]
    rows_in = [r for r in snap["leaderboard"]
               if (r.get("category") or "") == cat and (r.get("gender") or "") == gen]
    rows = []
    for i, r in enumerate(rows_in):
        wods = r.get("wods")
        per = []
        if isinstance(wods, list):
            for w in wods:
                per.append({
                    "wod_id": None, "name": w.get("name", ""),
                    "result_type": w.get("result_type", ""),
                    "value": w.get("result"), "tiebreak": None,
                    "status": "ok" if w.get("result") is not None else "",
                    "place": w.get("place"), "points": w.get("points", 0),
                })
        rows.append({
            "entry_id": None,
            "name": r.get("name", ""),
            "members": [],
            "avatar": _feed_asset(snap, r.get("avatar", "")),
            "points": r.get("points", 0),
            "place": i + 1,
            "wods": per,
            "is_me": bool(me_name and _same_person(me_name, r.get("name", ""))),
        })
    total = len({w["name"] for r in rows for w in r["wods"] if w["name"]})
    done = len({w["name"] for r in rows for w in r["wods"] if w["place"]})
    return {"division": {"id": division["id"], "name": division["name"],
                         "team_size": division["team_size"]},
            "wods_total": total, "wods_done": done, "rows": rows}


def _feed_asset(snap, url):
    """Ссылки на фото модуль отдаёт относительными — дописываем его адрес."""
    base = snap.get("base", "")
    if not url:
        return ""
    return url if url.startswith("http") else base + url


def feed_wods(snap, divs):
    out = []
    for w in snap["wods"]:
        cats = w.get("categories") or []
        gens = w.get("genders") or []
        names = []
        if cats or gens:
            for c in (cats or [""]):
                for g in (gens or [""]):
                    names.extend(_feed_divisions(divs, c, g))
            # ключ мог никуда не сопоставиться — показываем как в модуле
            if not names:
                names = [" ".join(x for x in [c, g] if x)
                         for c in (cats or [""]) for g in (gens or [""])]
        out.append({
            "id": w.get("id"), "ord": w.get("order", 0), "name": w.get("name", ""),
            "description": w.get("description", ""),
            "result_type": w.get("result_type", ""),
            "time_cap": w.get("time_cap", ""), "stage": "",
            # у модуля формат подсчёта — готовая строка («AMRAP 10 мин»)
            "scoring_note": w.get("scoring", ""),
            "day": w.get("day", ""),
            "divisions": [{"id": None, "name": n} for n in dict.fromkeys(names)],
        })
    return out


def feed_schedule(snap, divs):
    return [{
        "id": s.get("id"), "day": s.get("day", ""), "time": s.get("time", ""),
        "title": s.get("title", ""), "location": s.get("location", ""),
        "division": (_feed_group(divs, s.get("category"), s.get("gender"))
                     if (s.get("category") or s.get("gender")) else ""),
    } for s in snap["schedule"]]


def feed_heats(snap, divs, me_name):
    out = []
    for h in snap["heats"]:
        group = _feed_group(divs, h.get("category"), h.get("gender"))
        out.append({
            "id": h.get("id"), "number": h.get("heat", 0), "wod": h.get("wod", ""),
            "wod_id": None, "day": h.get("day", ""),
            "briefing_start": h.get("briefing_start", ""),
            "briefing_end": h.get("briefing_end", ""),
            "start_time": h.get("start_time", ""), "location": h.get("location", ""),
            "entries": [{
                "lane": a.get("lane", 0), "entry_id": None,
                "name": a.get("name", ""), "members": [],
                "division": a.get("category") or group,
                "avatar": _feed_asset(snap, a.get("avatar", "")),
                "is_me": bool(me_name and _same_person(me_name, a.get("name", ""))),
            } for a in (h.get("athletes") or [])],
        })
    return out


async def _event_divs_for_feed(c, event_id):
    return [dict(x) for x in await c.fetch(
        """SELECT id, name, team_size, feed_category, feed_gender
           FROM divisions WHERE event_id=$1 ORDER BY ord, id""", event_id)]


# ── Подсчёт протокола ────────────────────────────────────────────────
# Логика перенесена из соревновательного модуля (crossfit-comp) без изменений
# по смыслу: место в комплексе → баллы за место → сумма по комплексам.
# Слой сопоставления атлетов по ФИО не переносился: здесь заходы и результаты
# ссылаются на заявку по id, поэтому угадывать человека по тексту не нужно.

def parse_points_table(s):
    """«100,90,80,…» → [100, 90, 80, …]. Пусто или мусор → None (обычная формула)."""
    vals = []
    for part in str(s or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            vals.append(max(0, int(float(part))))
        except ValueError:
            return None
    return vals or None


def place_points(place, table=None):
    """Баллы за место: своя шкала комплекса, иначе формула с шагом 5."""
    if table:
        return table[place - 1] if 1 <= place <= len(table) else 0
    return max(0, 100 - 5 * (place - 1))


# Незачёт ранжируется после всех, кто комплекс закончил: так принято
# в протоколах — финишировавший всегда выше нефинишировавшего.
DNF_STATUSES = ("dnf", "dns", "cap")


def rank_places(rows, direction):
    """rows: [(entry_id, value, tiebreak, status)] → {entry_id: место}.

    Равные результаты делят лучшее место (1, 2, 2, 4). Тай-брейк разбивает
    равенство до деления места: меньше — лучше, это всегда время.
    """
    done = [r for r in rows if r[3] == "ok" and r[1] is not None]
    failed = [r for r in rows if r not in done]
    rev = direction == "desc"

    def key(r):
        tb = r[2]
        # тай-брейк всегда «меньше — лучше», поэтому знак не переворачиваем
        return (-r[1] if rev else r[1], float("inf") if tb is None else tb)

    done.sort(key=key)
    out, idx, n = {}, 0, len(done)
    while idx < n:
        j = idx
        while j < n and key(done[j]) == key(done[idx]):
            j += 1
        for k in range(idx, j):
            out[done[k][0]] = idx + 1
        idx = j
    # все незакончившие делят одно место сразу после финишировавших
    for r in failed:
        out[r[0]] = n + 1
    return out


async def _division_table(c, event_id, division_id):
    """Комплексы дивизиона, заявки и посчитанные баллы."""
    wods = await c.fetch(
        """SELECT w.* FROM wods w
           WHERE w.event_id = $1
             AND (NOT EXISTS (SELECT 1 FROM wod_divisions wd WHERE wd.wod_id = w.id)
                  OR EXISTS (SELECT 1 FROM wod_divisions wd
                             WHERE wd.wod_id = w.id AND wd.division_id = $2))
           ORDER BY w.ord, w.id""", event_id, division_id)

    entries = await c.fetch(
        """SELECT en.id, en.team_name,
                  ARRAY_AGG(u.name ORDER BY m.is_captain DESC, u.name) AS members,
                  MIN(u.id) FILTER (WHERE u.photo_v > 0) AS photo_user,
                  MIN(u.photo_v) FILTER (WHERE u.photo_v > 0) AS photo_v
           FROM entries en
           JOIN entry_members m ON m.entry_id = en.id
           JOIN users u ON u.id = m.user_id
           WHERE en.division_id = $1 AND en.status = 'active'
           GROUP BY en.id, en.team_name""", division_id)
    ids = [e["id"] for e in entries]
    if not ids:
        return wods, entries, {}, {}, {}

    res = {e: {} for e in ids}
    for row in await c.fetch(
            """SELECT entry_id, wod_id, value, tiebreak, status FROM results
               WHERE entry_id = ANY($1::int[])""", ids):
        res[row["entry_id"]][row["wod_id"]] = dict(row)

    points = {e: {} for e in ids}
    places = {e: {} for e in ids}
    for w in wods:
        table = parse_points_table(w["points_table"])
        direction = "asc" if w["result_type"] == "time" else "desc"
        rows = [(e, res[e][w["id"]]["value"], res[e][w["id"]]["tiebreak"],
                 res[e][w["id"]]["status"])
                for e in ids if w["id"] in res[e]]
        for eid, place in rank_places(rows, direction).items():
            places[eid][w["id"]] = place
            points[eid][w["id"]] = place_points(place, table)
    return wods, entries, res, points, places


async def h_leaderboard(r):
    """Протокол дивизиона. Без дивизиона считать нечего: у каждого свой набор
    комплексов, а место имеет смысл только внутри своей группы."""
    try:
        eid = int(r.match_info["id"])
        did = int(r.query.get("division", ""))
    except ValueError:
        return _json({"error": "division_required"}, status=400)

    async with db_pool.acquire() as c:
        d = await c.fetchrow(
            """SELECT id, name, team_size, feed_category, feed_gender
               FROM divisions WHERE id=$1 AND event_id=$2""", did, eid)
        if not d:
            return _json({"error": "division_not_found"}, status=404)
        feed = await c.fetchval("SELECT feed_url FROM events WHERE id=$1", eid)
        me = await current_user(r)
        if feed:
            snap = await feed_snapshot(feed)
            if not snap["ok"]:
                return _json({"error": "feed_unavailable"}, status=503)
            return _json(feed_leaderboard(snap, dict(d), me["name"] if me else ""))
        wods, entries, res, points, places = await _division_table(c, eid, did)
        mine = await _my_entry(c, eid, me["id"]) if me else None
        mine_entry = mine["id"] if mine else None

        rows = []
        for e in entries:
            eid_ = e["id"]
            members = list(e["members"] or [])
            per_wod = []
            for w in wods:
                got = res[eid_].get(w["id"])
                per_wod.append({
                    "wod_id": w["id"], "name": w["name"], "result_type": w["result_type"],
                    "value": got["value"] if got else None,
                    "tiebreak": got["tiebreak"] if got else None,
                    "status": got["status"] if got else "",
                    "place": places[eid_].get(w["id"]),
                    "points": points[eid_].get(w["id"], 0),
                })
            rows.append({
                "entry_id": eid_,
                "is_me": bool(mine_entry and mine_entry == eid_),
                "name": e["team_name"] or (members[0] if members else "Без имени"),
                "members": members if d["team_size"] > 1 else [],
                "avatar": f"/api/photo/{e['photo_user']}?v={e['photo_v']}" if e["photo_user"] else "",
                "points": sum(points[eid_].values()),
                "wods": per_wod,
            })

        # При равной сумме выше тот, у кого лучше места: сравниваем
        # отсортированные списки мест, при равенстве — следующее место
        def rank_key(x):
            best = sorted(w["place"] for w in x["wods"] if w["place"])
            return (-x["points"], best)

        rows.sort(key=rank_key)
        # место в протоколе: равная сумма и равные места делят одну строку
        place = 0
        prev = None
        for i, x in enumerate(rows):
            k = rank_key(x)
            if k != prev:
                place = i + 1
                prev = k
            x["place"] = place

        done = sum(1 for w in wods
                   if any(res[e["id"]].get(w["id"]) for e in entries))
    return _json({
        "division": {"id": d["id"], "name": d["name"], "team_size": d["team_size"]},
        "wods_total": len(wods), "wods_done": done,
        "rows": rows,
    })


async def h_event_wods(r):
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        feed = await c.fetchval("SELECT feed_url FROM events WHERE id=$1", eid)
        if feed:
            snap = await feed_snapshot(feed)
            if not snap["ok"]:
                return _json({"error": "feed_unavailable"}, status=503)
            return _json(feed_wods(snap, await _event_divs_for_feed(c, eid)))
        rows = await c.fetch(
            "SELECT * FROM wods WHERE event_id=$1 ORDER BY ord, id", eid)
        links = await c.fetch(
            """SELECT wd.wod_id, d.id, d.name FROM wod_divisions wd
               JOIN divisions d ON d.id = wd.division_id
               WHERE d.event_id = $1""", eid)
    by_wod = {}
    for l in links:
        by_wod.setdefault(l["wod_id"], []).append({"id": l["id"], "name": l["name"]})
    return _json([{
        "id": x["id"], "ord": x["ord"], "name": x["name"], "description": x["description"],
        "result_type": x["result_type"], "time_cap": x["time_cap"], "stage": x["stage"],
        "scoring_note": x["scoring_note"], "day": str(x["day"] or ""),
        # пустой список дивизионов = комплекс для всех
        "divisions": by_wod.get(x["id"], []),
    } for x in rows])


async def h_event_schedule(r):
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        feed = await c.fetchval("SELECT feed_url FROM events WHERE id=$1", eid)
        if feed:
            snap = await feed_snapshot(feed)
            if not snap["ok"]:
                return _json({"error": "feed_unavailable"}, status=503)
            return _json(feed_schedule(snap, await _event_divs_for_feed(c, eid)))
        rows = await c.fetch(
            """SELECT s.*, d.name AS division FROM schedule s
               LEFT JOIN divisions d ON d.id = s.division_id
               WHERE s.event_id=$1 ORDER BY s.day, s.time, s.id""", eid)
    return _json([{
        "id": x["id"], "day": str(x["day"] or ""), "time": x["time"],
        "title": x["title"], "location": x["location"], "division": x["division"] or "",
    } for x in rows])


async def h_event_heats(r):
    """Заходы со старт-листом. Состав берём по заявкам, а не по ФИО."""
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    my_entry_id = None
    async with db_pool.acquire() as c:
        feed = await c.fetchval("SELECT feed_url FROM events WHERE id=$1", eid)
        if feed:
            snap = await feed_snapshot(feed)
            if not snap["ok"]:
                return _json({"error": "feed_unavailable"}, status=503)
            me = await current_user(r)
            return _json(feed_heats(snap, await _event_divs_for_feed(c, eid),
                                    me["name"] if me else ""))
        me = await current_user(r)
        if me:
            mine = await _my_entry(c, eid, me["id"])
            my_entry_id = mine["id"] if mine else None
        heats = await c.fetch(
            """SELECT h.*, w.name AS wod FROM heats h
               LEFT JOIN wods w ON w.id = h.wod_id
               WHERE h.event_id=$1 ORDER BY h.day, h.start_time, h.number""", eid)
        lanes = await c.fetch(
            """SELECT he.heat_id, he.lane, en.id AS entry_id, en.team_name,
                      d.name AS division,
                      ARRAY_AGG(u.name ORDER BY m.is_captain DESC, u.name) AS members,
                      MIN(u.id) FILTER (WHERE u.photo_v > 0) AS photo_user,
                      MIN(u.photo_v) FILTER (WHERE u.photo_v > 0) AS photo_v
               FROM heat_entries he
               JOIN heats h ON h.id = he.heat_id
               JOIN entries en ON en.id = he.entry_id
               JOIN divisions d ON d.id = en.division_id
               JOIN entry_members m ON m.entry_id = en.id
               JOIN users u ON u.id = m.user_id
               WHERE h.event_id = $1
               GROUP BY he.heat_id, he.lane, en.id, en.team_name, d.name
               ORDER BY he.lane""", eid)
    by_heat = {}
    for l in lanes:
        members = list(l["members"] or [])
        by_heat.setdefault(l["heat_id"], []).append({
            "lane": l["lane"], "entry_id": l["entry_id"],
            "is_me": l["entry_id"] == my_entry_id,
            "name": l["team_name"] or (members[0] if members else "Без имени"),
            "members": members, "division": l["division"],
            "avatar": f"/api/photo/{l['photo_user']}?v={l['photo_v']}" if l["photo_user"] else "",
        })
    return _json([{
        "id": h["id"], "number": h["number"], "wod": h["wod"] or "",
        "wod_id": h["wod_id"], "day": str(h["day"] or ""),
        "briefing_start": h["briefing_start"], "briefing_end": h["briefing_end"],
        "start_time": h["start_time"], "location": h["location"],
        "entries": by_heat.get(h["id"], []),
    } for h in heats])


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


async def _event_image(r, column):
    """Картинка события — контент, оформление интерфейса она не меняет.

    banner — афиша 16:9 во всю ширину, mark — квадратный знак под круглый чип.
    Знак присылают с прозрачностью, поэтому тип отдаём по сигнатуре файла.
    """
    if not USE_DB:
        return web.Response(status=404)
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return web.Response(status=404)
    async with db_pool.acquire() as c:
        row = await c.fetchrow(f"SELECT {column} FROM events WHERE id=$1", eid)
    if not row or not row[column]:
        return web.Response(status=404)
    blob = bytes(row[column])
    ctype = "image/png" if blob[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg"
    resp = web.Response(body=blob, content_type=ctype)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


async def h_event_banner(r):
    return await _event_image(r, "banner")


async def h_event_mark(r):
    return await _event_image(r, "mark")


async def h_events(r):
    """Каталог событий."""
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT e.id, e.slug, e.title, e.city, e.venue, e.date_start, e.date_end,
                      e.status, e.banner_v, e.mark_v, e.reg_opens_at, e.reg_closes_at,
                      (SELECT COUNT(*) FROM entries en WHERE en.event_id=e.id AND en.status='active') AS entries,
                      (SELECT MIN(price) FROM divisions d WHERE d.event_id=e.id) AS price_min,
                      (SELECT MAX(price) FROM divisions d WHERE d.event_id=e.id) AS price_max,
                      (SELECT ARRAY_AGG(DISTINCT d.level) FROM divisions d
                        WHERE d.event_id=e.id AND d.level <> '') AS levels
               FROM events e
               WHERE e.visibility='public' AND e.status <> 'draft'
               ORDER BY e.date_start DESC NULLS LAST""")
    return _json([{
        "id": x["id"], "slug": x["slug"], "title": x["title"], "city": x["city"],
        "venue": x["venue"], "status": x["status"],
        "banner_v": x["banner_v"], "has_banner": bool(x["banner_v"]),
        "mark_v": x["mark_v"], "has_mark": bool(x["mark_v"]),
        "date_start": str(x["date_start"] or ""), "date_end": str(x["date_end"] or ""),
        # каталог показывает срок, а не дату: без этих полей срок не посчитать,
        # а «анонс» (регистрация ещё не открылась) не отличить от «сбора заявок»
        "reg_opens_at": str(x["reg_opens_at"] or ""),
        "reg_closes_at": str(x["reg_closes_at"] or ""),
        "entries": x["entries"],
        "price_min": x["price_min"], "price_max": x["price_max"],
        "levels": list(x["levels"] or []),
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
        # вкладку без содержимого не показываем, поэтому считаем заранее
        counts = dict(await c.fetchrow(
            """SELECT (SELECT COUNT(*) FROM wods WHERE event_id=$1)     AS wods,
                      (SELECT COUNT(*) FROM schedule WHERE event_id=$1) AS schedule,
                      (SELECT COUNT(*) FROM heats WHERE event_id=$1)    AS heats,
                      (SELECT COUNT(*) FROM results rs JOIN entries en ON en.id=rs.entry_id
                        WHERE en.event_id=$1)                           AS results""", eid))
        # своя заявка нужна экрану сразу: от неё зависит, что показывать —
        # кнопку подачи или карточку «вы заявлены»
        me = await current_user(r)
        mine = await _my_entry(c, eid, me["id"]) if me else None

    # Запрос к модулю — уже вне пула: сетевой вызов не должен держать
    # соединение к базе, иначе лежащий модуль выест пул на живом старте.
    feed_down = False
    if ev["feed_url"]:
        snap = await feed_snapshot(ev["feed_url"])
        feed_down = not snap["ok"]
        counts = {"wods": len(snap["wods"]), "schedule": len(snap["schedule"]),
                  "heats": len(snap["heats"]), "results": len(snap["leaderboard"])}
        if feed_down:
            # модуль лёг — вкладки всё равно показываем, внутри будет честная
            # ошибка: «протокола нет» и «связи нет» это разные сообщения
            counts = {"wods": 1, "schedule": 1, "heats": 1, "results": 1}
    return _json({
        "id": ev["id"], "slug": ev["slug"], "title": ev["title"],
        "description": ev["description"], "city": ev["city"], "venue": ev["venue"],
        "date_start": str(ev["date_start"] or ""), "date_end": str(ev["date_end"] or ""),
        "reg_opens_at": str(ev["reg_opens_at"] or ""), "reg_closes_at": str(ev["reg_closes_at"] or ""),
        "qual_start": str(ev["qual_start"] or ""), "qual_end": str(ev["qual_end"] or ""),
        "instagram": ev["instagram"], "telegram": ev["telegram"],
        "has_banner": bool(ev["banner_v"]), "banner_v": ev["banner_v"],
        "has_mark": bool(ev["mark_v"]), "mark_v": ev["mark_v"],
        "athletes": athletes, "entries": entries,
        "status": ev["status"],
        "my_entry": dict(mine) if mine else None,
        "has": {k: int(v) for k, v in counts.items()},
        "feed": bool(ev["feed_url"]), "feed_down": feed_down,
        "profile_complete": bool(me and me["name"] and me["gender"]) if me else False,
        "divisions": [{"id": d["id"], "name": d["name"], "team_size": d["team_size"],
                       "gender_rule": d["gender_rule"], "max_entries": d["max_entries"],
                       "level": d["level"],
                       "price": d["price"], "entries": d["entries"]} for d in divs],
    })


# ── Админка ──────────────────────────────────────────────────────────
# Один оператор с паролем. Ролевой доступ «организатор видит только свой
# старт» лежит в event_staff и включается, когда появятся реальные
# организаторы: сейчас городить его не на ком.

def _admin_ok(r):
    """Пароль приходит либо как есть, либо в base64.

    Заголовки HTTP не переносят ничего вне latin-1, а пароль вполне может быть
    кириллическим: браузер тогда просто не отправит запрос. Поэтому страница
    шлёт base64, а обычный заголовок оставлен для curl и совместимости.
    """
    if not ADMIN_PASSWORD:
        return False
    got = r.headers.get("X-Admin-Password", "")
    if not got:
        raw = r.headers.get("X-Admin-Password-B64", "")
        try:
            got = base64.b64decode(raw, validate=True).decode("utf-8") if raw else ""
        except Exception:
            return False
    # сравниваем байты: compare_digest на строках падает с не-ASCII,
    # а пароль вполне может быть кириллическим
    return hmac.compare_digest(got.encode("utf-8"), ADMIN_PASSWORD.encode("utf-8"))


def _need_admin():
    return _json({"error": "forbidden"}, status=403)


_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "",
    "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def slugify(title):
    out = []
    for ch in (title or "").lower():
        if ch in _TRANSLIT:
            out.append(_TRANSLIT[ch])
        elif ch.isalnum():
            out.append(ch)
        else:
            out.append("-")
    slug = "-".join(x for x in "".join(out).split("-") if x)
    return slug[:60] or "event"


def _date(v):
    """Пустая строка — это NULL, а не ошибка: часть дат необязательна."""
    v = str(v or "").strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except ValueError:
        raise ValueError(f"дата «{v}» не в формате ГГГГ-ММ-ДД")


EVENT_STATUSES = ("draft", "registration", "live", "finished")
NEWS_CATEGORIES = ("", "ru", "world")
GENDER_RULES = ("any", "male", "female", "mixed")
LEVELS = ("", "sc", "bg", "rx", "elite")


async def a_check(r):
    if not _admin_ok(r):
        return _need_admin()
    return _json({"ok": True, "db": db_pool is not None})


async def a_events(r):
    """Все события, включая черновики и скрытые: это рабочий список оператора."""
    if not _admin_ok(r):
        return _need_admin()
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT e.id, e.slug, e.title, e.city, e.status, e.visibility,
                      e.date_start, e.feed_url, e.banner_v, e.mark_v,
                      (SELECT COUNT(*) FROM divisions d WHERE d.event_id=e.id) AS divisions,
                      (SELECT COUNT(*) FROM entries en
                        WHERE en.event_id=e.id AND en.status='active')          AS entries
               FROM events e ORDER BY e.date_start DESC NULLS LAST, e.id DESC""")
    return _json([{
        "id": x["id"], "slug": x["slug"], "title": x["title"], "city": x["city"],
        "status": x["status"], "visibility": x["visibility"],
        "date_start": str(x["date_start"] or ""), "feed_url": x["feed_url"],
        "has_banner": bool(x["banner_v"]), "has_mark": bool(x["mark_v"]),
        "divisions": x["divisions"], "entries": x["entries"],
    } for x in rows])


EVENT_FIELDS = ("title", "description", "city", "venue", "status", "visibility",
                "telegram", "instagram", "feed_url")
EVENT_DATES = ("date_start", "date_end", "reg_opens_at", "reg_closes_at",
               "qual_start", "qual_end")


async def a_event_save(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)

    eid = r.match_info.get("id")
    title = _clean(body.get("title"), 200)
    if len(title) < 2:
        return _json({"error": "invalid", "fields": {"title": "Нужно название"}}, status=400)
    status = str(body.get("status") or "draft")
    if status not in EVENT_STATUSES:
        return _json({"error": "invalid", "fields": {"status": "Неизвестный статус"}}, status=400)
    try:
        dates = {k: _date(body.get(k)) for k in EVENT_DATES}
    except ValueError as e:
        return _json({"error": "invalid", "fields": {"date_start": str(e)}}, status=400)
    if dates["date_start"] and dates["date_end"] and dates["date_end"] < dates["date_start"]:
        return _json({"error": "invalid",
                      "fields": {"date_end": "Конец раньше начала"}}, status=400)
    if dates["reg_opens_at"] and dates["reg_closes_at"] \
            and dates["reg_closes_at"] < dates["reg_opens_at"]:
        return _json({"error": "invalid",
                      "fields": {"reg_closes_at": "Закрытие раньше открытия"}}, status=400)

    vals = {k: _clean(body.get(k), 400) for k in EVENT_FIELDS}
    vals["title"] = title
    vals["status"] = status
    vals["visibility"] = vals["visibility"] if vals["visibility"] in ("public", "link") else "public"
    vals["description"] = str(body.get("description") or "")[:4000]
    vals.update(dates)

    async with db_pool.acquire() as c:
        if eid:
            row = await c.fetchrow(
                """UPDATE events SET title=$2, description=$3, city=$4, venue=$5,
                        status=$6, visibility=$7, telegram=$8, instagram=$9, feed_url=$10,
                        date_start=$11, date_end=$12, reg_opens_at=$13, reg_closes_at=$14,
                        qual_start=$15, qual_end=$16
                   WHERE id=$1 RETURNING id""",
                int(eid), vals["title"], vals["description"], vals["city"], vals["venue"],
                vals["status"], vals["visibility"], vals["telegram"], vals["instagram"],
                vals["feed_url"], *[dates[k] for k in EVENT_DATES])
            if not row:
                return _json({"error": "not_found"}, status=404)
            new_id = row["id"]
        else:
            slug = slugify(title)
            # слаг участвует в ссылке-приглашении, поэтому он должен быть уникален
            taken = {x["slug"] for x in await c.fetch(
                "SELECT slug FROM events WHERE slug LIKE $1", slug + "%")}
            uniq, n = slug, 2
            while uniq in taken:
                uniq, n = f"{slug}-{n}", n + 1
            new_id = await c.fetchval(
                """INSERT INTO events (slug, title, description, city, venue, status,
                        visibility, telegram, instagram, feed_url,
                        date_start, date_end, reg_opens_at, reg_closes_at, qual_start, qual_end)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
                   RETURNING id""",
                uniq, vals["title"], vals["description"], vals["city"], vals["venue"],
                vals["status"], vals["visibility"], vals["telegram"], vals["instagram"],
                vals["feed_url"], *[dates[k] for k in EVENT_DATES])
        logger.info("Админка: событие %s сохранено", new_id)
    return _json({"ok": True, "id": new_id})


async def a_event_one(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        ev = await c.fetchrow("SELECT * FROM events WHERE id=$1", eid)
        if not ev:
            return _json({"error": "not_found"}, status=404)
        divs = await c.fetch(
            "SELECT * FROM divisions WHERE event_id=$1 ORDER BY ord, id", eid)
    out = {k: (str(ev[k]) if isinstance(ev[k], date) else ev[k])
           for k in ("id", "slug", "title", "description", "city", "venue", "status",
                     "visibility", "telegram", "instagram", "feed_url") + EVENT_DATES}
    for k in EVENT_DATES:
        out[k] = str(ev[k] or "")
    out["has_banner"] = bool(ev["banner_v"])
    out["has_mark"] = bool(ev["mark_v"])
    out["divisions"] = [{
        "id": d["id"], "name": d["name"], "team_size": d["team_size"],
        "gender_rule": d["gender_rule"], "level": d["level"], "price": d["price"],
        "max_entries": d["max_entries"], "ord": d["ord"],
        "feed_category": d["feed_category"], "feed_gender": d["feed_gender"],
    } for d in divs]
    return _json(out)


async def a_event_image(r):
    """Афиша и знак. Браузер уже ужал картинку, поэтому принимаем dataURL."""
    if not _admin_ok(r):
        return _need_admin()
    try:
        eid = int(r.match_info["id"])
        body = await r.json()
        kind = body.get("kind")
        head, b64 = str(body.get("data") or "").split(",", 1)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if kind not in ("banner", "mark"):
        return _json({"error": "bad_request"}, status=400)
    if "image/jpeg" not in head and "image/png" not in head:
        return _json({"error": "bad_format"}, status=400)
    try:
        blob = base64.b64decode(b64, validate=True)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if not blob or len(blob) > 4_000_000:
        return _json({"error": "too_big"}, status=400)
    col, ver = ("banner", "banner_v") if kind == "banner" else ("mark", "mark_v")
    async with db_pool.acquire() as c:
        v = await c.fetchval(
            f"UPDATE events SET {col}=$2, {ver}={ver}+1 WHERE id=$1 RETURNING {ver}", eid, blob)
    if v is None:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "v": v})


DIV_NUMS = ("team_size", "price", "ord")


async def a_division_save(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)
    did = r.match_info.get("id")
    name = _clean(body.get("name"), 80)
    if len(name) < 2:
        return _json({"error": "invalid", "fields": {"name": "Нужно название"}}, status=400)
    rule = str(body.get("gender_rule") or "any")
    level = str(body.get("level") or "")
    if rule not in GENDER_RULES or level not in LEVELS:
        return _json({"error": "invalid", "fields": {"name": "Неизвестный формат"}}, status=400)

    def num(key, lo, hi, default=0):
        try:
            v = int(body.get(key) if body.get(key) not in (None, "") else default)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    team = num("team_size", 1, 12, 1)
    price = num("price", 0, 1_000_000, 0)
    ordv = num("ord", 0, 999, 0)
    limit = body.get("max_entries")
    limit = int(limit) if str(limit or "").isdigit() and int(limit) > 0 else None
    fc = _clean(body.get("feed_category"), 80)
    fg = _clean(body.get("feed_gender"), 8)

    async with db_pool.acquire() as c:
        if did:
            row = await c.fetchrow(
                """UPDATE divisions SET name=$2, team_size=$3, gender_rule=$4, level=$5,
                        price=$6, max_entries=$7, ord=$8, feed_category=$9, feed_gender=$10
                   WHERE id=$1 RETURNING id""",
                int(did), name, team, rule, level, price, limit, ordv, fc, fg)
            if not row:
                return _json({"error": "not_found"}, status=404)
            return _json({"ok": True, "id": row["id"]})
        try:
            eid = int(body.get("event_id"))
        except (TypeError, ValueError):
            return _json({"error": "bad_request"}, status=400)
        new_id = await c.fetchval(
            """INSERT INTO divisions (event_id, name, team_size, gender_rule, level,
                    price, max_entries, ord, feed_category, feed_gender)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
            eid, name, team, rule, level, price, limit, ordv, fc, fg)
    return _json({"ok": True, "id": new_id})


async def a_division_delete(r):
    """Удаляем только пустой дивизион: с заявками это потеря данных."""
    if not _admin_ok(r):
        return _need_admin()
    try:
        did = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        n = await c.fetchval(
            "SELECT COUNT(*) FROM entries WHERE division_id=$1 AND status='active'", did)
        if n:
            return _json({"error": "has_entries", "entries": n}, status=409)
        await c.execute("DELETE FROM divisions WHERE id=$1", did)
    return _json({"ok": True})


async def a_entries(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        eid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT en.id, en.status, en.team_name, en.created_at, d.name AS division,
                      ARRAY_AGG(u.name ORDER BY m.is_captain DESC, u.name) AS members,
                      ARRAY_AGG(COALESCE(NULLIF(u.city,''),'—') ORDER BY m.is_captain DESC, u.name) AS cities
               FROM entries en
               JOIN divisions d ON d.id = en.division_id
               JOIN entry_members m ON m.entry_id = en.id
               JOIN users u ON u.id = m.user_id
               WHERE en.event_id=$1
               GROUP BY en.id, en.status, en.team_name, en.created_at, d.name, d.ord
               ORDER BY d.ord, en.id""", eid)
    return _json([{
        "id": x["id"], "status": x["status"], "team_name": x["team_name"],
        "division": x["division"], "members": list(x["members"] or []),
        "cities": list(x["cities"] or []),
        "created_at": x["created_at"].isoformat() if x["created_at"] else "",
    } for x in rows])


async def a_entry_status(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        enid = int(r.match_info["id"])
        body = await r.json()
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    st = str(body.get("status") or "")
    if st not in ("active", "withdrawn"):
        return _json({"error": "bad_request"}, status=400)
    async with db_pool.acquire() as c:
        row = await c.fetchrow(
            "UPDATE entries SET status=$2 WHERE id=$1 RETURNING id", enid, st)
    if not row:
        return _json({"error": "not_found"}, status=404)
    logger.info("Админка: заявка %s → %s", enid, st)
    return _json({"ok": True})


async def a_news_list(r):
    if not _admin_ok(r):
        return _need_admin()
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT id, title, summary, source_url, is_featured, image_v,
                      category, published_at
               FROM news ORDER BY published_at DESC""")
    return _json([{
        "id": x["id"], "title": x["title"], "summary": x["summary"],
        "source_url": x["source_url"], "is_featured": x["is_featured"],
        "category": x["category"], "has_image": bool(x["image_v"]),
        "published_at": x["published_at"].isoformat(),
    } for x in rows])


async def a_news_save(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)
    nid = r.match_info.get("id")
    title = _clean(body.get("title"), 300)
    if len(title) < 3:
        return _json({"error": "invalid", "fields": {"title": "Нужен заголовок"}}, status=400)
    summary = _clean(body.get("summary"), 500)
    text = str(body.get("body") or "")[:20000]
    url = _clean(body.get("source_url"), 500)
    feat = bool(body.get("is_featured"))
    cat = str(body.get("category") or "")
    if cat not in NEWS_CATEGORIES:
        return _json({"error": "invalid",
                      "fields": {"category": "Неизвестная категория"}}, status=400)
    async with db_pool.acquire() as c:
        if nid:
            row = await c.fetchrow(
                """UPDATE news SET title=$2, summary=$3, body=$4, source_url=$5,
                        is_featured=$6, category=$7
                   WHERE id=$1 RETURNING id""",
                int(nid), title, summary, text, url, feat, cat)
            if not row:
                return _json({"error": "not_found"}, status=404)
            return _json({"ok": True, "id": row["id"]})
        new_id = await c.fetchval(
            """INSERT INTO news (title, summary, body, source_url, is_featured, category)
               VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
            title, summary, text, url, feat, cat)
    return _json({"ok": True, "id": new_id})


async def a_news_image(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        nid = int(r.match_info["id"])
        body = await r.json()
        head, b64 = str(body.get("data") or "").split(",", 1)
    except Exception:
        return _json({"error": "bad_request"}, status=400)
    if "image/jpeg" not in head and "image/png" not in head:
        return _json({"error": "bad_format"}, status=400)
    blob = base64.b64decode(b64, validate=True)
    if not blob or len(blob) > 4_000_000:
        return _json({"error": "too_big"}, status=400)
    async with db_pool.acquire() as c:
        v = await c.fetchval(
            "UPDATE news SET image=$2, image_v=image_v+1 WHERE id=$1 RETURNING image_v", nid, blob)
    if v is None:
        return _json({"error": "not_found"}, status=404)
    return _json({"ok": True, "v": v})


async def a_news_delete(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        nid = int(r.match_info["id"])
    except ValueError:
        return _json({"error": "bad_id"}, status=404)
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM news WHERE id=$1", nid)
    return _json({"ok": True})


async def a_clubs(r):
    if not _admin_ok(r):
        return _need_admin()
    async with db_pool.acquire() as c:
        rows = await c.fetch(
            """SELECT c.id, c.name, c.city, c.is_active,
                      (SELECT COUNT(*) FROM users u WHERE u.club_id=c.id) AS athletes
               FROM clubs c ORDER BY c.name""")
    return _json([dict(x) for x in rows])


async def a_club_save(r):
    if not _admin_ok(r):
        return _need_admin()
    try:
        body = await r.json()
    except Exception:
        return _json({"error": "bad_json"}, status=400)
    cid = r.match_info.get("id")
    name = _clean(body.get("name"), 120)
    if len(name) < 2:
        return _json({"error": "invalid", "fields": {"name": "Нужно название"}}, status=400)
    city = _clean(body.get("city"), 80)
    active = bool(body.get("is_active", True))
    async with db_pool.acquire() as c:
        if cid:
            row = await c.fetchrow(
                "UPDATE clubs SET name=$2, city=$3, is_active=$4 WHERE id=$1 RETURNING id",
                int(cid), name, city, active)
            if not row:
                return _json({"error": "not_found"}, status=404)
            return _json({"ok": True, "id": row["id"]})
        new_id = await c.fetchval(
            "INSERT INTO clubs (name, city, is_active) VALUES ($1,$2,$3) RETURNING id",
            name, city, active)
    return _json({"ok": True, "id": new_id})


async def h_admin(r):
    f = BASE_DIR / "admin.html"
    if not f.exists():
        return web.Response(text="admin.html not found", status=404)
    resp = web.Response(text=f.read_text(encoding="utf-8"), content_type="text/html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


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
    app.router.add_get("/api/event-banner/{id}", h_event_banner)
    app.router.add_get("/api/event-mark/{id}", h_event_mark)
    app.router.add_get("/api/events", h_events)
    app.router.add_get("/api/events/{id}", h_event)
    app.router.add_patch("/api/me", h_me_save)
    app.router.add_post("/api/me/photo", h_me_photo)
    app.router.add_get("/api/clubs", h_clubs)
    app.router.add_get("/api/partner", h_partner)
    app.router.add_post("/api/partner/click", h_partner_click)
    app.router.add_get("/api/partner-logo/{id}", h_partner_logo)
    app.router.add_get("/api/news", h_news)
    app.router.add_get("/api/news/{id}", h_news_one)
    app.router.add_get("/api/news-image/{id}", h_news_image)
    app.router.add_post("/api/events/{id}/entry", h_entry_create)
    app.router.add_delete("/api/events/{id}/entry", h_entry_withdraw)
    app.router.add_get("/api/events/{id}/leaderboard", h_leaderboard)
    app.router.add_get("/api/events/{id}/wods", h_event_wods)
    app.router.add_get("/api/events/{id}/schedule", h_event_schedule)
    app.router.add_get("/api/events/{id}/heats", h_event_heats)

    app.router.add_get("/admin", h_admin)
    app.router.add_get("/api/admin/check", a_check)
    app.router.add_get("/api/admin/events", a_events)
    app.router.add_post("/api/admin/events", a_event_save)
    app.router.add_get("/api/admin/events/{id}", a_event_one)
    app.router.add_put("/api/admin/events/{id}", a_event_save)
    app.router.add_post("/api/admin/events/{id}/image", a_event_image)
    app.router.add_get("/api/admin/events/{id}/entries", a_entries)
    app.router.add_post("/api/admin/entries/{id}", a_entry_status)
    app.router.add_post("/api/admin/divisions", a_division_save)
    app.router.add_put("/api/admin/divisions/{id}", a_division_save)
    app.router.add_delete("/api/admin/divisions/{id}", a_division_delete)
    app.router.add_get("/api/admin/news", a_news_list)
    app.router.add_post("/api/admin/news", a_news_save)
    app.router.add_put("/api/admin/news/{id}", a_news_save)
    app.router.add_post("/api/admin/news/{id}/image", a_news_image)
    app.router.add_delete("/api/admin/news/{id}", a_news_delete)
    app.router.add_get("/api/admin/clubs", a_clubs)
    app.router.add_post("/api/admin/clubs", a_club_save)
    app.router.add_put("/api/admin/clubs/{id}", a_club_save)
    app.router.add_get("/api/admin/partners", a_partners)
    app.router.add_post("/api/admin/partners", a_partner_save)
    app.router.add_put("/api/admin/partners/{id}", a_partner_save)
    app.router.add_post("/api/admin/partners/{id}/logo", a_partner_logo)
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
