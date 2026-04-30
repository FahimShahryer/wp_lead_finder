from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    database_url: str = "postgresql+asyncpg://app:app@postgres:5432/wp2"
    redis_url: str = "redis://redis:6379/0"

    openai_api_key: str = ""
    serper_api_key: str = ""
    firecrawl_api_key: str = ""
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    # Reddit's bot detection prefers <platform>:<app-id>:<version> (by /u/<username>).
    # Override in .env with your actual reddit username.
    reddit_user_agent: str = "script:wp2-leadfinder:0.1 (by /u/anonymous)"

    # Optional residential proxy for outbound WhatsApp invite-page fetches.
    # Format: http://USER:PASS@host:port. Leave empty to use the api container's
    # direct egress IP. Useful for ban-isolation when running enrichment at scale.
    whatsapp_proxy: str = ""

    # Internal URL of the wa-sidecar Node service that owns Baileys WS sessions.
    # Compose injects this; default targets the in-network service name.
    wa_sidecar_url: str = "http://wa-sidecar:3001"

    # Shared secret used by the sidecar to authenticate inbound message webhooks
    # (POST /internal/inbound). Both api and sidecar containers must read the
    # same value via env. Override in .env for production; the dev default is
    # safe because port 8000 is only exposed for browser traffic on localhost.
    wa_sidecar_secret: str = "dev-internal-secret-change-me"

    @property
    def asyncpg_dsn(self) -> str:
        # asyncpg.connect/create_pool wants the bare 'postgresql://' DSN,
        # not the SQLAlchemy 'postgresql+asyncpg://' form.
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://", 1)


settings = Settings()
