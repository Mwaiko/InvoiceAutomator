"""
app/db/session.py

Async SQLAlchemy engine + session factory.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,       # SQL query logging in dev
    pool_pre_ping=True,            # detect stale connections
    pool_size=10,
    max_overflow=20,
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,        # keep objects usable after commit
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncSession:
    
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()