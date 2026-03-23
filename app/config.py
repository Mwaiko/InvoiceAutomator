from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",              # used locally
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App ───────────────────────────────────────────────────────────────────
    app_env: str = "development"
    app_secret_key: str
    app_debug: bool = False

    # ── Database ──────────────────────────────────────────────────────────────
    database_url: str

    # ── Storage ───────────────────────────────────────────────────────────────
    storage_backend: str = "local"
    storage_local_root: str = "./storage"

    # ── JWT ───────────────────────────────────────────────────────────────────
    jwt_algorithm: str = "HS256"
    jwt_access_token_expire_minutes: int = 60
    jwt_refresh_token_expire_days: int = 30

    # ── eTIMS ─────────────────────────────────────────────────────────────────
    etims_user_id: str = ""
    etims_password: str = ""
    etims_base_url: str = "https://etims.kra.go.ke"
    etims_headless: bool = True

    # ── CORS ──────────────────────────────────────────────────────────────────
    cors_origins: list[str] = ["http://localhost:3000"]

    # ── Derived helpers ───────────────────────────────────────────────────────
    @property
    def is_production(self) -> bool:
        return self.app_env == "production"

    @property
    def grn_storage_path(self) -> str:
        return f"{self.storage_local_root}/grns"

    @property
    def invoice_storage_path(self) -> str:
        return f"{self.storage_local_root}/invoices"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()