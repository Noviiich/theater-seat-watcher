"""Read models and safe rendering for owner-scoped runtime status."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")


@dataclass(frozen=True, slots=True)
class CandidateStatus:
    candidate_id: str
    title: str
    state: str
    current_cycle_no: int
    next_run_at: datetime | None
    stop_reason: str | None
    expires_at: datetime | None
    starts_at: datetime | None = None
    subscription_enabled: bool = True
    watch_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class RuntimeStatus:
    last_complete_catalogue_at: datetime | None
    pending_outbox: int
    oldest_outbox_at: datetime | None
    ambiguous_writes: int
    candidates: tuple[CandidateStatus, ...]
    workers: tuple[tuple[str, str, datetime | None, str | None], ...] = ()
    active_subscriptions: int = 0
    paused_subscriptions: int = 0


class StatusReader(Protocol):
    async def for_user(self, telegram_user_id: str, *, now: datetime) -> RuntimeStatus: ...


def render_user_status(
    status: RuntimeStatus,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> str:
    """Summarize tracking and next actions for the Telegram menu."""
    _aware(now)
    active = status.active_subscriptions
    paused = status.paused_subscriptions
    if active:
        lines = [f"Отслеживание включено. Активных подписок: {active}."]
    elif paused:
        lines = ["Поиск приостановлен. Возобновите подписку в «Моих подписках»."]
    else:
        lines = ["Поиск не настроен. Создайте подписку кнопкой «➕ Новая подписка»."]
    if paused and active:
        lines.append(f"Подписок на паузе: {paused}.")

    if active:
        if status.last_complete_catalogue_at is None:
            lines.append("Афиша ещё не загружена. Бот начнёт поиск после первой загрузки.")
        else:
            age = max(0, int((now - status.last_complete_catalogue_at).total_seconds()))
            if age > stale_after_seconds:
                lines.append("Не удаётся обновить афишу. Бот продолжит попытки автоматически.")
            else:
                lines.append(f"Афиша проверена {_ago(age)} назад.")

    visible = [
        item
        for item in status.candidates
        if item.subscription_enabled
        and (item.watch_until is None or item.watch_until > now)
        and item.state not in {"stopped", "skipped_limit", "dry_run_completed"}
    ]
    if visible:
        lines.append("Найденные сеансы:")
        sessions: dict[tuple[str, datetime | None], list[CandidateStatus]] = {}
        for item in visible:
            sessions.setdefault((item.title, item.starts_at), []).append(item)
        for values in list(sessions.values())[:5]:
            item = values[0]
            title = item.title
            if item.starts_at is not None:
                _aware(item.starts_at)
                title += f" · {item.starts_at.astimezone(MOSCOW):%d.%m.%Y %H:%M} МСК"
            if len(values) == 1:
                lines.append(f"• {title}: {_user_candidate_state(item, now=now)}")
            else:
                ready = sum(
                    value.state in {"awaiting_payment", "renewal_waiting"} for value in values
                )
                attention = sum(value.state == "needs_attention" for value in values)
                lines.append(
                    f"• {title}: задач {len(values)}, ссылок подготовлено {ready}, "
                    f"требуют внимания {attention}."
                )
                if any(value.stop_reason == "sale_quantity_limit" for value in values):
                    lines.append("Лимит продавца меньше запрошенного количества билетов.")
        if len(sessions) > 5:
            lines.append(f"И ещё сеансов: {len(sessions) - 5}.")
    elif active:
        lines.append("Подходящих новых сеансов пока нет.")

    if status.pending_outbox:
        lines.append("Сообщение с результатом задерживается. Бот попробует отправить его снова.")
    if status.ambiguous_writes:
        lines.append("Результат оформления пока неясен. Повторная попытка приостановлена.")
    return "\n".join(lines)


def _user_candidate_state(item: CandidateStatus, *, now: datetime) -> str:
    states = {
        "queued": "готовится подбор мест",
        "dry_run_queued": "готовится проверка мест без бронирования",
        "dry_run_claimed": "проверяются места без бронирования",
        "processing": "подбираются места",
        "submitting": "оформляется заказ",
        "renewal_claimed": "подбираются места для новой ссылки",
        "renewal_waiting": "новая ссылка будет подготовлена после окончания текущего удержания",
        "awaiting_payment": "ссылка на оплату подготовлена; проверьте сообщения бота",
        "waiting_availability": "подходящих свободных мест пока нет; поиск продолжится",
        "waiting_budget": "достигнут лимит стоимости или заказов; поиск продолжится",
        "needs_attention": "нужно ваше участие; проверьте сообщения бота",
    }
    result = states.get(item.state, "проверяется")
    if item.stop_reason == "sale_quantity_limit":
        result = "лимит продавца меньше запрошенного количества билетов"
    if item.next_run_at is not None and item.state not in {"needs_attention"}:
        _aware(item.next_run_at)
        remaining = max(0, int((item.next_run_at - now).total_seconds()))
        result += f", следующая попытка через {_duration(remaining)}"
    if (
        item.expires_at is not None
        and item.expires_at > now
        and item.state in {"awaiting_payment", "renewal_waiting"}
    ):
        _aware(item.expires_at)
        result += f"; на оплату осталось {_duration(int((item.expires_at - now).total_seconds()))}"
    return result + "."


def render_status(
    status: RuntimeStatus,
    *,
    now: datetime,
    stale_after_seconds: int,
) -> str:
    """Render operational state without provider URLs or personal data."""
    _aware(now)
    lines = ["Состояние бота:"]
    if status.last_complete_catalogue_at is None:
        lines.append("• Полная афиша ещё не загружена.")
    else:
        age = max(0, int((now - status.last_complete_catalogue_at).total_seconds()))
        stale = " (устарела)" if age > stale_after_seconds else ""
        lines.append(f"• Последняя полная афиша: {_ago(age)} назад{stale}.")
    oldest = (
        max(0, int((now - status.oldest_outbox_at).total_seconds()))
        if status.oldest_outbox_at is not None
        else None
    )
    queue_age = f", старейшее {_ago(oldest)}" if oldest is not None else ""
    lines.append(f"• Очередь сообщений: {status.pending_outbox}{queue_age}.")
    lines.append(f"• Неоднозначные оформления: {status.ambiguous_writes}.")
    if status.workers:
        worker_values: list[str] = []
        for name, state, last_success, error in status.workers:
            value = f"{name}={state}" + (f" ({error})" if error else "")
            if last_success is not None:
                success_age = max(0, int((now - last_success).total_seconds()))
                value += f", успех {_ago(success_age)} назад"
            worker_values.append(value)
        workers = ", ".join(worker_values)
        lines.append(f"• Фоновые задачи: {workers}.")
    if not status.candidates:
        lines.append("Активных или завершённых сеансов-кандидатов нет.")
        return "\n".join(lines)
    lines.append("Сеансы:")
    for item in status.candidates[:8]:
        details = [f"цикл №{item.current_cycle_no}", _state_label(item.state)]
        if item.next_run_at is not None:
            remaining = max(0, int((item.next_run_at - now).total_seconds()))
            details.append(f"следующая проверка через {_duration(remaining)}")
        if item.expires_at is not None and item.expires_at > now:
            ttl = int((item.expires_at - now).total_seconds())
            details.append(f"ссылка действует ещё {_duration(ttl)}")
        if item.stop_reason:
            details.append(f"причина: {item.stop_reason}")
        lines.append(f"• {item.title} [{item.candidate_id}]: " + "; ".join(details) + ".")
    if len(status.candidates) > 8:
        lines.append(f"Ещё задач: {len(status.candidates) - 8}. Общий прогресс — в меню «Статус».")
    return "\n".join(lines)


def remaining_ttl_line(*, expires_at: datetime, now: datetime) -> str:
    _aware(expires_at)
    _aware(now)
    seconds = max(0, int((expires_at - now).total_seconds()))
    return f"До окончания удержания: {_duration(seconds)}."


def _state_label(value: str) -> str:
    return {
        "queued": "ожидает обработки",
        "processing": "обрабатывается",
        "renewal_waiting": "ожидает нового цикла",
        "awaiting_payment": "ожидает оплаты или нового цикла",
        "waiting_availability": "ожидает соседние места",
        "waiting_budget": "ожидает доступный бюджет",
        "needs_attention": "требуется действие пользователя",
        "stopped": "остановлен",
        "dry_run_completed": "dry-run завершён",
    }.get(value, value)


def _ago(seconds: int) -> str:
    return _duration(seconds)


def _duration(seconds: int) -> str:
    minutes, rest = divmod(max(0, seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {rest} с"
    return f"{rest} с"


def _aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("status timestamps must be timezone-aware")
