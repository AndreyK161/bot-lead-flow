# Архитектура Lead Flow

## Структура и запуск

В одном репозитории находятся два приложения:

| Каталог | Ответственность |
| --- | --- |
| `apps/bot/backend/` | Telegram-бот, Bitrix/МегаФон webhook, отчёты, обзвон, общий реестр допуска |
| `apps/bot/backend/static/miniapp.html` | Существующий Telegram-интерфейс отчётов бота |
| `apps/planner/backend/` | Сотрудники Planner, дневные планы, задачи, таймеры, согласования, история |
| `apps/planner/frontend/` | Сайт «План дня», React/TypeScript/Vite |
| `apps/planner/static/` | Собранный сайт, обслуживаемый backend |
| `tests/` | Python-тесты и интеграционные проверки обоих приложений |
| `scripts/` | Служебные команды и перенос данных |
| `deploy/` | Шаблоны развёртывания и резервирования |

Сайт открывается в обычном браузере и может открываться через Telegram.
Особенности Telegram входят в этот же frontend; второй копии сайта нет.

Общий entry point — `apps.bot.backend.main:app`. Он обслуживает бот и
монтирует Planner по `/manager/`, включая `/manager/api/` и `/manager/assets/`.
Импорт модулей не запускает миграции: инициализация выполняется в lifespan.
Dockerfile копирует `apps/` и `scripts/`. Текущий Compose-сервис остаётся
`bot-lead-flow`, порт внутри контейнера — 8000. Тома и маршруты Nginx не менялись.

## Текущие зависимости

Это разделение исходников, ещё не разделение процессов или независимые сервисы.

- `apps/bot/backend/main.py` подключает API и инициализацию Planner.
- Бот передаёт подтверждённые распределения в Planner; интерфейс отчётов бота
  использует CRM-адаптер Planner.
- Planner использует настройки, Bitrix-клиент, реестр допуска, телефонию и
  очереди обзвона из `apps/bot/backend/`.
- Эти связи выражены явными Python-импортами. Совместимость через старые пакеты
  `app.manager_planner` и `app` не поддерживается: все потребители в этом
  репозитории переведены на новые пути.

Перед независимым запуском приложений потребуется отдельно выделить общие
контракты и зависимости. Не следует запускать ещё один экземпляр общего
`main` как отдельный Planner: lifespan также запускает фоновые задачи бота.

## Хранилища

Этот этап не переносит данные и не меняет схему БД.

- Основная база бота выбирается через `DATABASE_URL`; SQLite fallback остаётся
  для существующего локального режима и тестов.
- Planner пока использует отдельную SQLite-базу, путь задаётся через
  `MANAGER_PLANNER_DATABASE_PATH`, по умолчанию `data/manager_planner.sqlite3`.
- Существующий том `./data:/app/data` сохраняется. Исходники под `apps/`
  не означают перенос файлов баз в эти каталоги.
- Старые команды импорта и резервирования сохраняют прежние пути данных.

Миграция Planner в PostgreSQL — отдельная задача с переносом истории,
транзакционными проверками и проверенным откатом.

## Локальные проверки

Из корня репозитория:

```sh
python3 -m unittest discover -s tests -q
uvicorn apps.bot.backend.main:app --host 127.0.0.1 --port 8000
```

Frontend:

```sh
cd apps/planner/frontend
npm ci
npm run build
npm run test:api
npm run test:ui
npm run test:navigation
npm run test:profile
npm run test:login
```

Браузерным тестам требуется установленный Chromium Playwright. При использовании
готового браузера его путь можно передать через
`PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH`. Тесты запускают временный локальный HTTP
сервер и вымышленные API-ответы, внешние запросы блокируются.

Vite использует base `/manager/` и выводит сборку в `../static/` относительно
frontend. Готовую сборку нужно включать в изменения вместе с исходниками:
Python Dockerfile не запускает Node/Vite.

## Переход со старых путей

| До | После |
| --- | --- |
| `app/` (кроме Planner) | `apps/bot/backend/` |
| `app/manager_planner/` | `apps/planner/backend/` |
| `manager_miniapp/` | `apps/planner/frontend/` |
| `app/static/manager/` | `apps/planner/static/` |
| `uvicorn app.main:app` | `uvicorn apps.bot.backend.main:app` |

Обновлены основной Dockerfile, шаблон production-update и systemd-шаблон.
Существующие установленные systemd unit-файлы вне репозитория автоматически
не меняются; внешний launcher со старым Python-путём тоже нужно обновить.
Исправления поведения, сделанные до переноса, сохраняются.
