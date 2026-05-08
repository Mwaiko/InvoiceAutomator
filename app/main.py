from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.exceptions import AppError, app_error_handler, unhandled_error_handler
from app.core.logging import setup_logging


# ── Startup / shutdown ────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()

    # Ensure storage directories exist
    import os
    os.makedirs(settings.grn_storage_path, exist_ok=True)
    os.makedirs(settings.invoice_storage_path, exist_ok=True)

    yield  # application runs here

    # Teardown (close engine, etc.) — add as needed


# ── App factory ───────────────────────────────────────────────────────────────

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
    # In app/main.py
    from app.api.account_transactions import txn_router, finance_router
    from app.api.accounts   import router as accounts_router
    from app.api.expense_categories import router as expense_categories_router 
    from app.api.expenses   import router as expenses_router
    app.include_router(auth_router,       prefix="/api/v1")
    app.include_router(grns_router,       prefix="/api/v1")
    app.include_router(etims_router,      prefix="/api/v1")
    app.include_router(orders_router,     prefix="/api/v1")
    app.include_router(businesses_router, prefix="/api/v1")
    # Inside create_app()
    app.include_router(txn_router, prefix="/api/v1")
    app.include_router(finance_router, prefix="/api/v1")
    app.include_router(accounts_router,   prefix="/api/v1")
    app.include_router(expense_categories_router,prefix="/api/v1")
    app.include_router(expenses_router,   prefix="/api/v1")
    # ── Health check ──────────────────────────────────────────────────────────
    @app.get("/health", tags=["system"])
    async def health():
        return {"status": "ok", "env": settings.app_env}

    return app


app = create_app()