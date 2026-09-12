"""Привязка события платформы к внешнему соревновательному модулю.

  DATABASE_URL=... python link_feed.py <event_id> <base_url> [Дивизион=Категория/Пол ...]

Пример:
  python link_feed.py 1 https://comp.example.com \
      "Rx Мужчины=Продвинутые/М" "Rx Женщины=Продвинутые/Ж" "Scaled Мужчины=Новички/М"

Пустой base_url отвязывает событие — оно вернётся к своим таблицам.
"""
import asyncio, os, sys, asyncpg

async def main():
    if len(sys.argv) < 3:
        print(__doc__); return
    eid, base, pairs = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
    c = await asyncpg.connect(os.environ["DATABASE_URL"])
    await c.execute("UPDATE events SET feed_url=$2 WHERE id=$1", eid, base)
    print(("отвязано" if not base else f"событие {eid} → {base}"))
    for pair in pairs:
        name, _, key = pair.partition("=")
        cat, _, gen = key.partition("/")
        n = await c.execute(
            """UPDATE divisions SET feed_category=$3, feed_gender=$4
               WHERE event_id=$1 AND name=$2""", eid, name.strip(), cat.strip(), gen.strip())
        print(f"  {name.strip():20} → {cat.strip()}/{gen.strip()}  {'ок' if n.endswith('1') else 'НЕ НАЙДЕН'}")
    print("\nбез сопоставления (в протокол модуля не попадут):")
    for r in await c.fetch(
            "SELECT name FROM divisions WHERE event_id=$1 AND feed_category='' ORDER BY ord", eid):
        print("  ", r["name"])
    await c.close()

asyncio.run(main())
