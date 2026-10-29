import ast
import json
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


def lead_notification_recipient_ids(settings) -> set[int]:
    """Privileged users who should receive newly created lead/deal cards."""
    return set(settings.director_user_id_set) - set(
        getattr(settings, "lead_notification_excluded_user_id_set", set())
    )


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

    # Пользователи сохраняют руководительские/админские права, но не получают
    # общий поток карточек. Персональное уведомление о назначении сохраняется.
    lead_notification_excluded_users: str = ""

    @property
    def lead_notification_excluded_user_id_set(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.lead_notification_excluded_users.split(",")
            if value.strip()
        }

    # Telegram user_id админов бота — только привязка продажник↔Telegram (/link, /links), к лидам доступа нет
    admin_users: str = ""

    @property
    def admin_user_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.admin_users.split(",") if x.strip()}

    # Закрытые разделы Mini App с персональными данными звонков.
    super_admin_users: str = "1297686797"

    @property
    def super_admin_user_id_set(self) -> set[int]:
        return {int(x.strip()) for x in self.super_admin_users.split(",") if x.strip()}

    @property
    def mini_app_user_id_set(self) -> set[int]:
        """Directors and technical admins may open analytical reports."""
        return self.director_user_id_set | self.admin_user_id_set | self.super_admin_user_id_set

    # Отдел в Bitrix, из которого предлагать список ответственных при назначении
    sales_department_id: str
    # Руководители и другие сотрудники отдела, которых нельзя назначать ответственными.
    excluded_sales_user_ids: str = "26"
    excluded_sales_user_names: str = "Анастасия Коравчук"

    @property
    def excluded_sales_user_id_set(self) -> set[str]:
        return {value.strip() for value in self.excluded_sales_user_ids.split(",") if value.strip()}

    @property
    def excluded_sales_user_name_set(self) -> set[str]:
        return {
            value.strip().casefold()
            for value in self.excluded_sales_user_names.split(",")
            if value.strip()
        }

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
    manual_recovery_max_attempts: int = 5
    manual_recovery_base_delay_seconds: int = 30
    outcome_sync_time: str = "03:00"
    accompaniment_deal_category_id: str = "2"
    contract_source_url_field: str = "UF_CRM_1775217002"
    deal_refusal_reason_field: str = "UF_CRM_REFUSAL_REASON_OP"
    deal_contract_date_field: str = "UF_CRM_CONTRACT_DATE_OP"
    mini_app_url: str = "https://lead.prav-buro.ru/miniapp"
    manager_app_url: str = "https://tg.prav-buro.ru/workday/"
    manager_planner_database_path: str = "data/manager_planner.sqlite3"
    # Optional bearer-token validation endpoint for the standalone web UI.
    # Telegram Mini App authorization remains the default transport.
    leadflow_web_auth_url: str = ""
    leadflow_web_auth_timeout_seconds: float = 5.0
    mode_switch_user_id: int = 1297686797
    # Temporary read-only bridge used while the legacy distribution bot is
    # still online. An empty path keeps the bridge fully disabled.
    legacy_distribution_queue_path: str = ""
    legacy_distribution_confirmer_ids: str = ""
    legacy_distribution_poll_seconds: float = 30
    distribution_confirmer_ids: str = "1297686797"
    leadflow_presence_url: str = "https://lead.prav-buro.ru/api/v1/users/internal-presence"
    leadflow_presence_token: str = ""
    megafon_crm_token: str = ""
    megafon_bitrix_user_map: str = "{}"
    # CRM API used by the manager dialer.  Secrets stay in the environment;
    # MEGAFON_DIALER_USERS maps a Bitrix user id to user/group/clid, for example
    # {"42":{"user":"701","clid":"7495..."}}.
    megafon_vats_api_url: str = ""
    megafon_vats_api_key: str = ""
    megafon_vats_auth_mode: str = "header"
    megafon_vats_auth_header: str = "X-API-KEY"
    megafon_dialer_users: str = "{}"
    bitrix_automation_user_ids: str = "24"

    @property
    def legacy_distribution_confirmer_id_set(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.legacy_distribution_confirmer_ids.split(",")
            if value.strip()
        }

    @property
    def distribution_confirmer_id_set(self) -> set[int]:
        return {
            int(value.strip())
            for value in self.distribution_confirmer_ids.split(",")
            if value.strip()
        }

    @property
    def bitrix_automation_user_id_set(self) -> set[str]:
        return {
            value.strip() for value in self.bitrix_automation_user_ids.split(",")
            if value.strip()
        }

    @property
    def megafon_bitrix_users(self) -> dict[str, str]:
        raw = self.megafon_bitrix_user_map or "{}"
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            # Existing telephony service may provide a Python-style numeric mapping
            # such as {101: 42}; literal_eval keeps this fallback data-only.
            value = ast.literal_eval(raw)
        if not isinstance(value, dict):
            raise ValueError("MEGAFON_BITRIX_USER_MAP must be an object")
        return {str(extension): str(user_id) for extension, user_id in value.items()}

    @property
    def megafon_dialer_user_map(self) -> dict[str, dict[str, str]]:
        try:
            value = json.loads(self.megafon_dialer_users or "{}")
        except json.JSONDecodeError as exc:
            raise ValueError("MEGAFON_DIALER_USERS must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("MEGAFON_DIALER_USERS must be a JSON object")
        result: dict[str, dict[str, str]] = {}
        for bitrix_id, raw in value.items():
            if not isinstance(raw, dict):
                continue
            config = {
                key: str(raw.get(key) or "").strip()
                for key in ("user", "group", "clid")
            }
            if config["user"] or config["group"]:
                result[str(bitrix_id)] = config
        # Existing extension -> Bitrix bindings are a safe default for VATS
        # installations where the extension is also the CRM API user value.
        for extension, bitrix_id in self.megafon_bitrix_users.items():
            result.setdefault(str(bitrix_id), {"user": str(extension), "group": "", "clid": ""})
        return result


@lru_cache
def get_settings() -> Settings:
    return Settings()
