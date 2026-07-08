from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from contextlib import asynccontextmanager

from app.database import init_db
from app.routers import admin, api, slack, slack_dispatch, sso
from app.services.home import tiles_for
from app.services.scheduler import create_scheduler
from app.services.sso import sso_identity


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

templates = Jinja2Templates(directory="app/templates")


@app.get("/")
async def root(request: Request):
    identity = sso_identity(request)
    if identity is None:
        return RedirectResponse("/sso/authorize?app=legion&return_to=%2F", status_code=303)
    return templates.TemplateResponse(
        "home.html",
        {"request": request, "name": identity.get("name", ""), "tiles": tiles_for(identity)},
    )
