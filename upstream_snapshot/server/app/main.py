"""PiAgent 网关入口：uvicorn app.main:app"""
import asyncio
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .llm_proxy import router as llm_router
from .routes_auth import router as auth_router
from .routes_devices import router as devices_router
from .state import hub, timeout_pending
from .ws_device import router as ws_device_router
from .ws_user import router as ws_user_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("piagent")

_sweeper_task: asyncio.Task | None = None


async def _background_sweeper() -> None:
    """周期任务：心跳超时踢线 + 指令超时清理。"""
    while True:
        await asyncio.sleep(5)
        now = time.time()
        for conn in list(hub.devices.values()):
            if now - conn.last_seen > config.HEARTBEAT_TIMEOUT:
                log.warning("设备 %s 心跳超时（%.0fs），断开连接",
                            conn.device_id, now - conn.last_seen)
                try:
                    await conn.ws.close(code=4408)
                except Exception:
                    pass
                if hub.unregister_device(conn):
                    db.device_seen(conn.device_id)
                    await hub.push_device_status(conn.device_id, online=False)
        await timeout_pending(now)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sweeper_task
    db.init_db()
    _sweeper_task = asyncio.create_task(_background_sweeper())
    log.info("PiAgent 网关启动 (LLM proxy: %s, model: %s)",
             "on" if config.LLM_API_KEY else "OFF", config.LLM_MODEL)
    yield
    _sweeper_task.cancel()


app = FastAPI(title="PiAgent Gateway", version="0.1.0", lifespan=lifespan)

# 前端开发期（vite dev server 跨端口）需要 CORS；生产可收紧
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(devices_router)
app.include_router(llm_router)
app.include_router(ws_device_router)
app.include_router(ws_user_router)

_static = __import__("pathlib").Path(__file__).resolve().parent.parent / "static"
app.mount("/static", StaticFiles(directory=_static), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(_static / "index.html")


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "devices_online": list(hub.devices.keys()),
        "llm_proxy": "configured" if (config.LLM_BASE_URL and config.LLM_API_KEY) else "not_configured",
    }
