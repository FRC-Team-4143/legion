from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

from app.database import init_db
from app.routers import admin, api, slack, slack_dispatch, sso
from app.services.scheduler import create_scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    scheduler = create_scheduler()
    scheduler.start()
    app.state.scheduler = scheduler
    yield
    scheduler.shutdown()


app = FastAPI(title="Legion", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")

app.include_router(admin.router)
app.include_router(api.router)
app.include_router(sso.router)
app.include_router(slack.router)
app.include_router(slack_dispatch.router)


@app.get("/")
async def root():
    return RedirectResponse("/admin")
