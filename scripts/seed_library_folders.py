"""Seed production library folders hierarchy and assign real Spirit Cage episodes.

Hierarchy:
مكتبتي
├── Donghua
│   ├── Spirit Cage
│   │   ├── الموسم 1
│   │   │   ├── الحلقة 01
│   │   │   ├── الحلقة 02
│   │   │   └── الحلقة 03
│   │   └── الموسم 2
│   └── Swallowed Star
├── Anime
├── Movies
└── Private

All other test records remain unassigned (folder_id=NULL) under "غير مصنف".
"""
import asyncio
import uuid
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.user import User, UserRole
from app.models.folder import Folder
from app.models.video import Video


async def seed():
    engine = create_async_engine(settings.DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as db:
        # 1. Find Owner
        user_stmt = select(User).where(User.role == UserRole.OWNER).order_by(User.created_at.asc()).limit(1)
        user = (await db.execute(user_stmt)).scalar_one_or_none()
        if not user:
            print("No owner found! Exiting.")
            return

        print(f"Using Owner: {user.email} ({user.id})")

        # 2. Helper to get or create folder
        async def get_or_create_folder(name: str, parent_id=None, folder_type="folder"):
            stmt = select(Folder).where(
                Folder.user_id == user.id,
                Folder.name == name,
                Folder.parent_id == parent_id,
            )
            res = await db.execute(stmt)
            folder = res.scalar_one_or_none()
            if not folder:
                folder = Folder(
                    id=uuid.uuid4(),
                    user_id=user.id,
                    parent_id=parent_id,
                    name=name,
                    folder_type=folder_type,
                    allow_download=True,
                )
                db.add(folder)
                await db.commit()
                await db.refresh(folder)
                print(f"Created folder: {name} (parent: {parent_id}) -> {folder.id}")
            else:
                print(f"Existing folder: {name} -> {folder.id}")
            return folder

        # 3. Build Folders Hierarchy
        donghua = await get_or_create_folder("Donghua")
        spirit_cage = await get_or_create_folder("Spirit Cage", parent_id=donghua.id, folder_type="series")
        season_1 = await get_or_create_folder("الموسم 1", parent_id=spirit_cage.id, folder_type="season")
        season_2 = await get_or_create_folder("الموسم 2", parent_id=spirit_cage.id, folder_type="season")
        swallowed_star = await get_or_create_folder("Swallowed Star", parent_id=donghua.id, folder_type="series")

        anime = await get_or_create_folder("Anime")
        movies = await get_or_create_folder("Movies")
        private = await get_or_create_folder("Private")

        # 4. Assign real Spirit Cage videos to الموسم 1
        sc_ep1_id = uuid.UUID("0b692e45-cd81-4950-8a03-5e87e08c2cef")
        sc_ep2_id = uuid.UUID("59d5bd61-1af2-47fb-a477-4195d963c35a")
        sc_ep3_id = uuid.UUID("572e7658-5f52-4331-8936-968d295ff17d")

        ep1 = await db.get(Video, sc_ep1_id)
        if ep1:
            ep1.folder_id = season_1.id
            ep1.title = "الحلقة 01"
            ep1.series_name = "Spirit Cage"
            ep1.season_number = 1
            ep1.episode_number = 1
            print("Assigned Episode 1 to الموسم 1")

        ep2 = await db.get(Video, sc_ep2_id)
        if ep2:
            ep2.folder_id = season_1.id
            ep2.title = "الحلقة 02"
            ep2.series_name = "Spirit Cage"
            ep2.season_number = 1
            ep2.episode_number = 2
            print("Assigned Episode 2 to الموسم 1")

        ep3 = await db.get(Video, sc_ep3_id)
        if ep3:
            ep3.folder_id = season_1.id
            ep3.title = "الحلقة 03"
            ep3.series_name = "Spirit Cage"
            ep3.season_number = 1
            ep3.episode_number = 3
            print("Assigned Episode 3 to الموسم 1")

        await db.commit()
        print("Seeding completed successfully!")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
