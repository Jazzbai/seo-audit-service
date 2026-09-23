"""Standalone application entry point."""
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text

from app.config import settings
from app.db import engine, init_db
from app.version import VERSION


@asynccontextmanager
async def lifespan(app):
    if settings.DATABASE_URL.startswith("sqlite"):
        init_db()
    yield


app = FastAPI(title="ForgeSEO Platform", version=VERSION, lifespan=lifespan)


@app.get("/health")
def health():
    with engine.connect() as db:
        db.execute(text("SELECT 1"))
    return {"status": "ok", "service": "forgeseo-platform"}


from app.auth import router as auth_router
from app.api import router as api_router
from app.oauth import router as oauth_router

app.include_router(auth_router)
app.include_router(api_router)
app.include_router(oauth_router)

from app.webhooks import router as webhook_router
app.include_router(webhook_router)
