"""Scoped Planner identities imported without granting Lead Flow admin rights."""
from __future__ import annotations

from app import store


LEGACY_IDENTITIES = (
    {
        "telegram_id": 1297686797, "bitrix_user_id": None, "display_name": "Владелец",
        "role": "owner", "active": True, "login_enabled_on_insert": True,
        "legacy_created_at": "2026-09-21T10:25:11.522Z",
    },
    {
        "telegram_id": 1073597301, "bitrix_user_id": None, "display_name": "Руководитель",
        "role": "supervisor", "active": True, "login_enabled_on_insert": False,
        "legacy_created_at": "2026-10-12T08:39:40.034Z",
    },
    {
        "telegram_id": 399005637, "bitrix_user_id": "460", "display_name": "Вячеслав Шандра",
        "role": "manager", "active": True, "login_enabled_on_insert": False,
        "legacy_created_at": "2026-10-13T13:32:55.805Z",
    },
    {
        "telegram_id": 7874883320, "bitrix_user_id": "50", "display_name": "Евгений Гринёв",
        "role": "manager", "active": True, "login_enabled_on_insert": False,
        "legacy_created_at": "2026-09-21T11:34:42.365Z",
    },
    {
        "telegram_id": 7939114214, "bitrix_user_id": "110", "display_name": "Яна Азизова",
        "role": "manager", "active": True, "login_enabled_on_insert": False,
        "legacy_created_at": "2026-09-21T10:30:42.476Z",
    },
    {
        "telegram_id": 8306778724, "bitrix_user_id": "460", "display_name": "Вячеслав Шандра",
        "role": "manager", "active": True, "login_enabled_on_insert": False,
        "legacy_created_at": "2026-09-21T10:25:11.523Z",
    },
)


def seed_legacy_identities() -> None:
    for identity in LEGACY_IDENTITIES:
        store.upsert_planner_access(source="legacy-planner", **identity)


def authorized_identity(telegram_id: int) -> dict | None:
    identity = store.planner_access(telegram_id)
    if not identity or not identity["active"] or not identity["login_enabled"]:
        return None
    return identity


def enabled_user_ids(*, role: str | None = None) -> set[int]:
    """Return only explicitly opened accounts, optionally scoped by role."""
    return {
        int(identity["telegram_id"])
        for identity in store.list_planner_access()
        if identity["active"]
        and identity["login_enabled"]
        and (role is None or identity["role"] == role)
    }
