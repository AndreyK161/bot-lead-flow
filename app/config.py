from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration, loaded from environment variables / .env."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # Входящий вебхук (Bitrix REST, для crm.lead.get/update, crm.status.list, user.get)
    bitrix_webhook_url: str

    # Токен исходящего вебхука ONCRMLEADADD — сверяется, чтобы принимать запросы только от Битрикса
    bitrix_application_token: str

    @property
    def bitrix_application_token_set(self) -> set[str]:
        return {value.strip() for value in self.bitrix_application_token.split(",") if value.strip()}

    telegram_bot_token: str
    telegram_chat_id: str
    telegram_notification_chat_ids: str = ""

    @property
    def notification_chat_ids(self) -> list[str]:
        return list(dict.fromkeys([self.telegram_chat_id] + [x.strip() for x in self.telegram_notification_chat_ids.split(',') if x.strip()]))

    # Секрет, который Telegram присылает в заголовке X-Telegram-Bot-Api-Secret-Token
    # (задаётся при setWebhook) — защищает /telegram/webhook от чужих запросов
    telegram_webhook_secret: str

    # Telegram user_id руководителей — управляют лидами через кнопки, плюс доступ к ручному боту (через запятую)
    director_users: str

    @property
    def director_user_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.director_users.split(",") if x.strip()}

    # Telegram user_id админов бота — только привязка продажник↔Telegram (/link, /links), к лидам доступа нет
    admin_users: str = ""

    @property
    def admin_user_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_users.split(",") if x.strip()}

    @property
    def mini_app_user_id_set(self) -> set[int]:
        """Directors and technical admins may open analytical reports."""
        return self.director_user_id_set | self.admin_user_id_set

    # Отдел в Bitrix, из которого предлагать список ответственных при назначении
    sales_department_id: str

    # PostgreSQL используется в production; SQLite остаётся fallback для тестов/локальной разработки.
    database_url: str = ""
    database_path: str = "data/leads.sqlite3"
    track_deal_category_id: str = "0"
    deal_poll_interval_seconds: int = 15
    lead_unprocessed_status_id: str = "NEW"
    deal_unprocessed_stage_id: str = "NEW"
    daily_stats_time: str = "19:00"
    daily_stats_timezone: str = "Europe/Moscow"
    reconcile_interval_seconds: int = 300
    reconcile_overlap_seconds: int = 300
    reconcile_initial_lookback_hours: int = 24
    outcome_sync_time: str = "03:00"
    accompaniment_deal_category_id: str = "2"
    contract_source_url_field: str = "UF_CRM_1775217002"
    mini_app_url: str = "https://lead.prav-buro.ru/miniapp"


@lru_cache
def get_settings() -> Settings:
    return Settings()
