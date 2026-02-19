"""FastAPI application entry point."""

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.api.routes.companies import router as companies_router
from src.api.routes.exports import router as exports_router

log = structlog.get_logger(__name__)

app = FastAPI(
    title="Avito Parser — Contractor Registry",
    description="API для работы с базой подрядчиков по инженерным сетям Омска",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(companies_router)
app.include_router(exports_router)


@app.get("/health", tags=["health"])
async def health_check() -> dict:
    return {"status": "ok"}


@app.on_event("startup")
async def startup() -> None:
    log.info("api.startup")


@app.on_event("shutdown")
async def shutdown() -> None:
    log.info("api.shutdown")
