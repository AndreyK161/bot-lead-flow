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
    telegram_notification_chat_ids: str = ""

    @property
    def notification_chat_ids(self) -> list[str]:
        return list(dict.fromkeys([self.telegram_chat_id] + [x.strip() for x in self.telegram_notification_chat_ids.split(',') if x.strip()]))

    # Секрет, который Telegram присылает в заголовке X-Telegram-Bot-Api-Secret-Token
    # (задаётся при setWebhook) — защищает /telegram/webhook от чужих запросов
    telegram_webhook_secret: str

    # Telegram user_id людей, которым можно управлять лидами через кнопки (через запятую)
    admin_telegram_user_ids: str

    @property
    def admin_telegram_user_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_telegram_user_ids.split(",") if x.strip()}

    # Отдел в Bitrix, из которого предлагать список ответственных при назначении
    sales_department_id: str

    # STATUS_ID лидовой стадии "Мусор"
    junk_status_id: str

    manual_bot_token: str = ""
    manual_webhook_secret: str = ""
    database_path: str = "data/leads.sqlite3"


@lru_cache
def get_settings() -> Settings:
    return Settings()
