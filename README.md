# theater_tickets

Telegram-бот отслеживает новые сеансы Орловского театра «Свободное пространство»,
подбирает соседние места и отправляет подтверждённую ссылку на оплату.
Полное описание проекта находится в [docs/README.md](docs/README.md).

Реализованы read-only QuickTickets-клиент, обнаружение сеансов, подбор мест,
durable planning, строгий checkout-handoff и сохраняемый планировщик повторных
циклов, persistent outbox с Telegram-кнопками оплаты, полный прикладной сценарий
и устойчивый runtime с независимыми polling-задачами, backoff, single-instance
lock и диагностикой. Добавлены production CLI, контейнер/Compose, автоматические
миграции и SQLite backup/restore. Режим по умолчанию — `dry_run`, в котором
write transport отсутствует. Ограниченный live-запуск требует явных денежных
лимитов и подтверждения новой подписки администратором.

## Требования

- Python 3.12 или новее
- [uv](https://docs.astral.sh/uv/)

## Установка и проверка

```bash
uv sync --all-groups
uv run python -m theater_tickets diagnose
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Для запуска скопируйте `.env.example` в `.env`, задайте значения вне Git и
следуйте [эксплуатационной инструкции](docs/operations.md). Python сам не загружает
`.env`; Compose передаёт его процессу. Диагностика CLI не показывает секреты.

Для контролируемого `BOOKING_MODE=live` следуйте
[протоколу приёмки](docs/live-acceptance.md); успешная реальная приёмка ещё не
зафиксирована. Checkout и startup recovery отклоняют
неполный или неподтверждённый handoff, но оплату всегда совершает пользователь
на стороне продавца.

Покупатель заполняет ФИО, email и телефон в личном чате командой `/profile set`.
Эти данные сохраняются в SQLite-профиле владельца и не попадают в Git или логи.
Само наличие профиля не включает checkout: администратор отдельно подтверждает
новую live-подписку кнопкой в Telegram.
