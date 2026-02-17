from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dictapp.settings import settings

engine = create_async_engine(
    settings.database_url,
    echo=False,
)

AsyncSessionMaker = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)

async def get_session() -> AsyncSession:
    async with AsyncSessionMaker() as session:
        yield session

