"""Disposable Linux API/RQ workers, actual HTTP, PostgreSQL, Redis and FFmpeg."""
import os
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from contextlib import contextmanager

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker


ROOT=Path(__file__).resolve().parents[1]
IMAGE=os.getenv("ABV_INTEGRATION_IMAGE", "abv-real-e2e:20261014")
if os.getenv("ABV_INTEGRATION_IMAGE") and not IMAGE.startswith("abv-staging-candidate:"):
    raise RuntimeError("The optional candidate image must use the isolated staging namespace")
LABEL="real-integration"
NETWORK="abv-real-e2e-net"
VOLUME="abv-real-e2e-storage"


def docker(*args, timeout=120):
    result=subprocess.run(["docker",*map(str,args)],capture_output=True,text=True,encoding="utf-8",errors="replace",timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Docker operation failed ({args[:3]}): {result.stderr[-2000:]}")
    return (result.stdout + result.stderr if args[0]=="logs" else result.stdout).strip()


def assert_owned(name):
    assert docker("inspect","--format",'{{ index .Config.Labels "abv.purpose" }}',name)==LABEL,f"Unowned test container: {name}"


class RealPlatform:
    api_name="abv-real-e2e-api"
    workers=["abv-real-e2e-worker-1","abv-real-e2e-worker-2"]
    base="http://127.0.0.1:28080"

    def __init__(self):
        assert os.environ.get("ABV_RUN_REAL_INTEGRATION")=="1"
        assert_owned("abv-f16-pg");assert_owned("abv-f16-redis")
        self.temp=Path(tempfile.mkdtemp(prefix="abv-real-platform-"))
        self.env_file=self.temp/"fixture.env"
        env={"ENVIRONMENT":"test","DEBUG":"false","SECURITY_MODE":"lan","FORCE_HTTPS":"false",
             "POSTGRES_SERVER":"pg","POSTGRES_USER":"fixture","POSTGRES_PASSWORD":"fixture_only","POSTGRES_DB":"aboodvideo_e2e_test",
             "SECRET_KEY":"isolated-http-worker-fixture-signing-material","IP_PRIVACY_SALT":"isolated-fixture-privacy-material",
             "DATABASE_URL":"postgresql+asyncpg://fixture:fixture_only@pg:5432/aboodvideo_e2e_test",
             "DATABASE_URL_SYNC":"postgresql+psycopg://fixture:fixture_only@pg:5432/aboodvideo_e2e_test",
             "REDIS_QUEUE_URL":"redis://redis:6379/5","REDIS_SESSION_URL":"redis://redis:6379/6","REDIS_CACHE_URL":"redis://redis:6379/7",
             "ALLOWED_HOSTS":"localhost,127.0.0.1,test","TRUSTED_PROXIES":"127.0.0.1,::1",
             "STORAGE_BASE_PATH":"/storage","STORAGE_UPLOADS_PATH":"/storage/uploads","STORAGE_VIDEOS_PATH":"/storage/videos",
             "STORAGE_TEMP_PATH":"/storage/temp","STORAGE_PACKAGES_STAGING_PATH":"/storage/staging/packages",
             "SERVE_MEDIA_VIA_NGINX":"false","FFMPEG_PRESET":"ultrafast","HLS_SEGMENT_DURATION_SECONDS":"2","FFMPEG_THREADS":"1"}
        self.env_file.write_text("\n".join(f"{k}={v}" for k,v in env.items()),encoding="utf-8")
        self.engine=create_engine("postgresql+psycopg://fixture:fixture_only@127.0.0.1:25432/aboodvideo_e2e_test")
        self.session=sessionmaker(self.engine,expire_on_commit=False)
        self.created=[]
        self.identities={}

    def options(self):
        # An explicitly built candidate must execute the code baked into that
        # image. The default fast regression image instead mounts current source.
        source_mounts=[] if os.getenv("ABV_INTEGRATION_IMAGE") else [
                "--mount",f"type=bind,src={ROOT/'backend/app'},dst=/app/app,readonly",
                "--mount",f"type=bind,src={ROOT/'backend/migrations'},dst=/app/migrations,readonly",
                "--mount",f"type=bind,src={ROOT/'backend/alembic.ini'},dst=/app/alembic.ini,readonly"]
        return ["--network",NETWORK,"--env-file",str(self.env_file),"--label",f"abv.purpose={LABEL}",
                "--label",f"abv.run={os.getenv('ABV_INTEGRATION_RUN_ID', 'external-fixture')}",
                *source_mounts,"--mount",f"type=volume,src={VOLUME},dst=/storage"]

    def start(self):
        docker("run","--rm",*self.options(),IMAGE,"python","-m","alembic","upgrade","head")
        docker("run","-d","--name",self.api_name,*self.options(),"-p","127.0.0.1:28080:8000",IMAGE,
               "python","-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8000","--workers","2","--no-access-log")
        self.created.append(self.api_name)
        for name in self.workers:self.start_worker(name)
        for _ in range(150):
            try:
                if httpx.get(self.base+"/health",timeout=1).status_code==200:break
            except httpx.HTTPError:pass
            time.sleep(.2)
        else:raise RuntimeError("Disposable API failed to become healthy")
        self.seed()
        from studio.config import load_studio_config, save_studio_config
        self.previous_studio_config = load_studio_config()
        library = self.temp / "empty-font-library"; library.mkdir()
        save_studio_config({"font_library_folder": str(library)})
        return self

    def start_worker(self,name):
        assert name in self.workers
        docker("run","-d","--name",name,*self.options(),IMAGE,"python","-m","app.workers.transcode_worker")
        if name not in self.created:self.created.append(name)

    def restart_worker(self,name):
        assert name in self.workers;assert_owned(name)
        docker("rm","-f",name);self.start_worker(name)

    def seed(self):
        from app.models.user import User,UserRole
        from app.models.api_key import ApiKey
        from app.security.hashing import hash_password
        from app.security.api_key import generate_raw_api_key,hash_api_key
        from app.models.setting import SystemSetting
        from app.services.quality_settings import SETTING_KEY
        with self.session() as db:
            for role in ["OWNER","ADMIN","OTHER"]:
                name="real-"+role.lower()+"-"+uuid.uuid4().hex[:8]
                user=User(id=uuid.uuid4(),email=name+"@fixture.test",username=name,role=UserRole.OWNER if role=="OTHER" else UserRole(role),hashed_password=hash_password("Fixture-only-password-2026!"),is_active=True)
                db.add(user);db.flush();key=generate_raw_api_key()
                db.add(ApiKey(id=uuid.uuid4(),user_id=user.id,name="Disposable integration",prefix=key[:12],key_hash=hash_api_key(key),scopes=["uploads:create","uploads:write","videos:create","packages:create","packages:write","packages:publish"]))
                self.identities[role]={"user":user.id,"username":name,"key":key}
            setting=db.get(SystemSetting,SETTING_KEY)
            if setting:setting.value='360p,480p,720p'
            else:db.add(SystemSetting(key=SETTING_KEY,value='360p,480p,720p'))
            db.commit()

    @contextmanager
    def client(self,role="OWNER"):
        client=httpx.Client(base_url=self.base,timeout=30)
        response=client.post("/api/v1/auth/login",json={"username_or_email":self.identities[role]["username"],"password":"Fixture-only-password-2026!"})
        assert response.status_code==200,response.text
        client.headers["X-CSRF-Token"]=response.json()["csrf_token"]
        try:yield client
        finally:client.close()

    def synthetic(self,seconds=8,width=1280,height=720):
        name="fixture-"+uuid.uuid4().hex+".mp4"
        docker("exec",self.api_name,"mkdir","-p","/storage/fixtures")
        docker("exec",self.api_name,"ffmpeg","-hide_banner","-loglevel","error","-f","lavfi","-i",f"testsrc2=size={width}x{height}:rate=25","-f","lavfi","-i","sine=frequency=440:sample_rate=48000","-t",str(seconds),"-c:v","libx264","-preset","ultrafast","-pix_fmt","yuv420p","-c:a","aac","-movflags","+faststart",f"/storage/fixtures/{name}")
        local=self.temp/name;docker("cp",f"{self.api_name}:/storage/fixtures/{name}",local)
        return local

    def close(self):
        self.engine.dispose()
        if hasattr(self,"previous_studio_config"):
            from studio.config import save_studio_config
            save_studio_config(self.previous_studio_config)
        for name in reversed(self.created):
            assert_owned(name)
            (self.temp / (name+".log")).write_text(docker("logs",name),encoding="utf-8")
            docker("rm","-f",name)
