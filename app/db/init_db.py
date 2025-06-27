import logging
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.core.config import settings
from app.schemas.user import UserCreate
from app.services.user import create_user
from app.db.models.user import User

logger = logging.getLogger(__name__)


async def init_db() -> None:
    async with SessionLocal() as db:
        await create_first_superuser(db)


async def create_first_superuser(db: AsyncSession = None) -> None:
    if db is None:
        async with SessionLocal() as db:
            await _create_first_superuser(db)
    else:
        await _create_first_superuser(db)


async def _create_first_superuser(db: AsyncSession) -> None:
    from sqlalchemy import select
    
    if settings.FIRST_SUPERUSER_EMAIL:
        user = await db.execute(
            select(User).where(User.email == settings.FIRST_SUPERUSER_EMAIL)
        )
        user = user.scalars().first()
        
        if not user:
            user_in = UserCreate(
                email=settings.FIRST_SUPERUSER_EMAIL,
                password=settings.FIRST_SUPERUSER_PASSWORD,
                full_name=settings.FIRST_SUPERUSER_FULLNAME,
                is_superuser=True,
                is_verified=True,
            )
            user = await create_user(db, user_in)
            logger.info(f"Superuser {settings.FIRST_SUPERUSER_EMAIL} created")
        else:
            logger.info(f"Superuser {settings.FIRST_SUPERUSER_EMAIL} already exists")