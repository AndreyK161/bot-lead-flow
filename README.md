# bot-lead-flow

Небольшой сервис: принимает событие о новом лиде из Bitrix24 и шлёт красивое
уведомление в Telegram. Только чтение — в Bitrix ничего не пишет.

## Как это работает

1. В Bitrix24 создаётся **исходящий вебхук** на событие `ONCRMLEADADD`.
   При создании лида Bitrix сам делает POST на `/bitrix/webhook` этого сервиса
   (form-urlencoded), передавая `event`, `data[FIELDS][ID]` и
   `auth[application_token]`.
2. Сервис сверяет `application_token` с `BITRIX_APPLICATION_TOKEN` из `.env` —
   если не совпадает, отвечает 403.
3. По `ID` лида сервис дозапрашивает полные данные через **входящий вебхук**
   методом `crm.lead.get`, резолвит источник (`crm.status.list`) и
   ответственного (`user.get`).
4. Собирает HTML-сообщение и отправляет его в Telegram через
   `sendMessage` (`parse_mode=HTML`).

## Установка

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Заполните `.env` (см. разделы ниже, как получить каждое значение).

Запуск локально:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Эндпоинт: `POST /bitrix/webhook`. Для продакшена сервису нужен публичный
HTTPS-адрес (Bitrix24 стучится извне) — например, через reverse proxy /
хостинг с доменом, или туннель (ngrok/Cloudflare Tunnel) для теста.

## Настройка Bitrix24

### 1. Входящий вебхук (для чтения данных лида)

1. Откройте портал → **Приложения → Разработчикам → Другое → Входящий вебхук**.
2. Добавьте вебхук, выберите права (scope): **crm**, **user**.
3. Скопируйте полученный URL вида
   `https://your-portal.bitrix24.ru/rest/1/xxxxxxxxxxxxxxxx/`
   в `BITRIX_WEBHOOK_URL`.

### 2. Исходящий вебхук (уведомление о новом лиде)

1. Там же: **Приложения → Разработчикам → Другое → Исходящий вебхук**.
2. Событие: `OnCrmLeadAdd`.
3. Обработчик (URL): публичный адрес вашего сервиса, например
   `https://your-domain.example.com/bitrix/webhook`.
4. Bitrix сгенерирует токен приложения (application token) — скопируйте его в
   `BITRIX_APPLICATION_TOKEN`. Именно это значение сервис сверяет в каждом
   входящем запросе, чтобы отбрасывать чужие POST-запросы.
5. Сохраните и создайте тестовый лид — в логах сервиса и в Telegram должно
   появиться уведомление.

## Настройка Telegram-бота

1. Откройте [@BotFather](https://t.me/BotFather), отправьте `/newbot`,
   следуйте инструкциям, получите токен вида `123456789:AA...` —
   это `TELEGRAM_BOT_TOKEN`.
2. Добавьте бота в нужный чат/группу (или используйте личный чат с ботом).
3. Узнайте `chat_id`:
   - для группы: добавьте бота, отправьте любое сообщение в группу, затем
     откройте `https://api.telegram.org/bot<TOKEN>/getUpdates` и найдите
     `"chat":{"id": -100...}` в ответе;
   - либо воспользуйтесь ботами вида `@getmyid_bot` / `@userinfobot`.
4. Значение (обычно отрицательное число для групп) впишите в
   `TELEGRAM_CHAT_ID`.

## Структура проекта

```
app/
  config.py          # чтение .env через pydantic-settings
  bitrix_client.py    # запросы к Bitrix REST (crm.lead.get, crm.status.list, user.get)
  formatter.py         # парсинг полей лида + сборка HTML-сообщения
  telegram_client.py   # отправка sendMessage в Telegram
  main.py              # FastAPI-приложение, эндпоинт /bitrix/webhook
requirements.txt
.env.example
```

## Обработка edge-кейсов

- Пустые/отсутствующие `PHONE`/`EMAIL` (multifield) — строки просто
  пропускаются в сообщении.
- Отсутствующий `SOURCE_ID` или `ASSIGNED_BY_ID` — соответствующие строки не
  добавляются, доп. запросы к Bitrix не делаются.
- Все пользовательские текстовые поля (имя, `SOURCE_DESCRIPTION`, комментарий)
  экранируются через `html.escape` перед вставкой в HTML-сообщение — чтобы
  случайные `<`, `>`, `&` в данных лида не ломали разметку Telegram.
- Ошибки Bitrix API (`{"error": ...}` в теле ответа 200 OK) и ошибки Telegram
  API логируются и возвращаются как 502, чтобы это было видно в логах.
