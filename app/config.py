from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Входящий вебхук (Bitrix REST, для чтения crm.lead.get, crm.status.list, user.get)
    bitrix_webhook_url: str

    # Токен исходящего вебхука ONCRMLEADADD — сверяется, чтобы принимать запросы только от Битрикса
    bitrix_application_token: str

    telegram_bot_token: str
    telegram_chat_id: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
