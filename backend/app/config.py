from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://dispatcher@localhost:5432/dispatcher"
    redis_url: str = "redis://localhost:6379/0"
    jwt_secret: str = Field(min_length=32)
    languagetool_url: str = "http://languagetool:8081"
    generation_backend: Literal["template", "local_llm"] = "template"
    local_llm_url: str = "http://ollama:11434"
    local_llm_model: str = "qwen2.5:3b"
    stt_enabled: bool = False
    stt_url: str = "http://stt:2700"
    control_url: str = "http://control:8090"
    technical_control_mode: Literal["local", "controller"] = "local"
    tls_ca_file: str | None = None
    sip_ari_url: str = "http://telephony:8088/ari"
    cookie_secure: bool = False
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


settings = Settings()
