"""Перенос «Битвы за Херсонес 2026» из соревновательного модуля crossfit-comp.

  DATABASE_URL=... python import_hersones_2026.py [--photos DIR] [--keep-2027]

Модуль читается только публичными GET-запросами: протокол по шести группам,
комплексы, заходы, расписание и фото атлетов. В модуле ничего не меняется.

Что делает (в одной транзакции):
  1. Заводит завершённое событие «Битва за Херсонес 2026» (slug battle-2026)
     с шестью категориями: Новички → Beginners, Любители → Intermediate,
     Продвинутые → Rx, по полу — Мужчины / Женщины.
  2. Заводит 71 атлета отдельными профилями. tg_id у них отрицательные: такого
     id в Telegram не бывает, поэтому ни с кем настоящим они не совпадут
     и писем не получат (tg_verified_at пуст).
  3. Переносит комплексы (со своей шкалой финала), сырые результаты, заходы
     с дорожками и расписание. Места и баллы платформа считает сама —
     в конце они сверяются с протоколом модуля по каждому атлету.
  4. Удаляет тестовое событие «Битва за Херсонес 2027» (slug battle-2027)
     и сгенерированных для него атлетов, забрав у него афишу и знак.
     Проверенных через Telegram людей не трогает. --keep-2027 — не удалять.

Повторный запуск безопасен: прежний импорт 2026 удаляется и собирается заново.
"""
import asyncio, json, os, sys, unicodedata, urllib.request
from datetime import date, datetime, timezone

import asyncpg

COMP = "https://crossfit-comp-production.up.railway.app"
SLUG, OLD_SLUG = "battle-2026", "battle-2027"
TG_BASE = -10_000_000          # атлеты импорта: tg_id от −10 000 001 вниз
CATS = {"Новички": ("bg", "Beginners"), "Любители": ("inter", "Intermediate"),
        "Продвинутые": ("rx", "Rx")}
GENS = {"М": ("male", "Мужчины"), "Ж": ("female", "Женщины")}
DAYS = {"8 августа": date(2026, 8, 8), "9 августа": date(2026, 8, 9)}


def get_json(path, **params):
    from urllib.parse import urlencode
    url = COMP + path + (("?" + urlencode(params)) if params else "")
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def get_bytes(path):
    with urllib.request.urlopen(COMP + path, timeout=30) as r:
        return r.read()


def norm(s):
    s = unicodedata.normalize("NFC", s or "").lower().replace("ё", "е")
    return " ".join(s.split())


def name_key(s):
    """Имя без порядка слов: «Ольховик Павел» и «Павел Ольховик» — один ключ."""
    return " ".join(sorted(norm(s).split()[:2]))


def short_name(full):
    """«Фамилия Имя Отчество» → «Фамилия Имя», как в протоколах платформы."""
    parts = (full or "").split()
    return " ".join(parts[:2]) if len(parts) >= 2 else (full or "").strip()


async def main():
    args = sys.argv[1:]
    photos_dir = args[args.index("--photos") + 1] if "--photos" in args else None
    keep_old = "--keep-2027" in args

    # ── модуль ──
    wods_in = get_json("/api/wods")
    sched_in = get_json("/api/schedule")
    heats_in = get_json("/api/heats")
    groups = {}
    for cat in CATS:
        for gen in GENS:
            groups[(cat, gen)] = get_json("/api/leaderboard", category=cat, gender=gen)
    total = sum(len(v) for v in groups.values())
    print(f"модуль: {len(wods_in)} комплексов, {total} атлетов, {len(heats_in)} заходов, "
          f"{len(sched_in)} строк расписания")

    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    async with c.transaction():
        old = await c.fetchrow("SELECT * FROM events WHERE slug=$1", OLD_SLUG)

        # прежний импорт — долой, собираем заново
        await c.execute("DELETE FROM events WHERE slug=$1", SLUG)
        await c.execute("DELETE FROM users WHERE tg_id <= $1 AND tg_id > $2",
                        TG_BASE, TG_BASE - 1_000_000)

        eid = await c.fetchval(
            """INSERT INTO events (slug, title, description, city, venue, date_start, date_end,
                                   status, visibility, telegram, instagram, banner, banner_v,
                                   mark, mark_v)
               VALUES ($1,$2,$3,$4,$5,$6,$7,'finished','public',$8,$9,$10,$11,$12,$13)
               RETURNING id""",
            SLUG, "Битва за Херсонес 2026",
            "Первая «Битва за Херсонес»: два дня, девять комплексов и 71 атлет "
            "в трёх категориях. Протокол перенесён из системы соревнования.",
            "Севастополь", "Яшмовый · Гасфорт", date(2026, 8, 8), date(2026, 8, 9),
            old["telegram"] if old else "", old["instagram"] if old else "",
            old["banner"] if old else None, old["banner_v"] if old else 0,
            old["mark"] if old else None, old["mark_v"] if old else 0)

        # организатор — владелец платформы, если он есть в этой базе
        owner = await c.fetchval(
            "SELECT id FROM users WHERE tg_id = ANY($1::bigint[]) ORDER BY id LIMIT 1",
            [77180808] + ([int(os.environ["DEV_TG_ID"])] if os.environ.get("DEV_TG_ID") else []))
        if owner:
            await c.execute(
                "INSERT INTO event_staff (event_id, user_id, role, is_creator) VALUES ($1,$2,'organizer',TRUE)",
                eid, owner)

        # ── категории ──
        div = {}
        ord_ = 0
        for cat, (level, lname) in CATS.items():
            for gen, (rule, gname) in GENS.items():
                ord_ += 1
                div[(cat, gen)] = await c.fetchval(
                    """INSERT INTO divisions (event_id, name, team_size, gender_rule, level, price, ord)
                       VALUES ($1,$2,1,$3,$4,0,$5) RETURNING id""",
                    eid, f"{lname} {gname}", rule, level, ord_)

        # ── комплексы ──
        wod_id = {}
        for w in sorted(wods_in, key=lambda x: (x["order"], x["id"])):
            is_final = norm(w["name"]) == "финал"
            wid = await c.fetchval(
                """INSERT INTO wods (event_id, name, description, ord, result_type, time_cap, day,
                                     stage, points_table, scoring_note)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                eid, w["name"], w["description"], w["order"], w["result_type"] or "time",
                w["time_cap"], DAYS.get(w["day"]), "final" if is_final else "qualification",
                w["points_table"], w["scoring"])
            wod_id[w["id"]] = wid
            cats = w["categories"] or list(CATS)
            gens = w["genders"] or list(GENS)
            for cat in cats:
                for gen in gens:
                    if (cat, gen) in div:
                        await c.execute("INSERT INTO wod_divisions (wod_id, division_id) VALUES ($1,$2)",
                                        wid, div[(cat, gen)])

        def wods_for(cat, gen):
            return [w for w in wods_in
                    if (not w["categories"] or cat in w["categories"])
                    and (not w["genders"] or gen in w["genders"])]

        # Настоящие люди, которые уже вошли в платформу через Telegram: если имя
        # и пол совпадают ровно с одним, заявка становится его — старт попадёт
        # в его профиль. Однофамильцев не угадываем: при двух совпадениях —
        # отдельный профиль, как у всех.
        verified = {}
        for u in await c.fetch(
                "SELECT id, name, gender FROM users WHERE tg_verified_at IS NOT NULL AND name <> ''"):
            verified.setdefault((name_key(u["name"]), u["gender"]), []).append(u["id"])

        # ── атлеты, заявки, результаты ──
        entry_by_name = {}        # (cat, gen, норм. полное имя) → entry_id
        linked = []
        seq = 0
        photos = 0
        for (cat, gen), rows in groups.items():
            gw = {w["name"]: wod_id[w["id"]] for w in wods_for(cat, gen)}
            for row in sorted(rows, key=lambda r: r["name"]):
                seq += 1
                photo = None
                if row["avatar"]:
                    pid = row["avatar"].split("/")[3].split("?")[0]
                    local = photos_dir and os.path.join(photos_dir, f"{pid}.jpg")
                    try:
                        photo = open(local, "rb").read() if local and os.path.exists(local) \
                            else get_bytes(row["avatar"])
                    except Exception as e:
                        print("  фото не получено:", row["name"], e)
                same = verified.get((name_key(row["name"]), gen), [])
                if len(same) == 1:
                    uid = same[0]
                    linked.append(row["name"])
                else:
                    uid = await c.fetchval(
                        """INSERT INTO users (tg_id, name, gender, photo, photo_v)
                           VALUES ($1,$2,$3,$4,$5) RETURNING id""",
                        TG_BASE - seq, short_name(row["name"]), gen, photo, 1 if photo else 0)
                    photos += bool(photo)
                enid = await c.fetchval(
                    """INSERT INTO entries (event_id, division_id, status, payment_status, created_at)
                       VALUES ($1,$2,'active','paid',$3) RETURNING id""",
                    eid, div[(cat, gen)], datetime(2026, 7, 1, tzinfo=timezone.utc))
                await c.execute(
                    "INSERT INTO entry_members (entry_id, user_id, is_captain) VALUES ($1,$2,TRUE)",
                    enid, uid)
                entry_by_name[(cat, gen, norm(row["name"]))] = enid
                for w in row["wods"]:
                    if w["result"] is None:
                        continue
                    await c.execute(
                        "INSERT INTO results (entry_id, wod_id, value, status) VALUES ($1,$2,$3,'ok')",
                        enid, gw[w["name"]], float(w["result"]))
        print(f"атлетов {seq}, с фото {photos}; привязаны к настоящим профилям: {linked}")

        # ── заходы ──
        # В заходах модуля комплекс назван коротко («+ вайб» вместо «+ Вайб (Любители)»):
        # ищем комплекс группы, чьё название совпадает или начинается с названия захода.
        skipped, lanes = [], 0
        for h in heats_in:
            cat, gen = h["category"], h["gender"]
            cand = [w for w in wods_for(cat, gen)
                    if norm(w["name"]) == norm(h["wod"]) or norm(w["name"]).startswith(norm(h["wod"]))]
            if len(cand) != 1 or (cat, gen) not in div:
                print("  заход без комплекса:", h["wod"], cat, gen, [w["name"] for w in cand])
                continue
            members = [(a, entry_by_name.get((cat, gen, norm(a["name"])))) for a in h["athletes"]]
            skipped += [a["name"] for a, e in members if not e]
            members = [(a, e) for a, e in members if e]
            if not members:
                continue            # в заходе были только люди без результатов — пустой не переносим
            hid = await c.fetchval(
                """INSERT INTO heats (event_id, wod_id, number, day, briefing_start, briefing_end,
                                      start_time, location)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
                eid, wod_id[cand[0]["id"]], h["heat"], DAYS.get(h["day"]),
                h["briefing_start"], h["briefing_end"], h["start_time"], h["location"])
            for a, enid in members:
                await c.execute(
                    """INSERT INTO heat_entries (heat_id, entry_id, lane) VALUES ($1,$2,$3)
                       ON CONFLICT DO NOTHING""", hid, enid, a["lane"])
                lanes += 1
        print(f"дорожек {lanes}; в заходах без результатов в протоколе: {sorted(set(skipped))}")

        # ── расписание ──
        for s in sched_in:
            await c.execute(
                "INSERT INTO schedule (event_id, day, time, title, location) VALUES ($1,$2,$3,$4,$5)",
                eid, DAYS.get(s["day"]), s["time"], " ".join(s["title"].split()), s["location"])

        # ── сверка с протоколом модуля ──
        # Сверяем по заявке, а не по имени: привязанная к настоящему профилю
        # заявка называется так, как человек подписан в платформе.
        sys.path.insert(0, os.environ.get("PLATFORM_DIR", os.getcwd()))
        import backend

        def rank_key(x):
            return (-x["points"], sorted(w["place"] for w in x["wods"] if w["place"]))

        mismatch = 0
        for (cat, gen), rows in groups.items():
            table = await backend._division_table(c, eid, div[(cat, gen)])
            ours = {r["entry_id"]: r for r in backend._protocol_rows(*table, 1)}
            for row in rows:
                mine = ours.get(entry_by_name[(cat, gen, norm(row["name"]))])
                place_mod = next(k for k, x in enumerate(rows, 1) if rank_key(x) == rank_key(row))
                if not mine or mine["points"] != row["points"] or mine["place"] != place_mod:
                    mismatch += 1
                    print("  расхождение:", cat, gen, row["name"], row["points"], place_mod,
                          "→", mine and (mine["points"], mine["place"]))
        print("сверка с модулем:", "всё совпало" if not mismatch else f"расхождений {mismatch}")
        if mismatch:
            raise SystemExit("есть расхождения — транзакция отменена")

        # ── тестовый 2027 ──
        if old and not keep_old:
            gone = await c.fetch(
                """SELECT u.id FROM users u
                   WHERE u.tg_verified_at IS NULL
                     AND EXISTS (SELECT 1 FROM entry_members m JOIN entries en ON en.id = m.entry_id
                                 WHERE m.user_id = u.id AND en.event_id = $1)
                     AND NOT EXISTS (SELECT 1 FROM entry_members m JOIN entries en ON en.id = m.entry_id
                                     WHERE m.user_id = u.id AND en.event_id <> $1)
                     AND NOT EXISTS (SELECT 1 FROM event_staff s
                                     WHERE s.user_id = u.id AND s.event_id <> $1)""", old["id"])
            await c.execute("DELETE FROM events WHERE id=$1", old["id"])
            if gone:
                await c.execute("DELETE FROM users WHERE id = ANY($1::int[])", [x["id"] for x in gone])
            print(f"удалено: событие «{old['title']}» и {len(gone)} сгенерированных атлетов")

    print("событие", eid, SLUG)
    await c.close()


asyncio.run(main())
