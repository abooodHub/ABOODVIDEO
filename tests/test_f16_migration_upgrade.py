"""Additive upgrade on a seeded disposable database; no downgrade or stamping."""
import uuid
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine,MetaData,Table,select,text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
import pytest

from app.core.config import settings


@pytest.mark.parametrize("schema_source", ["historical_migrations", "captured_production_schema"])
def test_0019_additive_upgrade_preserves_existing_records(monkeypatch, schema_source):
    origin=make_url(settings.DATABASE_URL_SYNC)
    assert settings.ENVIRONMENT=="test" and origin.host=="127.0.0.1" and origin.port in {15432,25432} and origin.database=="aboodvideo_test"
    name="f16_upgrade_test_"+uuid.uuid4().hex[:12]
    admin=create_engine(origin,isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as db:
            # Generated identifier is fixed-prefix lowercase alphanumeric only.
            db.execute(text(f'CREATE DATABASE "{name}"'))
    finally:admin.dispose()
    target=origin.set(database=name)
    monkeypatch.setattr(settings,"DATABASE_URL_SYNC",target.render_as_string(hide_password=False))
    root=Path(__file__).resolve().parents[1]
    config=Config(str(root/"backend/alembic.ini"));config.set_main_option("script_location",str(root/"backend/migrations"))
    if schema_source == "historical_migrations":
        command.upgrade(config,"0018_playback_geo_country_policy")
    else:
        import hashlib
        capture=root/"tests/fixtures/schema/pre_0019.sql"
        assert hashlib.sha256(capture.read_bytes()).hexdigest()=="9fbf10cc27be0b65ab8cb57e867f3fb2314697f1ed0ad249f0823278e9a1f998"
        import psycopg
        with psycopg.connect(target.set(drivername="postgresql").render_as_string(hide_password=False)) as connection:
            connection.execute(capture.read_text(encoding="utf-8-sig"), prepare=False)
            # Schema-only dump has no version row. This is a newly created,
            # owned disposable database, never a stamp or a live DB operation.
            connection.execute("INSERT INTO public.alembic_version(version_num) VALUES ('0018_playback_geo_country_policy')")

    engine=create_engine(target)
    try:
        tables=MetaData();users=Table("users",tables,autoload_with=engine);videos=Table("videos",tables,autoload_with=engine)
        owner=uuid.uuid4();ids=[uuid.uuid4() for _ in range(3)]
        folder,series,season=uuid.uuid4(),uuid.uuid4(),uuid.uuid4()
        folders=Table("folders",tables,autoload_with=engine);series_table=Table("series",tables,autoload_with=engine);seasons=Table("seasons",tables,autoload_with=engine)
        jobs=Table("transcode_jobs",tables,autoload_with=engine)
        indexes=text("SELECT tablename,indexname,indexdef FROM pg_indexes WHERE schemaname='public' ORDER BY tablename,indexname")
        constraints=text("SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE connamespace='public'::regnamespace ORDER BY conname")
        with engine.begin() as db:
            db.execute(users.insert().values(id=owner,email="seed@test.local",username="seed",hashed_password="synthetic",role="OWNER"))
            db.execute(folders.insert().values(id=folder,user_id=owner,name="Preserved folder"))
            db.execute(series_table.insert().values(id=series,owner_id=owner,title="Preserved series",parent_folder_id=folder))
            db.execute(seasons.insert().values(id=season,series_id=series,season_number=1,display_name="Preserved season"))
            for id,title in zip(ids,["The Expanse","Temple","Experiment"]):
                db.execute(videos.insert().values(id=id,user_id=owner,title=title,source_filename="fixture.mp4",source_size_bytes=123,folder_id=folder,series_id=series,season_id=season))
            db.execute(jobs.insert().values(id=uuid.uuid4(),video_id=ids[0],status="COMPLETED",progress_percent=100))
            uploads=Table("upload_sessions",tables,autoload_with=engine)
            from datetime import datetime,timedelta,timezone
            db.execute(uploads.insert().values(id=uuid.uuid4(),video_id=ids[1],user_id=owner,filename="fixture.mp4",file_size_bytes=123,chunk_size_bytes=123,total_chunks=1,received_chunks=[0],bytes_received=123,temp_directory="/disposable/fixture",expires_at=datetime.now(timezone.utc)+timedelta(hours=1)))
            before={t.name:db.execute(select(t).order_by(*t.primary_key.columns)).all() for t in tables.tables.values()}
            old_indexes=db.execute(indexes).all();old_constraints=db.execute(constraints).all()
        command.upgrade(config,"head")
        upgraded=Table("videos",MetaData(),autoload_with=engine)
        assert set(upgraded.columns.keys())==set(videos.columns.keys())|{"is_test_content"}
        from alembic.migration import MigrationContext
        from alembic.autogenerate import compare_metadata
        from app.models.base import Base
        import app.models
        with engine.connect() as db:
            differences=compare_metadata(MigrationContext.configure(db),Base.metadata)
            assert differences == [], differences
            after=db.execute(select(*[upgraded.c[c.name] for c in videos.columns]).order_by(upgraded.c.id)).all()
            assert before["videos"]==after
            for table in tables.tables.values():
                assert before[table.name]==db.execute(select(table).order_by(*table.primary_key.columns)).all()
            assert old_indexes==db.execute(indexes).all()
            assert old_constraints==db.execute(constraints).all()
            assert db.execute(select(upgraded.c.is_test_content)).scalars().all()==[False]*3
            assert db.execute(text("SELECT version_num FROM alembic_version")).scalar_one()=="0019_explicit_test_content"
        with engine.begin() as db:
            db.execute(upgraded.update().where(upgraded.c.id==ids[2]).values(is_test_content=True))
            assert set(db.execute(select(upgraded.c.title).where(upgraded.c.is_test_content==False)).scalars())=={"The Expanse","Temple"}
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(jobs.insert().values(id=uuid.uuid4(),video_id=ids[0],status="QUEUED"))
                    db.execute(jobs.insert().values(id=uuid.uuid4(),video_id=ids[0],status="PROCESSING"))
            with pytest.raises(IntegrityError):
                with db.begin_nested():
                    db.execute(seasons.insert().values(id=uuid.uuid4(),series_id=uuid.uuid4(),season_number=2,display_name="Invalid foreign key"))
    finally:engine.dispose()
