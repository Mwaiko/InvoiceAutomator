from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import setup_logging


# ── Startup / shutdown ────────────────────────────────────────────────────────

# app/main.py
from app.db.base import Base  # Import your Base
from app.db.session import engine # Import your async engine

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    
    # --- ADD THIS PART ---
    # This creates the tables based on your Base models
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    # ----------------------

    import os
    os.makedirs(settings.grn_storage_path, exist_ok=True)
    yield
    
def create_app() -> FastAPI:
    app = FastAPI(
        title="GRN → eTIMS API",
        description="Goods Received Note processing and KRA eTIMS automation",
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # ── CORS ──────────────────────────────────────────────────────────────────
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Exception handlers ────────────────────────────────────────────────────
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)

    # ── Routers ───────────────────────────────────────────────────────────────
    from app.api.auth       import router as auth_router
    from app.api.grns       import router as grns_router
    from app.api.etims      import router as etims_router
    from app.api.orders     import router as orders_router
    from app.api.businesses import router as businesses_router

    app.include_router(auth_router,       prefix="/api/v1")
    app.include_router(grns_router,       prefix="/api/v1")
    app.include_router(etims_router,      prefix="/api/v1")
    app.include_router(orders_router,     prefix="/api/v1")
    app.include_router(businesses_router, prefix="/api/v1")

    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()