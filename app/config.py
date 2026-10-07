import os
from pathlib import Path
from urllib.parse import quote, unquote, urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict


def read_secret(secret_name: str, env_name: str, default: str = "") -> str:
    """Read a Compose/Kubernetes secret first, then its explicit env fallback."""
    secret_dir = Path(os.getenv("RADAR_SECRET_DIR", "/run/secrets"))
    secret_path = secret_dir / secret_name
    try:
        if secret_path.is_file():
            value = secret_path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    return os.getenv(env_name, default)


class Settings(BaseSettings):
    POSTGRES_USER: str = read_secret("postgres_user", "POSTGRES_USER", "radar_app")
    POSTGRES_PASSWORD: str = read_secret("postgres_password", "POSTGRES_PASSWORD")
    POSTGRES_DB: str = "shortmox_radar"
    POSTGRES_HOST: str = "postgres_db"
    POSTGRES_PORT: int = 5432

    REDIS_URL: str = "redis://redis_queue:6379/0"
    OLLAMA_URL: str = "http://ollama:11434"

    RADAR_SESSION_SECRET: str = read_secret(
        "radar_session_secret", "RADAR_SESSION_SECRET"
    )
    RADAR_USERS_JSON: str = read_secret("radar_users_json", "RADAR_USERS_JSON", "[]")
    RADAR_INGEST_API_KEY: str = read_secret(
        "radar_ingest_api_key", "RADAR_INGEST_API_KEY"
    )
    PUBLIC_CHAIN_LOOKUPS_ENABLED: bool = False
    RADAR_SESSION_TTL_SECONDS: int = 28_800
    RADAR_SESSION_COOKIE: str = "radar_session"
    RADAR_COOKIE_SECURE: bool = True
    RADAR_ALLOWED_ORIGINS: str = "http://localhost:8080,http://127.0.0.1:8080"
    RADAR_MAX_WS_PER_USER: int = 5
    RADAR_WS_SESSION_RECHECK_SECONDS: float = 10.0
    SIEM_EXPORT_ENABLED: bool = False
    SIEM_WEBHOOK_URL: str | None = None

    TELEGRAM_BOT_TOKEN: str | None = None
    TELEGRAM_CHAT_ID: str | None = None
    TELEGRAM_API_ID: int = 0
    TELEGRAM_API_HASH: str = read_secret("telegram_api_hash", "TELEGRAM_API_HASH")
    TELEGRAM_COLLECTION_BOT_TOKEN: str = read_secret(
        "telegram_collection_bot_token", "TELEGRAM_COLLECTION_BOT_TOKEN"
    )
    TELEGRAM_SESSION_STRING: str = read_secret(
        "telegram_session_string", "TELEGRAM_SESSION_STRING"
    )
    TELEGRAM_TARGET_CHANNELS: str = ""

    # Collection is explicit opt-in. Collector credentials are server-only.
    COLLECTION_ENABLED: bool = False
    COLLECTION_MAX_RESULTS: int = 25
    COLLECTION_MAX_X_PAGES: int = 5
    COLLECTION_MAX_TELEGRAM_MESSAGES: int = 100
    X_COLLECTION_PROVIDER: str = "scraping"
    X_SCRAPING_CREDENTIAL_SOURCE: str = "env"
    X_PROXY_SOURCE: str = "env"
    X_AUTH_TOKEN: str = read_secret("x_auth_token", "X_AUTH_TOKEN")
    X_CT0: str = read_secret("x_ct0", "X_CT0")
    X_GRAPHQL_BEARER_TOKEN: str = read_secret(
        "x_graphql_bearer_token", "X_GRAPHQL_BEARER_TOKEN"
    )
    X_PROXY_URL: str = read_secret("x_proxy_url", "X_PROXY_URL")
    X_BEARER_TOKEN: str = read_secret("x_bearer_token", "X_BEARER_TOKEN")
    BACKEND_INGEST_URL: str = "http://127.0.0.1:8005/api/v1/ingest"

    MTPROTO_HOST: str | None = None
    MTPROTO_PORT: int = 8888
    MTPROTO_SECRET: str = ""

    TOR_SOCKS_PROXY: str | None = None
    TOR_CONTROL_PORT: int | None = None
    TOR_CONTROL_PASSWORD: str | None = None

    NEO4J_URI: str = "bolt://neo4j_graph:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str = read_secret("neo4j_password", "NEO4J_PASSWORD")

    CLICKHOUSE_URL: str = "http://clickhouse_lake:8123/"

    @property
    def DATABASE_URL(self) -> str:
        user = quote(self.POSTGRES_USER, safe="")
        password = quote(self.POSTGRES_PASSWORD, safe="")
        return (
            f"postgresql://{user}:{password}@{self.POSTGRES_HOST}:"
            f"{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def allowed_origins(self) -> list[str]:
        return [origin.strip() for origin in self.RADAR_ALLOWED_ORIGINS.split(",") if origin.strip()]

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()


def _is_example_secret(value: str | None) -> bool:
    """Recognize the explicit placeholders distributed in .env.example."""
    return bool(value and value.strip().upper().startswith("REPLACE_"))


def _browser_origin_scheme(origin: str) -> str | None:
    """Accept only exact browser origins; plain HTTP is restricted to loopback."""
    try:
        parsed = urlparse(origin)
        # Reading port also rejects malformed values such as https://host:bad.
        parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not origin.startswith(f"{parsed.scheme}://")
        or not parsed.netloc
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return None
    if parsed.scheme == "http" and parsed.hostname not in {
        "localhost", "127.0.0.1", "::1"
    }:
        return None
    return parsed.scheme


def validate_required_settings(config: Settings = settings) -> None:
    missing = []
    if not config.POSTGRES_USER:
        missing.append("POSTGRES_USER")
    if not config.POSTGRES_PASSWORD or _is_example_secret(config.POSTGRES_PASSWORD):
        missing.append("POSTGRES_PASSWORD")
    if len(config.RADAR_SESSION_SECRET) < 32 or _is_example_secret(
        config.RADAR_SESSION_SECRET
    ):
        missing.append("RADAR_SESSION_SECRET (at least 32 characters)")
    if len(config.RADAR_INGEST_API_KEY) < 32 or _is_example_secret(
        config.RADAR_INGEST_API_KEY
    ):
        missing.append("RADAR_INGEST_API_KEY (at least 32 characters)")
    if _is_example_secret(config.NEO4J_PASSWORD):
        missing.append("NEO4J_PASSWORD")
    clickhouse_password = unquote(urlparse(config.CLICKHOUSE_URL).password or "")
    if _is_example_secret(clickhouse_password):
        missing.append("CLICKHOUSE_PASSWORD")
    origins = config.allowed_origins
    schemes = [_browser_origin_scheme(origin) for origin in origins]
    if not origins or None in schemes:
        missing.append("RADAR_ALLOWED_ORIGINS (valid HTTPS origins or loopback HTTP only)")
    elif "https" in schemes:
        if "http" in schemes:
            missing.append("RADAR_ALLOWED_ORIGINS (do not mix HTTPS and HTTP)")
        if not config.RADAR_COOKIE_SECURE:
            missing.append("RADAR_COOKIE_SECURE (true for HTTPS origins)")
    if config.SIEM_EXPORT_ENABLED:
        parsed = urlparse(config.SIEM_WEBHOOK_URL or "")
        if parsed.scheme != "https" or not parsed.netloc:
            missing.append("SIEM_WEBHOOK_URL (HTTPS required when export is enabled)")
    if missing:
        raise RuntimeError(
            "Required secure settings are missing or unsafe: "
            + ", ".join(missing)
        )
