"""
Наполнение одного события полным турниром: атлеты, заявки, комплексы,
заходы с дорожками, расписание и результаты.

Нужен, чтобы прогнать на живых объёмах то, что нельзя проверить на трёх
строчках: сетку ввода, протокол на сотню человек, старт-листы, поиск своего
захода. Сид делает витрину, этот скрипт — турнир.

    DATABASE_URL='…' python fill_event.py 1
    DATABASE_URL='…' python fill_event.py 1 --athletes 120 --done 3 --keep-dates

  --athletes N   сколько человек завести на старт (по умолчанию хватит на все
                 категории события)
  --done N       сколько комплексов отсудить полностью; следующий заполняется
                 наполовину, остальные остаются пустыми. Так у судьи есть
                 работа, а у атлета — живой протокол
  --keep-dates   не двигать даты события. По умолчанию старт становится
                 сегодняшним и переходит в «идёт», иначе живой турнир
                 не проверить
  --judges N     сколько судей назначить (отдельные люди, не участники)

Скрипт стирает турнирные данные только этого события. Категории, афиша
и настройки остаются: их заводит организатор, а не генератор.

Pillow нужен для аватарок, как и в seed_demo.
"""

import os
import sys
import random
import asyncio
from datetime import date, timedelta

import asyncpg
from dotenv import load_dotenv

try:
    from seed_demo import avatar          # Pillow нужен только для аватарок
except ImportError:                       # на сервере его нет — не повод не работать
    avatar = None

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]

CITIES = ["Севастополь", "Краснодар", "Сочи", "Ялта", "Ростов-на-Дону",
          "Симферополь", "Анапа", "Новороссийск", "Москва", "Казань"]
GYMS = ["CrossFit Chersonesos", "Арена Юг", "Атлант", "Форт", "Прайд", "Титан"]

FIRST_M = ["Александр", "Дмитрий", "Максим", "Сергей", "Андрей", "Алексей",
           "Артём", "Илья", "Кирилл", "Михаил", "Никита", "Егор", "Роман",
           "Владимир", "Тимур", "Даниил", "Глеб", "Пётр", "Антон", "Игорь"]
FIRST_W = ["Анна", "Мария", "Елена", "Дарья", "Ольга", "Наталья", "Ирина",
           "Юлия", "Екатерина", "Полина", "Ксения", "Алина", "Виктория",
           "София", "Марина", "Татьяна", "Вера", "Кристина", "Лидия", "Милана"]
LAST_M = ["Астахов", "Белов", "Ветров", "Гордеев", "Дьяков", "Ершов", "Жуков",
          "Зимин", "Ильин", "Карпов", "Лапшин", "Морозов", "Носов", "Орлов",
          "Панин", "Рыбаков", "Сомов", "Тарасов", "Ушаков", "Фомин", "Хромов",
          "Цветков", "Чижов", "Шилов", "Щукин", "Юдин", "Яковлев", "Бурый",
          "Гущин", "Дроздов", "Ежов", "Зайцев"]

# Комплексы: разные типы результата и разные шкалы — иначе половина расчёта
# останется непроверенной. Финал со своей таблицей баллов, как на реальном старте.
WODS = [
    dict(name="Открытие", day=0, stage="qualification", result_type="time",
         time_cap="12:00", scoring_note="На время",
         description="21-15-9:\nТрастеры 43/30 кг\nПодтягивания с прыжка"),
    dict(name="Штанга", day=0, stage="qualification", result_type="weight",
         scoring_note="Максимум за подход",
         description="Взятие на грудь в стойку. Три попытки, в зачёт лучшая."),
    dict(name="Гимнастика", day=0, stage="qualification", result_type="reps",
         time_cap="8:00", scoring_note="AMRAP 8 мин", tiebreak=True,
         description="AMRAP 8:\n10 берпи через штангу\n15 махов гирей 24/16 кг"),
    dict(name="Спринт", day=1, stage="qualification", result_type="time",
         time_cap="6:00", scoring_note="На время",
         description="500 м гребля\n50 двойных прыжков\n30 приседаний со штангой"),
    dict(name="Финал", day=1, stage="final", result_type="time",
         time_cap="15:00", scoring_note="На время",
         points_table="100,90,80,70,60,50,40,30,20,15,10,5",
         description="Чиппер: канат, штанга, ходьба на руках, бег 400 м."),
]

DAY_SCHEDULE = [
    ("07:30", "Регистрация и взвешивание", "Главный вход"),
    ("08:30", "Брифинг судей", "Судейская"),
    ("09:00", "Общий брифинг участников", "Площадка A"),
    ("18:30", "Итоги дня", "Площадка A"),
]


def athlete_names(n, gender, used):
    """Непересекающиеся ФИО. Пул большой, но проверяем: дубли ломают старт-лист."""
    first, last = (FIRST_M, LAST_M) if gender == "М" else (FIRST_W, LAST_M)
    out = []
    while len(out) < n:
        surname = random.choice(last)
        if gender == "Ж":
            surname += "а" if not surname.endswith(("ый", "ой", "ий")) else "ая"
        name = f"{surname} {random.choice(first)}"
        if name in used:
            continue
        used.add(name)
        out.append(name)
    return out


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


async def main():
    args = sys.argv[1:]
    if not args or not args[0].isdigit():
        print(__doc__)
        return
    eid = int(args[0])

    def opt(flag, default):
        return int(args[args.index(flag) + 1]) if flag in args else default

    want_athletes = opt("--athletes", 0)
    done_wods = opt("--done", 3)
    judges_n = opt("--judges", 3)
    keep_dates = "--keep-dates" in args

    if not avatar:
        print("Pillow не найден — участники будут без фото.")
    random.seed(1000 + eid)          # один и тот же старт наполняется одинаково
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as c:
        ev = await c.fetchrow("SELECT id, title FROM events WHERE id=$1", eid)
        if not ev:
            print(f"События {eid} нет.")
            await pool.close()
            return
        divs = await c.fetch(
            "SELECT * FROM divisions WHERE event_id=$1 ORDER BY ord, id", eid)
        if not divs:
            print("У события нет категорий — сначала заведите их в админке.")
            await pool.close()
            return

        # ── Чистим только турнирную часть этого события ──────────────
        # по одному запросу: с параметром asyncpg готовит выражение и несколько
        # команд в него не принимает
        for sql in (
            "DELETE FROM results WHERE wod_id IN (SELECT id FROM wods WHERE event_id=$1)",
            "DELETE FROM heat_entries WHERE heat_id IN (SELECT id FROM heats WHERE event_id=$1)",
            "DELETE FROM heats WHERE event_id=$1",
            "DELETE FROM schedule WHERE event_id=$1",
            "DELETE FROM wod_divisions WHERE wod_id IN (SELECT id FROM wods WHERE event_id=$1)",
            "DELETE FROM wods WHERE event_id=$1",
            "DELETE FROM entry_members WHERE entry_id IN (SELECT id FROM entries WHERE event_id=$1)",
            "DELETE FROM entries WHERE event_id=$1",
        ):
            await c.execute(sql, eid)

        # ── Даты: живой турнир проверяется только на живых датах ─────
        d1 = date.today()
        d2 = d1 + timedelta(days=1)
        if not keep_dates:
            await c.execute(
                """UPDATE events SET status='live', date_start=$2, date_end=$3,
                          reg_opens_at=$4, reg_closes_at=$5 WHERE id=$1""",
                eid, d1, d2, d1 - timedelta(days=60), d1 - timedelta(days=2))
        else:
            row = await c.fetchrow(
                "SELECT date_start, date_end FROM events WHERE id=$1", eid)
            d1 = row["date_start"] or d1
            d2 = row["date_end"] or d1 + timedelta(days=1)
        days = [d1, d2]

        # ── Сколько людей нужно каждой категории ─────────────────────
        plan = []
        for d in divs:
            base = 8 if d["team_size"] > 1 else random.choice((14, 18, 22))
            if d["max_entries"]:
                base = min(base, d["max_entries"])
            plan.append((d, base))
        need = sum(n * max(1, d["team_size"]) for d, n in plan)
        if want_athletes:
            k = want_athletes / need
            plan = [(d, max(2, round(n * k))) for d, n in plan]
            need = sum(n * max(1, d["team_size"]) for d, n in plan)

        # ── Атлеты ───────────────────────────────────────────────────
        used = {r["name"] for r in await c.fetch("SELECT name FROM users")}
        tg = await c.fetchval("SELECT COALESCE(MAX(tg_id), 900000) FROM users") + 1
        pavel = await c.fetchrow(
            "SELECT id, gender FROM users WHERE name = 'Павел Ольховик'")

        async def make_user(name, gender):
            nonlocal tg
            tg += 1
            uid = await c.fetchval(
                """INSERT INTO users (tg_id,name,gender,city,gym,photo,photo_v,
                                      height_cm,weight_kg)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                tg, name, gender, random.choice(CITIES), random.choice(GYMS),
                avatar(name) if avatar else None, 1 if avatar else 0,
                random.randint(160, 195) if gender == "М" else random.randint(155, 180),
                round(random.uniform(70, 98) if gender == "М"
                      else random.uniform(52, 74), 1))
            return uid

        # ── Заявки ───────────────────────────────────────────────────
        entries = {}                                  # division_id → [entry_id]
        for d, count in plan:
            entries[d["id"]] = []
            for i in range(count):
                if d["team_size"] > 1:
                    team = f"Команда {i + 1}"
                    eids = await c.fetchval(
                        """INSERT INTO entries (event_id,division_id,team_name)
                           VALUES ($1,$2,$3) RETURNING id""", eid, d["id"], team)
                    genders = ["М", "Ж"] if d["gender_rule"] == "mixed" else \
                        ["М"] * d["team_size"]
                    for j in range(d["team_size"]):
                        g = genders[j % len(genders)]
                        uid = await make_user(athlete_names(1, g, used)[0], g)
                        await c.execute(
                            """INSERT INTO entry_members (entry_id,user_id,is_captain)
                               VALUES ($1,$2,$3)""", eids, uid, j == 0)
                else:
                    g = "Ж" if d["gender_rule"] == "female" else "М"
                    # владельца DEV-аккаунта сажаем в первую подходящую категорию,
                    # иначе на его экране «Мои заявки» нечего смотреть
                    if (i == 0 and pavel and pavel["gender"] == g
                            and not any(entries.values())):
                        uid = pavel["id"]
                    else:
                        uid = await make_user(athlete_names(1, g, used)[0], g)
                    eids = await c.fetchval(
                        """INSERT INTO entries (event_id,division_id)
                           VALUES ($1,$2) RETURNING id""", eid, d["id"])
                    await c.execute(
                        """INSERT INTO entry_members (entry_id,user_id,is_captain)
                           VALUES ($1,$2,TRUE)""", eids, uid)
                entries[d["id"]].append(eids)

        # ── Комплексы ────────────────────────────────────────────────
        wod_ids = []
        for n, w in enumerate(WODS, 1):
            wid = await c.fetchval(
                """INSERT INTO wods (event_id,name,description,ord,result_type,
                        time_cap,day,stage,points_table,scoring_note)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10) RETURNING id""",
                eid, w["name"], w["description"], n, w["result_type"],
                w.get("time_cap", ""), days[w["day"]], w["stage"],
                w.get("points_table", ""), w["scoring_note"])
            wod_ids.append((wid, w))
            # финал бежит не вся сетка, а сильнейшие категории
            targets = divs if w["stage"] != "final" else [
                d for d in divs if d["level"] in ("rx", "elite")] or list(divs)
            for d in targets:
                await c.execute(
                    "INSERT INTO wod_divisions (wod_id,division_id) VALUES ($1,$2)",
                    wid, d["id"])

        # ── Заходы и расписание ──────────────────────────────────────
        LANES = 8
        sched = []
        for day_i, day in enumerate(days):
            clock = 9 * 60 + 30
            for wid, w in wod_ids:
                if w["day"] != day_i:
                    continue
                for d in divs:
                    linked = await c.fetchval(
                        "SELECT 1 FROM wod_divisions WHERE wod_id=$1 AND division_id=$2",
                        wid, d["id"])
                    if not linked:
                        continue
                    lot = entries[d["id"]]
                    for hn in range(0, len(lot), LANES):
                        chunk = lot[hn:hn + LANES]
                        hid = await c.fetchval(
                            """INSERT INTO heats (event_id,wod_id,number,day,
                                    briefing_start,briefing_end,start_time,location)
                               VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
                            eid, wid, hn // LANES + 1, day,
                            hhmm(clock - 15), hhmm(clock - 5), hhmm(clock),
                            "Площадка A" if day_i == 0 else "Главная арена")
                        await c.executemany(
                            """INSERT INTO heat_entries (heat_id,entry_id,lane)
                               VALUES ($1,$2,$3)""",
                            [(hid, e, i + 1) for i, e in enumerate(chunk)])
                        clock += 20
                clock += 15                       # перестановка снаряда между КП

            # Заходы в расписание не дублируем: их время живёт на своей вкладке.
            # Полсотни строк «Комплекс — категория, заход N» делают расписание
            # нечитаемым, а расписание нужно для другого — для дня целиком.
            for t, title, place in DAY_SCHEDULE:
                sched.append((day, t, title, place, None))
            for wid, w in wod_ids:
                if w["day"] != day_i:
                    continue
                first = await c.fetchval(
                    "SELECT MIN(start_time) FROM heats WHERE event_id=$1 AND wod_id=$2",
                    eid, wid)
                if first:
                    sched.append((day, first, f"{w['name']} — начало", 
                                  "Площадка A" if day_i == 0 else "Главная арена", None))
            if day_i == len(days) - 1:
                sched.append((day, "19:30", "Награждение", "Главная арена", None))

        await c.executemany(
            """INSERT INTO schedule (event_id,day,time,title,location,division_id)
               VALUES ($1,$2,$3,$4,$5,$6)""",
            [(eid, d, t, ti, lo, dv) for d, t, ti, lo, dv in sched])

        # ── Результаты ───────────────────────────────────────────────
        # Полностью отсуженные комплексы, один наполовину, остальные пустые:
        # так одновременно видно и живой протокол, и работу, которая осталась.
        filled = 0
        for n, (wid, w) in enumerate(wod_ids):
            share = 1.0 if n < done_wods else 0.5 if n == done_wods else 0.0
            if not share:
                continue
            for d in divs:
                linked = await c.fetchval(
                    "SELECT 1 FROM wod_divisions WHERE wod_id=$1 AND division_id=$2",
                    wid, d["id"])
                if not linked:
                    continue
                lot = entries[d["id"]]
                for e in lot[:max(1, int(len(lot) * share))]:
                    roll = random.random()
                    if roll < 0.04:
                        status, value, tb = "dnf", None, None
                    elif roll < 0.06:
                        status, value, tb = "dns", None, None
                    elif roll < 0.10 and w.get("time_cap"):
                        status, value, tb = "cap", None, None
                    else:
                        status = "ok"
                        if w["result_type"] == "time":
                            value = round(random.uniform(210, 690))
                        elif w["result_type"] == "weight":
                            value = round(random.uniform(50, 145) / 2.5) * 2.5
                        else:
                            value = random.randint(80, 260)
                        # тай-брейк есть только там, где он осмыслен: у AMRAP
                        # это время последнего засчитанного повторения
                        tb = round(random.uniform(200, 480)) if w.get("tiebreak") else None
                    await c.execute(
                        """INSERT INTO results (entry_id,wod_id,value,tiebreak,status,
                                judged_at)
                           VALUES ($1,$2,$3,$4,$5,NOW())""", e, wid, value, tb, status)
                    filled += 1

        # ── Судьи ────────────────────────────────────────────────────
        await c.execute("DELETE FROM event_staff WHERE event_id=$1 AND role='judge'", eid)
        for i in range(judges_n):
            name = athlete_names(1, "М" if i % 2 else "Ж", used)[0]
            uid = await make_user(name, "М" if i % 2 else "Ж")
            await c.execute(
                """INSERT INTO event_staff (event_id,user_id,role)
                   VALUES ($1,$2,'judge')""", eid, uid)
        if pavel:
            await c.execute(
                """INSERT INTO event_staff (event_id,user_id,role,is_creator)
                   VALUES ($1,$2,'organizer',TRUE)
                   ON CONFLICT (event_id,user_id) DO UPDATE SET role='organizer'""",
                eid, pavel["id"])

        counts = await c.fetchrow(
            """SELECT (SELECT COUNT(*) FROM entries WHERE event_id=$1) AS entries,
                      (SELECT COUNT(*) FROM wods WHERE event_id=$1) AS wods,
                      (SELECT COUNT(*) FROM heats WHERE event_id=$1) AS heats,
                      (SELECT COUNT(*) FROM schedule WHERE event_id=$1) AS sched""", eid)

    await pool.close()
    print(f"«{ev['title']}» наполнено: заявок {counts['entries']}, "
          f"комплексов {counts['wods']}, заходов {counts['heats']}, "
          f"строк расписания {counts['sched']}, результатов {filled}.")
    print(f"Отсужено полностью: {min(done_wods, len(WODS))} из {len(WODS)}, "
          f"следующий наполовину. Судей назначено: {judges_n}.")
    if not keep_dates:
        print(f"Даты сдвинуты на {d1}–{d2}, статус «идёт».")


if __name__ == "__main__":
    asyncio.run(main())
