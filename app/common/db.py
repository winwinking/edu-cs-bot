"""async engine + session 工厂。全项目只有这一份 engine，避免各处各建一套连接池。"""
import asyncio
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.common.config import get_settings

settings = get_settings()

# connect_args 里的 timeout 是 asyncpg 建立连接的超时；pool_pre_ping 避免拿到已被服务端断开的连接
engine = create_async_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=10,
    connect_args={"timeout": settings.db_connect_timeout_seconds},
)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """所有 ORM 模型（步骤 1.4）的基类"""


async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def check_db_connection() -> bool:
    """给 /ready 探活用；超时了就当不健康，不能让 /ready 被一个卡住的连接拖死"""
    try:
        async with asyncio.timeout(settings.db_connect_timeout_seconds):
            async with engine.connect() as conn:
                await conn.exec_driver_sql("SELECT 1")
        return True
    except Exception:
        return False
