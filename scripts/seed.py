import asyncio
from sqlalchemy import select

from dictapp.db import AsyncSessionMaker
from dictapp.models import Entry


DEMO = [
    Entry(hanzi="你好", pinyin="nǐ hǎo", ru="привет", pos="фраза", examples="你好！你怎么样？"),
    Entry(hanzi="谢谢", pinyin="xièxie", ru="спасибо", pos="фраза", examples="谢谢你的帮助。"),
    Entry(hanzi="学习", pinyin="xuéxí", ru="учиться; изучать", pos="глагол", examples="我在学习中文。"),
]


async def main():
    async with AsyncSessionMaker() as session:
        existing = await session.execute(select(Entry.id).limit(1))
        if existing.first():
            print("Entries already exist, skipping seed.")
            return

        session.add_all(DEMO)
        await session.commit()
        print(f"Seeded {len(DEMO)} entries.")


if __name__ == "__main__":
    asyncio.run(main())

