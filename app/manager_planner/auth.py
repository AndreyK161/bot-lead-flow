"""Telegram HMAC validation. Never accept client-supplied identity or roles."""
import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
    pass


class InvalidTelegramLogin(ValueError):
    pass


def validate_init_data(raw: str, token: str, max_age: int = 43200, now: int | None = None) -> int:
    try:
        if not raw or len(raw) > 16384:
            raise ValueError()
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True, max_num_fields=32)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError()
        signature = fields.pop("hash")
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError()
        check = "\n".join(f"{key}={value}" for key, value in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        timestamp = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if timestamp > current + 30 or current - timestamp > max_age:
            raise ValueError()
        user_id = json.loads(fields["user"])["id"]
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise ValueError()
        return user_id
    except (ValueError, KeyError, TypeError, OverflowError):
        raise InvalidInitData("Откройте приложение заново из Telegram.") from None


def validate_login_widget(payload: dict, token: str, max_age: int = 600, now: int | None = None) -> int:
    """Validate data returned by the official Telegram Login Widget."""
    try:
        if not isinstance(payload, dict) or len(payload) > 12:
            raise ValueError()
        fields = dict(payload)
        signature = str(fields.pop("hash"))
        if not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError()
        allowed = {"id", "first_name", "last_name", "username", "photo_url", "auth_date"}
        if not fields.keys() <= allowed:
            raise ValueError()
        check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
        secret = hashlib.sha256(token.encode()).digest()
        expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        timestamp = int(fields["auth_date"])
        current = int(time.time()) if now is None else now
        if timestamp > current + 30 or current - timestamp > max_age:
            raise ValueError()
        user_id = fields["id"]
        if type(user_id) is not int or not 0 < user_id < 2**63:
            raise ValueError()
        return user_id
    except (ValueError, KeyError, TypeError, OverflowError):
        raise InvalidTelegramLogin("Не удалось подтвердить вход через Telegram.") from None


def _session_key(token: str) -> bytes:
    return hmac.new(token.encode(), b"LeadFlowPlannerWebSession", hashlib.sha256).digest()


def issue_web_session(telegram_id: int, token: str, ttl: int = 43200, now: int | None = None) -> str:
    issued_at = int(time.time()) if now is None else now
    raw = json.dumps(
        {"telegram_id": telegram_id, "iat": issued_at, "exp": issued_at + ttl},
        sort_keys=True, separators=(",", ":"),
    ).encode()
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    signature = hmac.new(_session_key(token), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{signature}"


def validate_web_session(raw: str, token: str, now: int | None = None) -> int:
    try:
        if not raw or len(raw) > 1024:
            raise ValueError()
        body, signature = raw.split(".", 1)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", body) or not re.fullmatch(r"[0-9a-f]{64}", signature):
            raise ValueError()
        expected = hmac.new(_session_key(token), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        decoded = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
        payload = json.loads(decoded)
        current = int(time.time()) if now is None else now
        telegram_id = payload["telegram_id"]
        if type(telegram_id) is not int or not 0 < telegram_id < 2**63:
            raise ValueError()
        if int(payload["iat"]) > current + 30 or int(payload["exp"]) < current:
            raise ValueError()
        return telegram_id
    except (ValueError, KeyError, TypeError, OverflowError, binascii.Error):
        raise InvalidTelegramLogin("Сессия Telegram истекла. Войдите снова.") from None
