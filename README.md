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
write transport отсутствует; live защищён gate до приёмки шага 20.

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

Не включайте `BOOKING_MODE=live` до ограниченной реальной приёмки шага 20
из [roadmap](docs/roadmap.md): production entrypoint сейчас безопасно отклонит live.
Checkout и startup recovery уже отклоняют
неполный или неподтверждённый handoff, но оплату всегда совершает пользователь
на стороне продавца.

Для будущего live checkout `BUYER_PROFILE_PATH` указывает на приватный JSON-файл
**вне** репозитория. Файл должен быть доступен только пользователю сервиса
(`0600`) и содержать ровно `lastname`, `firstname`, `middlename`, `email`,
`phone`, `personal_data_consent: true`. Не отправляйте этот файл, его значения
или путь в Git, логи либо Telegram. Само наличие профиля не включает checkout:
требуется успешная приёмка и отдельное снятие live-gate.
