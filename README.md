# theater_tickets

Telegram-бот отслеживает новые сеансы Орловского театра «Свободное пространство»,
подбирает соседние места и в дальнейшем будет отправлять ссылку на оплату.
Полное описание проекта находится в [docs/README.md](docs/README.md).

Реализованы read-only QuickTickets-клиент, обнаружение сеансов, подбор мест,
durable planning, строгий checkout-handoff и сохраняемый планировщик повторных
циклов, а также persistent outbox с Telegram-кнопками оплаты. Полный автоматический
live-сценарий пока не подключён; режим по умолчанию — `dry_run`, в котором write
transport не вызывается.

## Требования

- Python 3.12 или новее
- [uv](https://docs.astral.sh/uv/)

## Установка и проверка

```bash
uv sync --all-groups
uv run python -m theater_tickets
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
```

Для будущих настроек скопируйте `.env.example` в `.env` и задайте значения вне
Git. Пока `.env` не загружается автоматически, а статус CLI не показывает секреты.

Не включайте автоматический `BOOKING_MODE=live` до завершения worker-шага 17
из [roadmap](docs/roadmap.md). Checkout и startup recovery уже отклоняют
неполный или неподтверждённый handoff, но оплату всегда совершает пользователь
на стороне продавца.

Для будущего live checkout `BUYER_PROFILE_PATH` указывает на приватный JSON-файл
**вне** репозитория. Файл должен быть доступен только пользователю сервиса
(`0600`) и содержать ровно `lastname`, `firstname`, `middlename`, `email`,
`phone`, `personal_data_consent: true`. Не отправляйте этот файл, его значения
или путь в Git, логи либо Telegram. Само наличие профиля не включает checkout:
требуется явный live-вызов уже сохранённого intent, а автоматический worker пока
выключен.
