from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text, select, delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from config import DATABASE_URL

logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, connect_args={"check_same_thread": False})
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class Post(Base):
    __tablename__ = "posts"

    id: int = Column(Integer, primary_key=True, autoincrement=True)
    code: str = Column(String(64), unique=True, nullable=False, index=True)
    text: str = Column(Text, nullable=False)
    image_url: Optional[str] = Column(String(2048), nullable=True)
    _buttons: str = Column("buttons", Text, nullable=False, default="[]")
    is_active: bool = Column(Boolean, nullable=False, default=True)
    created_at: datetime = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    trigger_count: int = Column(Integer, nullable=False, default=0)

    @property
    def buttons(self) -> List[Dict[str, str]]:
        return json.loads(self._buttons)

    @buttons.setter
    def buttons(self, value: List[Dict[str, str]]) -> None:
        self._buttons = json.dumps(value[:3])


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("DB ready")


async def create_post(code, text, image_url, buttons) -> Post:
    async with AsyncSessionLocal() as s:
        post = Post(code=code.upper(), text=text, image_url=image_url)
        post.buttons = buttons
        s.add(post)
        await s.commit()
        await s.refresh(post)
        return post


async def get_post(code: str) -> Optional[Post]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Post).where(Post.code == code.upper(), Post.is_active == True))
        return r.scalar_one_or_none()


async def get_post_any(code: str) -> Optional[Post]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Post).where(Post.code == code.upper()))
        return r.scalar_one_or_none()


async def list_posts() -> List[Post]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Post).order_by(Post.created_at.desc()))
        return list(r.scalars().all())


async def update_post(code, *, text=None, image_url=None, buttons=None, is_active=None) -> Optional[Post]:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Post).where(Post.code == code.upper()))
        post = r.scalar_one_or_none()
        if not post:
            return None
        if text is not None:
            post.text = text
        if image_url is not None:
            post.image_url = image_url
        if buttons is not None:
            post.buttons = buttons
        if is_active is not None:
            post.is_active = is_active
        await s.commit()
        await s.refresh(post)
        return post


async def delete_post(code: str) -> bool:
    async with AsyncSessionLocal() as s:
        r = await s.execute(delete(Post).where(Post.code == code.upper()))
        await s.commit()
        return r.rowcount > 0


async def bump_trigger(code: str) -> None:
    async with AsyncSessionLocal() as s:
        r = await s.execute(select(Post).where(Post.code == code.upper()))
        post = r.scalar_one_or_none()
        if post:
            post.trigger_count += 1
            await s.commit()
