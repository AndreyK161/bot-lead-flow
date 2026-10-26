"""Shared rules for deciding who is an assignable sales manager."""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.config import get_settings


DEFAULT_EXCLUDED_USER_IDS = {"26"}
DEFAULT_EXCLUDED_USER_NAMES = {"анастасия коравчук"}


def user_name(user: dict[str, Any]) -> str:
    return " ".join(
        part
        for part in (
            str(user.get("NAME") or "").strip(),
            str(user.get("LAST_NAME") or "").strip(),
        )
        if part
    ) or str(user.get("EMAIL") or user.get("ID") or "")


def assignable_sales_users(
    users: Iterable[dict[str, Any]], settings: Any | None = None,
) -> list[dict[str, Any]]:
    settings = settings or get_settings()
    excluded_ids = {
        str(value)
        for value in getattr(
            settings, "excluded_sales_user_id_set", DEFAULT_EXCLUDED_USER_IDS,
        )
    }
    excluded_names = {
        str(value).strip().casefold()
        for value in getattr(
            settings, "excluded_sales_user_name_set", DEFAULT_EXCLUDED_USER_NAMES,
        )
    }
    return [
        user
        for user in users
        if str(user.get("ID") or "") not in excluded_ids
        and user_name(user).strip().casefold() not in excluded_names
    ]
