"""SQLAlchemy 2.0 Database Connection & Session Management."""
import time
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Any
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import declarative_base, sessionmaker
from app.core.config import settings
from app.core.logging import logger

# 1. Async Engine for FastAPI Request Lifecycle
async_engine = create_async_engine(
    settings.DATABASE_URL,
    echo=settings.DEBUG,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_pre_ping=True,
    pool_recycle=3600,
)

AsyncSessionLocal = async_sessionmaker(
    bind=async_engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)

# 2. Sync Engine for Migrations, CLI utilities, and Bootstrap
sync_engine = create_engine(
    settings.DATABASE_URL_SYNC,
    echo=False,
    pool_pre_ping=True,
    pool_recycle=3600,
)

SyncSessionLocal = sessionmaker(
    bind=sync_engine,
    autocommit=False,
    autoflush=False,
)

# Base model
Base = declarative_base()


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """Dependency that provides an async database session per request."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


@asynccontextmanager
async def get_db_context() -> AsyncGenerator[AsyncSession, None]:
    """Context manager that provides an async database session for scripts/tests/background tasks."""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def check_db_health() -> Dict[str, Any]:
    """Probe database health and response latency."""
    start_time = time.perf_counter()
    try:
        async with async_engine.connect() as conn:
            result = await conn.execute(text("SELECT 1"))
            row = result.scalar()
            latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
            if row == 1:
                return {
                    "status": "healthy",
                    "latency_ms": latency_ms,
                    "database": settings.POSTGRES_DB,
                }
            return {
                "status": "unhealthy",
                "error": "Unexpected query result",
                "latency_ms": latency_ms,
            }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start_time) * 1000, 2)
        logger.error(f"Database health probe failed; error_type={type(exc).__name__}")
        return {
            "status": "unhealthy",
            "error": "Database probe is temporarily unavailable.",
            "code": "DATABASE_UNAVAILABLE",
            "latency_ms": latency_ms,
        }


async def close_db_connections() -> None:
    """Graceful shutdown: close all pooled database connections."""
    logger.info("Disposing async database connection pool...")
    await async_engine.dispose()
    logger.info("Database pool disposed.")
