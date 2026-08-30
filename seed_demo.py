"""
Демо-наполнение платформы: события, атлеты с аватарами, заявки
(личные и командные), комплексы, результаты, заходы, расписание.

Запуск:  ./.venv/bin/python seed_demo.py
Скрипт идемпотентный — очищает демо-данные и создаёт их заново.

Pillow нужен только здесь (генерация аватарок), в requirements.txt его нет.
"""

import os
import io
import time
import asyncio
import random
from datetime import date

import asyncpg
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

load_dotenv()
DATABASE_URL = os.environ["DATABASE_URL"]
DEV_TG_ID = int(os.getenv("DEV_TG_ID", "77180808"))

random.seed(7)  # стабильные результаты между запусками

PALETTE = ["#2f4858", "#33658a", "#86bbd8", "#758e4f", "#f6ae2d", "#f26419",
           "#8d6a9f", "#4f6367", "#b56576", "#6d597a"]


def avatar(text, size=256):
    """Простая аватарка: инициалы на цветном фоне."""
    color = random.choice(PALETTE)
    img = Image.new("RGB", (size, size), color)
    d = ImageDraw.Draw(img)
    initials = "".join(w[0] for w in text.split()[:2]).upper()
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", size // 3)
    except OSError:
        font = ImageFont.load_default()
    box = d.textbbox((0, 0), initials, font=font)
    d.text(((size - box[2] + box[0]) / 2, (size - box[3] + box[1]) / 2 - box[1]),
           initials, fill="white", font=font)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def logo(seed_text, w=900, h=420):
    """Обложка события — абстрактная (в жизни организатор загрузит своё фото).
    Название не рисуем: оно и так выводится под обложкой в карточке."""
    rnd = random.Random(seed_text)
    base = rnd.choice([(28, 34, 46), (38, 30, 30), (26, 40, 38), (34, 28, 44)])
    img = Image.new("RGB", (w, h), base)
    d = ImageDraw.Draw(img, "RGBA")
    # диагональные полосы разной прозрачности — спокойный фон под текст
    for i in range(-h, w, 46):
        alpha = rnd.randint(6, 20)
        d.polygon([(i, h), (i + 30, h), (i + 30 + h, 0), (i + h, 0)], fill=(255, 255, 255, alpha))
    # мягкое затемнение снизу, чтобы карточка не спорила с текстом
    for y in range(h // 2, h):
        k = int(120 * (y - h // 2) / (h / 2))
        d.line([(0, y), (w, y)], fill=(0, 0, 0, k))
    d.rectangle([0, h - 6, w, h], fill=(242, 196, 64, 255))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


MEN = [
    "Павел Ольховик", "Иван Петров", "Олег Ковалёв", "Родион Бородин",
    "Кирилл Дергунов", "Валерий Кудрин", "Николай Титаев", "Владислав Орбан",
    "Артём Алексеев", "Сергей Задимидченко", "Тимур Гиммаров", "Дмитрий Федотов",
]
WOMEN = [
    "Юлия Ященко", "Мария Сидорова", "Дарья Гриценко", "Елена Штанг",
    "Полина Павлова", "Анастасия Чегина", "Ирина Ермилова", "Ольга Лучшева",
    "Лилия Абибулаева", "Регина Закирьянова",
]
CITIES = ["Севастополь", "Краснодар", "Сочи", "Ялта", "Ростов-на-Дону",
          "Симферополь", "Анапа", "Новороссийск"]
GYMS = ["CrossFit Chersonesos", "Арена Юг", "Атлант", "Форт", "Прайд", "Титан"]


async def main():
    pool = await asyncpg.create_pool(DATABASE_URL)
    async with pool.acquire() as c:
        # чистим всё, кроме DEV-пользователя (он привязан к Telegram-входу)
        await c.execute("""
            TRUNCATE results, heat_entries, heats, schedule, wod_divisions, wods,
                     entry_members, entries, divisions, event_staff, events RESTART IDENTITY CASCADE;
        """)
        await c.execute("DELETE FROM users WHERE tg_id <> $1", DEV_TG_ID)

        # ── Люди ─────────────────────────────────────────────────────
        users = {}
        tg = 200000
        for name, gender in [(n, "М") for n in MEN] + [(n, "Ж") for n in WOMEN]:
            city, gym = random.choice(CITIES), random.choice(GYMS)
            if name == "Павел Ольховик":       # это владелец DEV-аккаунта
                uid = await c.fetchval(
                    """UPDATE users SET name=$2, gender=$3, city=$4, gym=$5,
                              photo=$6, photo_v=photo_v+1
                       WHERE tg_id=$1 RETURNING id""",
                    DEV_TG_ID, name, gender, city, gym, avatar(name))
                if uid is None:
                    uid = await c.fetchval(
                        """INSERT INTO users (tg_id,name,gender,city,gym,photo,photo_v)
                           VALUES ($1,$2,$3,$4,$5,$6,1) RETURNING id""",
                        DEV_TG_ID, name, gender, city, gym, avatar(name))
            else:
                tg += 1
                uid = await c.fetchval(
                    """INSERT INTO users (tg_id,name,gender,city,gym,photo,photo_v)
                       VALUES ($1,$2,$3,$4,$5,$6,1) RETURNING id""",
                    tg, name, gender, city, gym, avatar(name))
            users[name] = uid
        owner = users["Павел Ольховик"]

        # ── События ──────────────────────────────────────────────────
        async def make_event(slug, title, city, venue, d1, d2, status, descr,
                             reg1=None, reg2=None, q1=None, q2=None, tg="", ig=""):
            eid = await c.fetchval(
                """INSERT INTO events (slug,title,city,venue,date_start,date_end,
                                       status,description,logo,logo_v,created_by,
                                       reg_opens_at,reg_closes_at,qual_start,qual_end,
                                       telegram,instagram)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17) RETURNING id""",
                slug, title, city, venue, d1, d2, status, descr, logo(title),
                int(time.time()) % 100000, owner,   # версия меняется — кэш обложки сбрасывается
                reg1, reg2, q1, q2, tg, ig)
            await c.execute(
                """INSERT INTO event_staff (event_id,user_id,role,is_creator)
                   VALUES ($1,$2,'organizer',TRUE)""", eid, owner)
            return eid

        ev_battle = await make_event(
            "battle-2027", "Битва за Херсонес 2027", "Севастополь", "Парк Победы",
            date(2027, 8, 8), date(2027, 8, 9), "registration",
            "Главный старт юга. Два дня, четыре комплекса в квалификации и финал для 12 лучших в каждом дивизионе.",
            reg1=date(2027, 4, 1), reg2=date(2027, 7, 20),
            q1=date(2027, 6, 1), q2=date(2027, 6, 14),          # онлайн-отбор
            tg="https://t.me/hersones_cf", ig="https://instagram.com/hersones_cf")
        ev_winter = await make_event(
            "winter-open-2027", "Зимний Open", "Краснодар", "Арена Юг",
            date(2027, 2, 14), date(2027, 2, 15), "live",
            "Зимний отборочный турнир. Три комплекса, финал для сильнейших.",
            reg1=date(2026, 12, 1), reg2=date(2027, 2, 1),      # без онлайн-отбора
            tg="https://t.me/winter_open")
        ev_cup = await make_event(
            "south-cup-2026", "Кубок Юга 2026", "Сочи", "Олимпийский парк",
            date(2026, 5, 20), date(2026, 5, 21), "finished",
            "Завершённый турнир — результаты доступны в лидерборде.",
            reg1=date(2026, 3, 1), reg2=date(2026, 5, 10))

        async def make_div(eid, name, team_size, rule, ord_, price=0):
            return await c.fetchval(
                """INSERT INTO divisions (event_id,name,team_size,gender_rule,ord,price)
                   VALUES ($1,$2,$3,$4,$5,$6) RETURNING id""",
                eid, name, team_size, rule, ord_, price)

        # ── Дивизионы ────────────────────────────────────────────────
        b_men   = await make_div(ev_battle, "Rx Мужчины", 1, "male", 1, 4500)
        b_women = await make_div(ev_battle, "Rx Женщины", 1, "female", 2, 4500)
        b_pairs = await make_div(ev_battle, "Пары микс", 2, "mixed", 3, 8000)   # за команду
        w_men   = await make_div(ev_winter, "Rx Мужчины", 1, "male", 1, 3000)
        w_women = await make_div(ev_winter, "Rx Женщины", 1, "female", 2, 3000)
        c_men   = await make_div(ev_cup, "Rx Мужчины", 1, "male", 1, 2500)
        c_women = await make_div(ev_cup, "Rx Женщины", 1, "female", 2, 2500)

        async def make_entry(eid, did, names, team_name=""):
            entry = await c.fetchval(
                """INSERT INTO entries (event_id,division_id,team_name)
                   VALUES ($1,$2,$3) RETURNING id""", eid, did, team_name)
            for i, n in enumerate(names):
                await c.execute(
                    """INSERT INTO entry_members (entry_id,user_id,is_captain)
                       VALUES ($1,$2,$3)""", entry, users[n], i == 0)
            return entry

        # ── Заявки ───────────────────────────────────────────────────
        # Битва: личные + командные (Павел участвует — увидим во вкладке «Мои»)
        for n in MEN[:8]:
            await make_entry(ev_battle, b_men, [n])
        for n in WOMEN[:7]:
            await make_entry(ev_battle, b_women, [n])
        await make_entry(ev_battle, b_pairs, ["Иван Петров", "Мария Сидорова"], "Морские волки")
        await make_entry(ev_battle, b_pairs, ["Олег Ковалёв", "Дарья Гриценко"], "Шторм")
        await make_entry(ev_battle, b_pairs, ["Кирилл Дергунов", "Елена Штанг"], "Атлант")

        # Зимний Open — идёт сейчас, результаты частично внесены
        w_entries_m = [await make_entry(ev_winter, w_men, [n]) for n in MEN[1:8]]
        w_entries_w = [await make_entry(ev_winter, w_women, [n]) for n in WOMEN[1:6]]
        # Павел тоже заявлен
        w_entries_m.append(await make_entry(ev_winter, w_men, ["Павел Ольховик"]))

        # Кубок Юга — завершён, результаты по всем комплексам
        c_entries_m = [await make_entry(ev_cup, c_men, [n]) for n in MEN[2:9]]
        c_entries_w = [await make_entry(ev_cup, c_women, [n]) for n in WOMEN[2:8]]

        # ── Комплексы ────────────────────────────────────────────────
        async def make_wod(eid, name, descr, ord_, rtype, cap, day, stage, divs, ptable=""):
            wid = await c.fetchval(
                """INSERT INTO wods (event_id,name,description,ord,result_type,time_cap,
                                     day,stage,points_table)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9) RETURNING id""",
                eid, name, descr, ord_, rtype, cap, day, stage, ptable)
            for d in divs:
                await c.execute(
                    "INSERT INTO wod_divisions (wod_id,division_id) VALUES ($1,$2)", wid, d)
            return wid

        await make_wod(ev_battle, "Добро пожаловать", "21-15-9:\nТрастеры (43/30 кг)\nПодтягивания",
                       1, "time", "12:00", date(2027, 8, 8), "qualification", [b_men, b_women, b_pairs])
        await make_wod(ev_battle, "Суши вёсла", "AMRAP 10 мин:\n10 берпи над штангой\n8 становых тяг",
                       2, "reps", "10:00", date(2027, 8, 8), "qualification", [b_men, b_women])
        await make_wod(ev_battle, "Финал", "На время:\n50 двойных\n40 приседаний\n30 отжиманий",
                       3, "time", "8:00", date(2027, 8, 9), "final", [b_men, b_women],
                       "100,90,80,70,60,50,40,30,20,15,10,5")

        w1 = await make_wod(ev_winter, "Открытие", "На время: 21-15-9 трастеры и подтягивания",
                            1, "time", "12:00", date(2027, 2, 14), "qualification", [w_men, w_women])
        w2 = await make_wod(ev_winter, "Максимум", "1ПМ становая тяга",
                            2, "weight", "", date(2027, 2, 14), "qualification", [w_men, w_women])
        w3 = await make_wod(ev_winter, "Финал", "AMRAP 8 мин",
                            3, "reps", "8:00", date(2027, 2, 15), "final", [w_men, w_women])

        c1 = await make_wod(ev_cup, "Разгон", "На время", 1, "time", "10:00",
                            date(2026, 5, 20), "qualification", [c_men, c_women])
        c2 = await make_wod(ev_cup, "Сила", "1ПМ толчок", 2, "weight", "",
                            date(2026, 5, 20), "qualification", [c_men, c_women])
        c3 = await make_wod(ev_cup, "Финал", "На время", 3, "time", "9:00",
                            date(2026, 5, 21), "final", [c_men, c_women],
                            "100,90,80,70,60,50,40,30,20,15,10,5")

        # ── Результаты ───────────────────────────────────────────────
        async def add_result(entry, wid, value, judged=True):
            await c.execute(
                """INSERT INTO results (entry_id,wod_id,value,status,judged_by,judged_at)
                   VALUES ($1,$2,$3,'ok',$4,NOW())""", entry, wid, value, owner if judged else None)

        # Зимний Open идёт: первый комплекс у всех, второй — у части, финал пуст
        for e in w_entries_m + w_entries_w:
            await add_result(e, w1, random.randint(240, 420))          # секунды
        for e in (w_entries_m + w_entries_w)[:7]:
            await add_result(e, w2, random.choice(range(80, 165, 5)))  # кг

        # Кубок Юга завершён: результаты по всем трём комплексам
        for e in c_entries_m + c_entries_w:
            await add_result(e, c1, random.randint(300, 520))
            await add_result(e, c2, random.choice(range(70, 155, 5)))
            await add_result(e, c3, random.randint(180, 360))

        # ── Заходы и расписание (Зимний Open) ────────────────────────
        t = 10 * 60  # 10:00 в минутах
        for i, group in enumerate([w_entries_m[:4], w_entries_m[4:], w_entries_w], start=1):
            hid = await c.fetchval(
                """INSERT INTO heats (event_id,wod_id,number,day,briefing_start,briefing_end,
                                      start_time,location)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8) RETURNING id""",
                ev_winter, w1, i, date(2027, 2, 14),
                f"{(t-20)//60:02d}:{(t-20)%60:02d}", f"{(t-5)//60:02d}:{(t-5)%60:02d}",
                f"{t//60:02d}:{t%60:02d}", "Площадка A")
            for lane, e in enumerate(group, start=1):
                await c.execute(
                    "INSERT INTO heat_entries (heat_id,entry_id,lane) VALUES ($1,$2,$3)", hid, e, lane)
            t += 25

        for time_, title, place in [
            ("09:00", "Регистрация и брифинг", "Главная сцена"),
            ("10:00", "Комплекс 1 — Открытие", "Площадка A"),
            ("13:00", "Комплекс 2 — Максимум", "Площадка B"),
            ("16:00", "Награждение дня", "Главная сцена"),
        ]:
            await c.execute(
                """INSERT INTO schedule (event_id,day,time,title,location)
                   VALUES ($1,$2,$3,$4,$5)""", ev_winter, date(2027, 2, 14), time_, title, place)

        # ── Отчёт ────────────────────────────────────────────────────
        stats = await c.fetchrow("""
            SELECT (SELECT COUNT(*) FROM users)    AS users,
                   (SELECT COUNT(*) FROM events)   AS events,
                   (SELECT COUNT(*) FROM divisions)AS divisions,
                   (SELECT COUNT(*) FROM entries)  AS entries,
                   (SELECT COUNT(*) FROM wods)     AS wods,
                   (SELECT COUNT(*) FROM results)  AS results,
                   (SELECT COUNT(*) FROM heats)    AS heats
        """)
        print("Готово:", ", ".join(f"{k} — {v}" for k, v in dict(stats).items()))

    await pool.close()


asyncio.run(main())
