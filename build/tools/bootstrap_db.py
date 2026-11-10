import asyncio
import sqlite3
import sys

from astrbot.core.db.sqlite import SQLiteDatabase

DB = sys.argv[1]


async def main() -> None:
    db = SQLiteDatabase(DB)
    await db.initialize()


asyncio.run(main())
c = sqlite3.connect(DB)
rows = sorted(r[0] for r in c.execute("select name from sqlite_master where type='table'"))
print(len(rows), "tables")
print(", ".join(rows))
