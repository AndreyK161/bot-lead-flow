#!/usr/bin/env python3
"""Host-side Lead Flow watchdog with state-change Telegram alerts."""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

APP_DIR = Path(os.environ.get("LEADFLOW_MONITOR_APP_DIR", "/opt/pravburo/bot-lead-flow"))
STATE_FILE = Path(os.environ.get("LEADFLOW_MONITOR_STATE", "/var/lib/leadflow-monitor/state.json"))
TIMEOUT = float(os.environ.get("LEADFLOW_MONITOR_TIMEOUT", "10"))
BACKUP_MAX_AGE = int(os.environ.get("LEADFLOW_MONITOR_BACKUP_MAX_AGE", "129600"))


def http_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "leadflow-monitor/1"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status}")
        value = json.loads(response.read(262144))
        if value.get("status") != "ok":
            raise RuntimeError(str(value.get("status") or "invalid response"))
        return value


def docker_state(container: str) -> dict:
    raw = subprocess.check_output(
        ["docker", "inspect", container, "--format", "{{json .State}}"],
        text=True, timeout=TIMEOUT,
    )
    state = json.loads(raw)
    if not state.get("Running"):
        raise RuntimeError("container stopped")
    health = (state.get("Health") or {}).get("Status")
    if health and health != "healthy":
        raise RuntimeError(f"container {health}")
    if state.get("OOMKilled"):
        raise RuntimeError("container OOM-killed")
    return state


def newest_backup_age() -> int:
    files = glob.glob(str(APP_DIR / "backups/automatic/bot-lead-flow-*.dump"))
    if not files:
        raise RuntimeError("backup not found")
    age = max(0, int(time.time() - max(Path(path).stat().st_mtime for path in files)))
    if age > BACKUP_MAX_AGE:
        raise RuntimeError(f"backup is {age // 3600}h old")
    return age


def disk_usage() -> int:
    percent = round(shutil.disk_usage(APP_DIR).used / shutil.disk_usage(APP_DIR).total * 100)
    if percent >= 85:
        raise RuntimeError(f"disk usage {percent}%")
    return percent


def dotenv_value(name: str) -> str:
    try:
        for line in (APP_DIR / ".env").read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


def dotenv_token() -> str:
    return dotenv_value("TELEGRAM_BOT_TOKEN")


def telegram_chat_id() -> str:
    explicit = os.environ.get("LEADFLOW_MONITOR_CHAT_ID", "").strip()
    if explicit:
        return explicit
    super_admin = dotenv_value("SUPER_ADMIN_USERS").split(",", 1)[0].strip()
    return super_admin or dotenv_value("MODE_SWITCH_USER_ID").strip()


def telegram_webhook(token: str) -> dict:
    if not token:
        raise RuntimeError("Telegram token unavailable")
    with urllib.request.urlopen(
        f"https://api.telegram.org/bot{token}/getWebhookInfo", timeout=TIMEOUT,
    ) as response:
        result = json.loads(response.read(262144))
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected request")
    info = result["result"]
    if info.get("last_error_date"):
        raise RuntimeError(str(info.get("last_error_message") or "webhook error"))
    pending = int(info.get("pending_update_count") or 0)
    if pending > 50:
        raise RuntimeError(f"Telegram queue has {pending} updates")
    return {"pending_updates": pending}


def run_checks() -> dict[str, str]:
    checks: dict[str, str] = {}
    actions = {
        "bot container": lambda: docker_state("leadflow-bot"),
        "planner container": lambda: docker_state("leadflow-planner"),
        "postgres container": lambda: docker_state("bot-lead-flow-postgres-1"),
        "bot health": lambda: http_json("http://127.0.0.1:8081/health"),
        "planner health": lambda: http_json("http://127.0.0.1:8082/health"),
        "public bot": lambda: http_json("https://tg.prav-buro.ru/health"),
        "public planner": lambda: http_json("https://flow.prav-buro.ru/health"),
        "backup freshness": newest_backup_age,
        "disk": disk_usage,
        "Telegram webhook": lambda: telegram_webhook(dotenv_token()),
    }
    for name, action in actions.items():
        try:
            action()
        except Exception as exc:
            checks[name] = str(exc)[:300]
    return checks


def send_message(token: str, text: str) -> None:
    chat_id = telegram_chat_id()
    if not token or not chat_id:
        return
    payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        if not json.loads(response.read(262144)).get("ok"):
            raise RuntimeError("Telegram notification failed")


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError, TypeError):
        return {"failures": {}}


def save_state(failures: dict[str, str]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps({"failures": failures, "checked_at": int(time.time())}, ensure_ascii=False))
    os.replace(temporary, STATE_FILE)


def main() -> int:
    failures = run_checks()
    previous = load_state().get("failures") or {}
    token = dotenv_token()
    if failures != previous:
        if failures:
            lines = ["🚨 Lead Flow: проблема мониторинга"]
            lines.extend(f"• {name}: {error}" for name, error in sorted(failures.items()))
            send_message(token, "\n".join(lines))
        elif previous:
            send_message(token, "✅ Lead Flow восстановлен: все проверки проходят")
    save_state(failures)
    if failures:
        for name, error in sorted(failures.items()):
            print(f"FAIL {name}: {error}")
        return 1
    print("OK Lead Flow monitoring checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
