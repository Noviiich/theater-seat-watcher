# Структура проекта

Сейчас уже существуют базовый Python-пакет, настройки, чистые модели домена,
SQLite adapter, Alembic-миграции, сценарий discovery → checkout → outbox и
устойчивый lifecycle фоновых задач с runtime-диагностикой. Добавлены production
CLI, контейнер и операции SQLite backup/restore.
Ниже — **целевая** структура, которая создаётся постепенно по roadmap; пустые
модули заранее не нужны.

```text
theater_tickets/
├── AGENTS.md
├── README.md                       # Запуск и ссылки на docs
├── pyproject.toml
├── uv.lock
├── .python-version
├── .env.example                    # Только имена/безопасные примеры
├── .gitignore
├── alembic.ini
├── migrations/
├── docs/
│   ├── README.md
│   ├── requirements.md
│   ├── quicktickets-api.md
│   ├── architecture.md
│   ├── seat-selection.md
│   ├── project-structure.md
│   ├── operations.md
│   ├── live-acceptance.md
│   └── roadmap.md
├── config/
│   └── halls/
│       └── <hall-id>.yaml           # Проверенный профиль, без ПДн
├── src/theater_tickets/
│   ├── __init__.py
│   ├── __main__.py                 # Запуск через python -m theater_tickets
│   ├── bootstrap.py                # Сборка зависимостей и lifecycle задач
│   ├── production.py               # Concrete dry-run composition
│   ├── settings.py
│   ├── domain/
│   │   ├── models.py               # Session, Seat, Group, Money, Preferences
│   │   ├── events.py
│   │   ├── errors.py
│   │   ├── policies.py             # Фильтры, лимиты, приоритеты
│   │   └── seating/
│   │       ├── topology.py
│   │       ├── candidates.py
│   │       └── scoring.py
│   ├── application/
│   │   ├── ports.py                # Provider, repositories, notifier, clock
│   │   ├── subscriptions.py
│   │   ├── discovery.py
│   │   ├── planning.py             # Каждый подходящий сеанс в пределах лимитов
│   │   ├── checkout.py             # Типизированные request/result/stages и порты
│   │   ├── booking.py              # Повторная проверка и provider-neutral решение
│   │   ├── renewals.py             # Сроки циклов, ожидания и повтор после 1200 секунд
│   │   ├── outbox.py                # Сообщения оплаты, batch summary и порты доставки
│   │   ├── runtime.py              # Контракты lifecycle/lock/диагностики
│   │   ├── status.py               # Owner-scoped модель расширенного /status
│   │   └── reconciliation.py       # Startup recovery без повтора неизвестного POST
│   ├── adapters/
│   │   ├── logging.py              # JSON-события с очисткой секретов
│   │   ├── quicktickets/
│   │   │   ├── client.py           # HTTP GET, headers, timeouts, rate limiting
│   │   │   ├── catalogue.py        # Парсинг афиши, JSON-LD и iframe context
│   │   │   ├── inventory.py        # Схема + занятость + capabilities
│   │   │   ├── provider.py         # Catalogue + fresh session/inventory reads
│   │   │   ├── dto.py              # Внешние форматы и validation
│   │   │   ├── checkout.py         # Валидация и оркестрация этапов оформления
│   │   │   ├── browser.py          # Изолированный Playwright checkout по HAR-контракту
│   │   │   ├── reconciliation.py   # found/not-found/unknown/unsupported
│   │   │   └── browser.py          # Опционально по результату исследования
│   │   ├── telegram/
│   │   │   ├── routers/
│   │   │   ├── middleware.py       # Allowlist, private chat, update dedup
│   │   │   ├── keyboards.py
│   │   │   └── notifier.py
│   │   └── persistence/
│   │       ├── booking.py          # Context, dry-run, failure/recovery transitions
│   │       ├── checkout.py         # Короткие транзакции стадий intent
│   │       ├── outbox.py            # Order + outbox атомарно, claim/retry/supersede
│   │       ├── reconciliation.py   # Recovery states и request snapshot
│   │       ├── renewals.py         # Атомарный claim, next_run_at и release allocation
│   │       ├── runtime.py          # Lease-lock, worker state и status queries
│   │       ├── database.py
│   │       ├── models.py           # ORM отдельно от domain.models
│   │       ├── repositories.py
│   │       └── unit_of_work.py
│   ├── workers/
│       ├── polling.py
│       ├── runtime.py              # Независимые loops, backoff и graceful shutdown
│       ├── booking.py              # Live/dry-run/resume и one-shot workflow
│       ├── renewals.py             # Сохранённые next_run_at, без повторов пропущенных тиков
│       ├── reconciliation.py
│       └── outbox.py
│   └── operations/
│       └── database.py             # Migrate/health/smoke/SQLite backup/restore
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── fakes/
│   └── fixtures/quicktickets/      # Минимальные очищенные HTML/JSON
├── scripts/
│   └── inspect_quicktickets.py    # Явная диагностика чтения, без live POST
├── deploy/
│   ├── Dockerfile
│   ├── compose.yaml
│   ├── deploy-release.sh
│   ├── prepare-image-upload.sh
│   └── bootstrap-ubuntu-debian.sh
└── .github/workflows/ci.yml
```

Направление зависимостей: `domain` ← `application` ← `adapters/workers`.
`bootstrap` знает конкретные адаптеры и передаёт их в сценарии. Код алгоритма
работает с переданными данными и не читает YAML, HTTP или SQLite самостоятельно.

Данные эксплуатации (`.env`, SQLite/WAL, cookie jar, storage_state, данные профиля
покупателя, приватные трассировки и backups) находятся вне отслеживаемого дерева
либо в игнорируемом каталоге с ограниченными правами. Профили предпочтений зала
можно версионировать; платёжные ссылки и пользовательские заказы — нельзя.

`tests/unit` проверяет правила и алгоритм без I/O. `contract` проверяет парсеры
и адаптеры на фиксированных очищенных данных. `integration` запускает SQLite,
fake provider и fake Telegram, проверяя транзакции, outbox и восстановление.
Live-проверки не входят в обычный `pytest` и не запускаются в CI.

Модели persistence включают Candidate → RenewalCycle → CheckoutIntent → Order,
а dry-run сохраняет отдельный DryRunReport без intent/allocation/POST.
История циклов хранится отдельно от текущего состояния отслеживания сеанса.
Планировщик renewals пробуждает задачи по времени начала удержания и подтверждённому
исходу заказа; outbox создаёт новое сообщение на каждый успешный цикл. Fake clock
позволяет проверить границы 1199/1200/1201 секунд и перезапуск без реального ожидания.
