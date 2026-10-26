"""Telegram HMAC validation. Never accept client-supplied identity or roles."""
import hashlib
import hmac
import json
import re
import time
from urllib.parse import parse_qsl


class InvalidInitData(ValueError):
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
