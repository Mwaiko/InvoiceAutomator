"""
app/db/session.py

Updated Async SQLAlchemy engine + session factory for Supabase.
"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from app.config import settings

# For Supabase, we must disable prepared statement caching if using port 6543 (Transaction Mode)
# and ensure SSL is handled for secure remote connections.
engine = create_async_engine(
    settings.database_url,
    echo=settings.app_debug,       # SQL query logging in dev
    pool_pre_ping=True,            # Detect stale connections
    pool_size=10,                  # Adjust based on your Supabase tier
    max_overflow=20,
    connect_args={
        "prepared_statement_cache_size": 0,  # CRITICAL for Supabase Pooler
        "statement_cache_size": 0,           # CRITICAL for Supabase Pooler
    },
)

AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,        # Keeps objects usable after commit
    autoflush=False,
    autocommit=False,
)


async def get_db() -> AsyncSession:
    """
    Dependency for FastAPI routes to provide a database session.
    Automatically handles cleanup and rollbacks on errors.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
            # Note: Explicitly calling commit here is optional; 
            # many prefer to commit inside the business logic.
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()