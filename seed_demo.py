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
from datetime import date, datetime, timedelta, timezone

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


# Афиши организаторов почти всегда свёрстаны на тёмном — демо-картинки
# повторяют это, иначе на стенде не видно, как они стыкуются с фоном.
BANNER_INKS = [(214, 58, 48), (232, 122, 40), (54, 118, 214), (196, 196, 196)]


def banner(seed_text, w=1600, h=900):
    """Афиша события 16:9 — идёт во всю ширину экрана.

    Абстрактная: в жизни организатор приносит своё фото. Композиций несколько,
    иначе лента из десятка событий выглядит одной картинкой и по ней нельзя
    судить о вёрстке. Название не рисуем — оно выводится под афишей."""
    rnd = random.Random(seed_text)
    ink = rnd.choice(BANNER_INKS)
    img = Image.new("RGB", (w, h), (10, 11, 13))
    d = ImageDraw.Draw(img, "RGBA")
    kind = rnd.choice(["diagonal", "block", "arc", "bars"])

    if kind == "diagonal":
        for i in range(-h, w, 78):
            d.polygon([(i, h), (i + 52, h), (i + 52 + h, 0), (i + h, 0)],
                      fill=(255, 255, 255, rnd.randint(4, 14)))
        x0 = rnd.randint(w // 5, w // 2)
        d.polygon([(x0, h), (x0 + 120, h), (x0 + 120 + h, 0), (x0 + h, 0)], fill=ink + (150,))

    elif kind == "block":
        cut = rnd.randint(int(w * 0.32), int(w * 0.55))
        d.rectangle([0, 0, cut, h], fill=ink + (140,))
        for y in range(0, h, 34):
            d.rectangle([cut, y, w, y + 16], fill=(255, 255, 255, rnd.randint(4, 11)))

    elif kind == "arc":
        cx, cy = rnd.randint(int(w * 0.3), int(w * 0.7)), h // 2
        for r in range(int(h * 0.62), 0, -int(h * 0.11)):
            d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ink + (120,), width=9)

    else:  # bars
        x = 0
        while x < w:
            bw = rnd.choice([26, 42, 70, 110])
            if rnd.random() < 0.22:
                d.rectangle([x, 0, x + bw, h], fill=ink + (135,))
            else:
                d.rectangle([x, 0, x + bw, h], fill=(255, 255, 255, rnd.randint(3, 12)))
            x += bw + rnd.choice([14, 22, 34])

    # общее затемнение: интерфейс не должен спорить с афишей, поэтому даже
    # демо-картинка держится в тёмном диапазоне
    d.rectangle([0, 0, w, h], fill=(8, 9, 10, 105))
    # виньетка по краям: центральный кроп на узком экране остаётся спокойным
    for x in range(w // 4):
        k = int(150 * (1 - x / (w / 4)))
        d.line([(x, 0), (x, h)], fill=(0, 0, 0, k))
        d.line([(w - 1 - x, 0), (w - 1 - x, h)], fill=(0, 0, 0, k))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=86)
    return buf.getvalue()


def news_image(seed_text, w=1200, h=675):
    """Картинка новости. Демонстрационная — в жизни редактор загрузит своё фото."""
    return banner(seed_text, w, h)


def mark(seed_text, size=512):
    """Квадратный знак 1:1 — показывается кругом рядом с названием.

    PNG с прозрачностью: знак ложится на подложку приложения, а не тащит
    свой фон. Рисунок держим в пределах 76% ширины — углы срежет кроп."""
    rnd = random.Random(seed_text + "mark")
    ink = rnd.choice(BANNER_INKS)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = int(size * 0.12)
    d.ellipse([pad, pad, size - pad, size - pad], fill=(18, 19, 22, 255))
    r = int(size * 0.26)
    c = size // 2
    d.regular_polygon((c, c, r), n_sides=6, rotation=rnd.choice([0, 30]),
                      fill=ink + (255,))
    d.regular_polygon((c, c, int(r * 0.52)), n_sides=6, rotation=rnd.choice([0, 30]),
                      fill=(242, 241, 238, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
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
            TRUNCATE news, results, heat_entries, heats, schedule, wod_divisions, wods,
                     entry_members, entries, divisions, event_staff, events,
                     clubs RESTART IDENTITY CASCADE;
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
                             reg1=None, reg2=None, q1=None, q2=None, tg="", ig="",
                             with_banner=True, with_mark=True, organizer=None):
            # часть событий намеренно идёт без картинок: так на стенде видно
            # запасные варианты — типографическую плашку и букву в круге
            eid = await c.fetchval(
                """INSERT INTO events (slug,title,city,venue,date_start,date_end,
                                       status,description,banner,banner_v,mark,mark_v,
                                       created_by,
                                       reg_opens_at,reg_closes_at,qual_start,qual_end,
                                       telegram,instagram)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
                   RETURNING id""",
                slug, title, city, venue, d1, d2, status, descr,
                # версия меняется при каждом сиде — кэш картинок сбрасывается
                banner(title) if with_banner else None,
                int(time.time()) % 100000 if with_banner else 0,
                mark(title) if with_mark else None,
                int(time.time()) % 100000 if with_mark else 0,
                owner, reg1, reg2, q1, q2, tg, ig)
            # организатор у каждого старта свой: если посадить владельца на все,
            # у него в «Работе на стартах» окажется весь каталог
            await c.execute(
                """INSERT INTO event_staff (event_id,user_id,role,is_creator)
                   VALUES ($1,$2,'organizer',TRUE)""", eid, organizer or owner)
            return eid

        ev_battle = await make_event(
            "battle-2027", "Битва за Херсонес 2027", "Севастополь", "Парк Победы",
            date(2027, 8, 8), date(2027, 8, 9), "registration",
            "Главный старт юга. Два дня, четыре комплекса в квалификации и финал для 12 лучших в каждой категории.",
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
            reg1=date(2026, 3, 1), reg2=date(2026, 5, 10),
            tg="https://t.me/south_cup_2026")

        # ── Ещё события: наполняют каталог, чтобы лента и сегменты
        # проверялись на реальной плотности. Комплексов и результатов у них нет —
        # это витринные записи. Сроки заданы относительно сегодняшнего дня,
        # иначе обратный отсчёт на стенде никогда не показывается.
        today = date.today()
        # Павла в витринные события не записываем: иначе отметка «вы заявлены»
        # стоит на каждой строке каталога и перестаёт что-либо значить
        OTHERS = [n for n in MEN if n != "Павел Ольховик"]

        def days(n):
            return today + timedelta(days=n)

        FILLER = [
            # slug, название, город, площадка, старт, длит., статус,
            # закрытие регистрации, цена, размер команды, афиша, знак
            ("autumn-throwdown-2026", "Осенний Throwdown", "Ростов-на-Дону", "Дон-Арена",
             days(38), 1, "registration", days(9), 3500, 1, True, True),
            ("baltic-cup-2026", "Кубок Балтики 2026", "Калининград", "Янтарь-холл",
             days(24), 2, "registration", days(3), 4000, 1, True, True),
            ("ural-games-2026", "Ural Games 2026", "Екатеринбург", "Экспо-центр",
             days(0), 2, "live", days(-14), 5000, 1, True, True),
            ("siberia-open-2026", "Открытый чемпионат Сибири по функциональному многоборью",
             "Новосибирск", "Спорт-комплекс «Заря»",
             days(61), 2, "registration", days(47), 4200, 1, False, True),
            ("volga-fit-2026", "Volga Fit Fest", "Казань", "Ак Барс Арена",
             days(52), 1, "registration", days(30), 3000, 1, True, False),
            ("moscow-winter-2027", "Зимний кубок Москвы", "Москва", "ЦСКА Арена",
             days(140), 2, "registration", days(112), 6000, 1, True, True),
            ("vladivostok-team-2026", "Team Battle Владивосток", "Владивосток", "Фетисов Арена",
             days(45), 1, "registration", days(21), 0, 2, True, True),
            ("volga-gp-2026", "Гран-при Поволжья 2026", "Самара", "МТЛ Арена",
             days(-56), 2, "finished", days(-84), 2800, 1, True, True),
            ("crimea-qual-2026", "Крымский отбор 2026", "Симферополь", "Арена Юг",
             days(-21), 1, "finished", days(-49), 2000, 1, True, True),
        ]

        for slug, title, city, venue, d1, dur, status, reg_close, price, tsize, ban, mk in FILLER:
            eid = await make_event(
                slug, title, city, venue, d1, d1 + timedelta(days=dur - 1), status,
                f"{title} — тестовое событие каталога.",
                reg1=reg_close - timedelta(days=60), reg2=reg_close,
                # канал есть у каждого старта: блок «Организатор» — часть карточки,
                # а не украшение, и на стенде он не должен пропадать через раз
                tg="https://t.me/" + slug.replace("-", "_"),
                with_banner=ban, with_mark=mk,
                # каталог наполняют чужие старты, а не свои
                organizer=users[OTHERS[sum(map(ord, slug)) % len(OTHERS)]])
            # набор уровней у каждого старта свой — так на превью видно разные плашки
            LEVEL_SETS = [["rx"], ["inter", "rx"], ["bg", "inter", "rx"], ["rx", "elite"],
                          ["inter", "rx", "elite"]]
            levels = LEVEL_SETS[sum(map(ord, slug)) % len(LEVEL_SETS)]
            LEVEL_NAME = {"bg": "Beginners", "inter": "Intermediate",
                          "rx": "Rx", "elite": "Elite"}
            if tsize > 1:
                did = await c.fetchval(
                    """INSERT INTO divisions (event_id,name,team_size,gender_rule,ord,price,level)
                       VALUES ($1,'Rx МЖ',$2,'mixed',1,$3,'rx') RETURNING id""",
                    eid, tsize, price)
                pairs = list(zip(OTHERS[:4], WOMEN[:4]))
                for i, (m, w) in enumerate(pairs):
                    e = await c.fetchval(
                        """INSERT INTO entries (event_id,division_id,team_name)
                           VALUES ($1,$2,$3) RETURNING id""", eid, did, f"Команда {i + 1}")
                    for j, n in enumerate((m, w)):
                        await c.execute(
                            """INSERT INTO entry_members (entry_id,user_id,is_captain)
                               VALUES ($1,$2,$3)""", e, users[n], j == 0)
            else:
                combos = [(lv, g) for lv in levels for g in ("male", "female")]
                for ord_, (lv, gender) in enumerate(combos, 1):
                    names = OTHERS if gender == "male" else WOMEN
                    did = await c.fetchval(
                        """INSERT INTO divisions (event_id,name,team_size,gender_rule,ord,price,level)
                           VALUES ($1,$2,1,$3,$4,$5,$6) RETURNING id""",
                        eid,
                        f"{LEVEL_NAME[lv]} {'Мужчины' if gender == 'male' else 'Женщины'}",
                        gender, ord_, price, lv)
                    take = random.Random(slug + lv + gender).randint(2, 5)
                    for n in names[:take]:
                        e = await c.fetchval(
                            """INSERT INTO entries (event_id,division_id)
                               VALUES ($1,$2) RETURNING id""", eid, did)
                        await c.execute(
                            """INSERT INTO entry_members (entry_id,user_id,is_captain)
                               VALUES ($1,$2,TRUE)""", e, users[n])
                    if lv == "rx" and gender == "male" and slug in ("ural-games-2026", "baltic-cup-2026"):
                        e = await c.fetchval(
                            """INSERT INTO entries (event_id,division_id)
                               VALUES ($1,$2) RETURNING id""", eid, did)
                        await c.execute(
                            """INSERT INTO entry_members (entry_id,user_id,is_captain)
                               VALUES ($1,$2,TRUE)""", e, users["Павел Ольховик"])

        # ── Клубы ────────────────────────────────────────────────────
        # Заглушка до реального справочника. «Независимый атлет» сюда не кладём:
        # это отсутствие клуба (club_id = NULL), а не строка справочника.
        CLUBS = [
            ("CrossFit Херсонес", "Севастополь"),
            ("Арена Юг", "Симферополь"),
            ("CrossFit Кубань", "Краснодар"),
            ("Северный Ветер", "Санкт-Петербург"),
            ("Сталь", "Екатеринбург"),
            ("Волга Атлетик", "Казань"),
            ("Первый Дивизион", "Москва"),
            ("Тихий Океан", "Владивосток"),
        ]
        club_ids = {}
        for cname, ccity in CLUBS:
            club_ids[cname] = await c.fetchval(
                """INSERT INTO clubs (name, city) VALUES ($1,$2) RETURNING id""", cname, ccity)
        # раздаём клубы атлетам, часть оставляем независимыми
        all_club_ids = list(club_ids.values())
        for i, (uname, uid) in enumerate(users.items()):
            await c.execute("UPDATE users SET club_id=$2, height_cm=$3, weight_kg=$4 WHERE id=$1",
                            uid,
                            None if i % 5 == 0 else all_club_ids[i % len(all_club_ids)],
                            165 + (i * 3) % 30,
                            round(58 + (i * 3.7) % 42, 1))

        # ── Новости ──────────────────────────────────────────────────
        # (раздел, в слайдере, заголовок, краткое, текст)
        NEWS = [
            (True,  "Отбор на финал: правила сезона 2026",
             "Федерация опубликовала регламент отбора — четыре онлайн-этапа и два очных.",
             "Регламент закрепляет четыре онлайн-этапа с открытой подачей видео и два "
             "очных отбора. Проходной порог в финал — 40 лучших в каждом дивизионе."),
            (True,  "Битва за Херсонес объявила комплексы квалификации",
             "Четыре комплекса опубликованы за два месяца до старта.",
             "Организаторы выложили все четыре комплекса квалификации заранее, "
             "чтобы атлеты успели спланировать подготовку."),
            (True,  "Рекорд России в трастере побит на Кубке Юга",
             "Новая отметка — 142,5 кг в дивизионе Rx.",
             "Попытка состоялась в третьем комплексе финального дня."),
            (False, "Судейский семинар пройдёт в Казани",
             "Двухдневный курс для судей региональных стартов, набор открыт.",
             "Курс покрывает стандарты движений, работу с протоколом и разбор спорных ситуаций."),
            (False, "Зимний Open расширил сетку до трёх дивизионов",
             "Добавлен дивизион для новичков без опыта соревнований.",
             "Организаторы отмечают рост числа заявок от атлетов первого года занятий."),
            (False, "Как читать протокол: разбор системы баллов",
             "Почему место в комплексе важнее абсолютного результата.",
             "Разбираем, как очки за отдельные комплексы складываются в итоговую таблицу."),
            (False, "Открыт приём заявок на Кубок Балтики",
             "Регистрация закроется через неделю.",
             "Осталось ограниченное число мест в дивизионе Rx мужчины."),
        ]
        # мировой кроссфит — чтобы вкладка «CF Мир» не была пустой
        WORLD = [
            (True,  "CrossFit Games 2026: обновлён формат финала",
             "Организаторы сократили число финалистов и добавили командный день.",
             "В финал выходят 30 атлетов вместо 40, добавлен отдельный командный день."),
            (False, "Европейский отбор пройдёт в Мадриде",
             "Даты и площадка объявлены за полгода до старта.",
             "Площадка рассчитана на четыре тысячи зрителей."),
            (False, "Новые стандарты движений в гимнастике",
             "Уточнены требования к выходу силой и подъёму разгибом.",
             "Изменения вступают в силу со следующего сезона."),
        ]
        CATS = ["ru"] * len(NEWS) + ["world"] * len(WORLD)
        NEWS = NEWS + WORLD
        for i, (feat, title, summary, body_text) in enumerate(NEWS):
            await c.execute(
                """INSERT INTO news (title, summary, body, image, image_v,
                                     is_featured, published_at, source_url, category)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
                title, summary, body_text,
                news_image(title) if feat or i % 2 == 0 else None,
                int(time.time()) % 100000 if feat or i % 2 == 0 else 0,
                feat, datetime.now(timezone.utc) - timedelta(days=i * 2, hours=i),
                "https://t.me/crossfit_ru" if i % 3 == 0 else "",
                CATS[i])

        async def make_div(eid, name, team_size, rule, ord_, price=0, level=''):
            return await c.fetchval(
                """INSERT INTO divisions (event_id,name,team_size,gender_rule,ord,price,level)
                   VALUES ($1,$2,$3,$4,$5,$6,$7) RETURNING id""",
                eid, name, team_size, rule, ord_, price, level)

        # ── Дивизионы ────────────────────────────────────────────────
        b_men   = await make_div(ev_battle, "Rx Мужчины", 1, "male", 1, 4500, "rx")
        b_women = await make_div(ev_battle, "Rx Женщины", 1, "female", 2, 4500, "rx")
        b_pairs = await make_div(ev_battle, "Rx МЖ", 2, "mixed", 3, 8000, "rx")
        await make_div(ev_battle, "Intermediate Мужчины", 1, "male", 4, 3500, "inter")
        await make_div(ev_battle, "Elite Мужчины", 1, "male", 5, 6000, "elite")
        w_men   = await make_div(ev_winter, "Rx Мужчины", 1, "male", 1, 3000, "rx")
        w_women = await make_div(ev_winter, "Rx Женщины", 1, "female", 2, 3000, "rx")
        await make_div(ev_winter, "Beginners Мужчины", 1, "male", 3, 2000, "bg")
        c_men   = await make_div(ev_cup, "Rx Мужчины", 1, "male", 1, 2500, "rx")
        c_women = await make_div(ev_cup, "Rx Женщины", 1, "female", 2, 2500, "rx")

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



if __name__ == "__main__":
    asyncio.run(main())
