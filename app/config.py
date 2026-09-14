from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Входящий вебхук (Bitrix REST, для crm.lead.get/update, crm.status.list, user.get)
    bitrix_webhook_url: str

    # Токен исходящего вебхука ONCRMLEADADD — сверяется, чтобы принимать запросы только от Битрикса
    bitrix_application_token: str

    telegram_bot_token: str
    telegram_chat_id: str

    # Секрет, который Telegram присылает в заголовке X-Telegram-Bot-Api-Secret-Token
    # (задаётся при setWebhook) — защищает /telegram/webhook от чужих запросов
    telegram_webhook_secret: str

    # Telegram user_id единственного человека, которому можно управлять лидами через кнопки
    admin_telegram_user_id: int

    # Отдел в Bitrix, из которого предлагать список ответственных при назначении
    sales_department_id: str

    # STATUS_ID лидовой стадии "Мусор"
    junk_status_id: str


@lru_cache
def get_settings() -> Settings:
    return Settings()
