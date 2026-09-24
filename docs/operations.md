# Эксплуатация и восстановление

## Границы текущего запуска

Production entrypoint запускает polling публичных страниц QuickTickets,
Telegram long polling и persistent runtime. Режим по умолчанию — `dry_run`.
Ограниченный `live` доступен только с платёжным терминалом и денежными лимитами каждой подписки;
контейнер сам не создаёт live-подписку. Реальное оформление включается отдельным
подтверждением её владельца в Telegram. Оплата всегда выполняется пользователем.

Один контейнер обслуживает одну SQLite-БД. Compose не публикует порты и хранит
БД/backup в отдельных named volumes. Дополнительный экземпляр всё равно будет
остановлен runtime lease, но `docker compose up --scale bot=2` не поддерживается.

## Секреты и профили

1. Скопировать `.env.example` в `.env`, установить права `0600` и заполнить
   `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_USER_ID`. `.env` не коммитить.
   Администратор указывает свой числовой Telegram ID; остальные пользователи
   сначала отправляют запрос доступа, который администратор принимает или отклоняет
   кнопкой в Telegram.
2. Для обычной работы оставить `BOOKING_MODE=dry_run`. Для контролируемого
   запуска задать `BOOKING_MODE=live`, проверенный
   `QUICKTICKETS_PAYMENT_TERMINAL_CHOICE`. Денежные лимиты каждый пользователь
   вводит при создании своей подписки в Telegram. `DATABASE_URL` в Compose указывает
   на `/app/data/theater_tickets.sqlite3`.
3. Создать проверенный `config/halls/<profile_id>.yaml` по
   [алгоритму мест](seat-selection.md) и примеру в `config/halls/README.md`.
   Fingerprint, сегменты, ось сцены и оценки проверяются на актуальной схеме.
   Если такого профиля ещё нет, в `/subscribe` можно указать `auto`: бот
   консервативно строит сегменты из координат текущей схемы и разрывает ряд на
   большом промежутке. Проверенный профиль остаётся предпочтительным.
4. Пользователь заполняет свой профиль в личном чате:
   `/profile set Фамилия | Имя | Отчество | email | телефон`. Данные хранятся
   только в SQLite БД владельца; `/profile` показывает текущий профиль, а та же
   команда обновляет его. Не включать значения в Git, логи или тикеты.

Допущенный пользователь выбирает количество мест в «Новой подписке»
или через `/subscribe`, вводит максимум за один билет и максимум за весь заказ
(рубли и копейки), затем подтверждает создание. Общая сумма включает возможную
комиссию. Число мест и оба денежных предела действуют на **каждый сеанс отдельно**:
три новых подходящих сеанса могут дать три заказа и три ссылки, каждый в своём
лимите; общая сумма трёх заказов может быть выше предела одного заказа.
Общий предел пакета и одновременно активных заказов через Telegram не задаётся.
Лимиты хранятся отдельно у каждой подписки; «Мои подписки» показывает
их владельцу. Live-подписка повторно оформляет каждый принятый сеанс примерно
через 1200 секунд от начала предыдущего удержания и присылает новую ссылку при
наличии подходящих мест. Повторы продолжаются до остановки сеанса, паузы или
удаления подписки либо начала сеанса. В dry-run те же лимиты используются при подборе мест,
а реальный заказ не создаётся.

Hall profile не содержит ПДн и может версионироваться. Профиль покупателя, cookies,
`.env`, БД, backup, HAR и платёжные URL — приватные эксплуатационные данные.

## Первый dry-run запуск

```bash
docker build --platform linux/amd64 -t theater-tickets:local -f deploy/Dockerfile .
docker compose -f deploy/compose.yaml up -d
docker compose -f deploy/compose.yaml ps
docker compose -f deploy/compose.yaml exec bot theater-tickets smoke
```

`run` перед стартом сам выполняет Alembic upgrade и полный `integrity_check`
вместе с проверкой schema head. Периодический healthcheck использует отдельную
быстрый read-only модуль `python -m theater_tickets.healthcheck`: он импортирует
только стандартную библиотеку, читает ревизию схемы и не блокирует рабочую
WAL-базу длительным сканированием. Обе проверки работают без запросов
QuickTickets и Telegram. Логи можно смотреть командой
`docker compose -f deploy/compose.yaml logs -f bot`; они структурированы и не
должны содержать токены, контакты или payment URL.

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

После [однократной настройки автодеплоя](deployment.md) обновление запускается
push в `main`; следующие команды остаются для ручного восстановления.

1. Проверить `/status` и создать backup.
2. Получить готовый образ из CI или локально выполнить
   `docker build --platform linux/amd64 -t theater-tickets:local -f deploy/Dockerfile .`.
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

Текущий статус и ограничения зафиксированы в
[протоколе приёмки](live-acceptance.md). Наличие `BOOKING_MODE=live` в `.env`
само по себе не создаёт заказ: новая подписка администратора требует заполненного
профиля и отдельной кнопки подтверждения. После создания
подписки следите за статусом и при необходимости остановите её в «Мои подписки».
Успешная реальная приёмка ещё не зафиксирована.
