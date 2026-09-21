# Эксплуатация и восстановление

## Границы текущего запуска

Production entrypoint запускает реальный polling публичных страниц QuickTickets,
Telegram long polling и полный persistent runtime, но только в `dry_run`.
Изменяющий checkout transport в этой композиции отсутствует. `BOOKING_MODE=live`
намеренно отклоняется до ограниченной приёмки шага 20; не удаляйте эту защиту
ради пробного запуска. Оплата всегда выполняется пользователем.

Один контейнер обслуживает одну SQLite-БД. Compose не публикует порты и хранит
БД/backup в отдельных named volumes. Дополнительный экземпляр всё равно будет
остановлен runtime lease, но `docker compose up --scale bot=2` не поддерживается.

## Секреты и профили

1. Скопировать `.env.example` в `.env`, установить права `0600` и заполнить
   `TELEGRAM_BOT_TOKEN`, `ALLOWED_TELEGRAM_USER_IDS`. `.env` не коммитить.
2. Оставить `BOOKING_MODE=dry_run`. `DATABASE_URL` в Compose принудительно
   указывает на `/app/data/theater_tickets.sqlite3`.
3. Создать проверенный `config/halls/<profile_id>.yaml` по
   [алгоритму мест](seat-selection.md) и примеру в `config/halls/README.md`.
   Fingerprint, сегменты, ось сцены и оценки проверяются на актуальной схеме.
4. Профиль покупателя нужен только для будущего live. Это файл вне Git с правами
   `0600`, содержащий ровно `lastname`, `firstname`, `middlename`, `email`,
   `phone`, `personal_data_consent: true`. Для контейнера его следует подключить
   read-only в `/run/secrets/buyer-profile.json` и задать такой же
   `BUYER_PROFILE_PATH`; содержимое и путь не выводить в логи.

Hall profile не содержит ПДн и может версионироваться. Buyer profile, cookies,
`.env`, БД, backup, HAR и платёжные URL — приватные эксплуатационные данные.

## Первый dry-run запуск

```bash
docker compose -f deploy/compose.yaml build
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml exec bot theater-tickets smoke
```

`run` перед стартом сам выполняет Alembic upgrade и проверяет integrity/schema
head. Healthcheck выполняет только read-only `smoke`, без запросов QuickTickets и
Telegram. Логи можно смотреть командой `docker compose -f deploy/compose.yaml logs -f bot`;
они структурированы и не должны содержать токены, контакты или payment URL.

Первый полный снимок каждой подписки становится baseline и не создаёт старые
заказы. После появления нового сеанса dry-run сохраняет решение без allocation,
POST и ссылки оплаты. `/status` показывает свежесть афиши, очередь и причины
ожидания. Для остановки конкретного Candidate используется `/stop <candidate-id>`,
для всей подписки — `/pause <subscription-id>`; `/resume` не сбрасывает baseline.

## Настройки времени и повторов

- `POLL_INTERVAL_SECONDS=60`, `POLL_JITTER_SECONDS=10` — афиша.
- `AVAILABILITY_RETRY_SECONDS=180` — повтор при отсутствии мест/бюджета.
- `RENEWAL_INTERVAL_SECONDS=1200` — новый цикл от фактического `held_at`.
- `EXPECTED_HOLD_TTL_SECONDS=1200` — ожидаемый TTL; фактический ответ checkout
  имеет приоритет.

Не уменьшать два значения `1200` без нового подтверждения контракта. Оплата не
останавливает повторы автоматически: после оплаты пользователь нажимает кнопку
остановки или выполняет `/stop`. История старых RenewalCycle/Order сохраняется;
рестарт читает `next_run_at` и не воспроизводит все пропущенные тики.

## Обновление

1. Проверить `/status` и создать backup.
2. Получить новую версию и выполнить `docker compose -f deploy/compose.yaml build`.
3. Выполнить `docker compose -f deploy/compose.yaml up -d --no-deps bot`.
4. Проверить `ps`, `logs` и `theater-tickets smoke`.

Не использовать `docker compose down -v`: ключ `-v` удалит постоянную БД и
контейнерные backup. Миграции выполняются до старта worker; downgrade рабочей БД
не входит в обычное обновление.

## Backup

Работающую WAL-БД нельзя резервировать копированием одного `.sqlite3`. Команда
ниже использует SQLite Backup API, затем `integrity_check` и права `0600`:

```bash
docker compose -f deploy/compose.yaml exec bot \
  theater-tickets backup /app/backups/theater-tickets-YYYYMMDDTHHMMSSZ.sqlite3
docker compose -f deploy/compose.yaml cp \
  bot:/app/backups/theater-tickets-YYYYMMDDTHHMMSSZ.sqlite3 ./backups/
```

Имя должно быть новым: существующий backup не перезаписывается. Копия содержит
ПДн и payment URL и защищается как `.env`.

## Restore

Restore выполняется только при остановленном bot:

```bash
docker compose -f deploy/compose.yaml stop bot
docker compose -f deploy/compose.yaml run --rm bot \
  restore /app/backups/theater-tickets-YYYYMMDDTHHMMSSZ.sqlite3 --yes
docker compose -f deploy/compose.yaml run --rm bot migrate
docker compose -f deploy/compose.yaml run --rm bot smoke
docker compose -f deploy/compose.yaml up -d bot
```

Перед заменой команда создаёт в data volume owner-only файл
`*.pre-restore-<UTC>.bak`, восстанавливает через SQLite API, удаляет старые
WAL/SHM sidecars и проверяет целостность. После старта сохраняются Candidate,
циклы, intent, allocation, outbox dedup и история заказов; очередь продолжает
работу без создания нового цикла только из-за рестарта.

## `needs_attention`

Если `/status` показывает `needs_attention`:

1. Остановить повторы только проблемного Candidate через `/stop` и сделать backup.
2. По внутренним candidate/intent ID определить стадию, не копируя buyer payload
   или полную ссылку оплаты в тикет/лог.
3. Не освобождать allocation и не повторять POST вручную после timeout. Без
   подтверждённого provider lookup состояние остаётся заблокированным.
4. Проверить актуальность hall profile, buyer profile, CAPTCHA/auth и контракт.
   Исправление конфигурации не превращает неизвестный заказ в доказанный отказ.

Ручное разрешение допустимо только после доказательства отсутствия заказа и
удержания либо с сохранением блокировки. Автоматическая проверка оплаты и отмена
заказа не выполняются.

## Подготовка live

После успешного dry-run оператор задаёт полные лимиты цены/пакета/активных
заказов, проверяет buyer/hall profiles, backup и `/status`. Затем в рамках шага
20 выполняется минимальная контролируемая приёмка одного заказа и одного нового
цикла через 1200 секунд. Только зафиксированный результат этой приёмки разрешает
снять startup gate для `BOOKING_MODE=live`; простой перевод переменной сейчас
завершится безопасной ошибкой до запуска worker.
