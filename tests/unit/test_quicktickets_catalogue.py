from __future__ import annotations

import pytest

from theater_tickets.adapters.quicktickets.catalogue import parse_catalogue, parse_session_page


def test_catalogue_deduplicates_visible_and_hidden_session_links() -> None:
    html = """
    <div data-elem-type="event" data-elem-id="386" data-filter-name="Онегин">
      <a href="/theatre/e386">Онегин</a>
      <a href="/theatre/s3159" data-date="2026-10-24">24 октября</a>
      <span class="event-filter-session" data-session-id="3159" data-date="2026-10-24"></span>
    </div>
    """

    snapshot = parse_catalogue(html)

    assert snapshot.complete
    assert len(snapshot.sessions) == 1
    assert snapshot.sessions[0].session_id == "3159"
    assert snapshot.sessions[0].event_id == "386"
    assert snapshot.sessions[0].date_hint == "2026-10-24"


def test_catalogue_rejects_conflicting_date_hints() -> None:
    snapshot = parse_catalogue(
        '<div data-elem-type="event" data-elem-id="1">'
        '<a href="/t/s9" data-date="2026-01-01"></a>'
        '<span data-session-id="9" data-date="2026-01-02"></span></div>'
    )

    assert not snapshot.complete
    assert "conflicting date" in snapshot.errors[0]


@pytest.mark.parametrize("html", ["", "<html><body>broken</body></html>"])
def test_catalogue_does_not_turn_missing_markers_into_empty_success(html: str) -> None:
    snapshot = parse_catalogue(html)

    assert not snapshot.complete
    assert snapshot.errors


def test_session_page_extracts_timezone_aware_start_date_and_iframe() -> None:
    details = parse_session_page(
        '<script type="application/ld+json">'
        '{"@type":"Event","startDate":"2026-10-24T16:00:00+03:00"}'
        '</script><iframe src="https://hall.quicktickets.ru/?scope=qt"></iframe>'
    )

    assert details.start_dates[0].isoformat() == "2026-10-24T16:00:00+03:00"
    assert details.iframe_urls == ("https://hall.quicktickets.ru/?scope=qt",)


def test_session_page_rejects_missing_or_conflicting_dates() -> None:
    with pytest.raises(ValueError, match="timezone"):
        parse_session_page('<script type="application/ld+json">{"startDate":"2026-10-24"}</script>')
    with pytest.raises(ValueError, match="conflicting"):
        parse_session_page(
            '<script type="application/ld+json">{"startDate":"2026-01-01T10:00:00+03:00"}</script>'
            '<script type="application/ld+json">{"startDate":"2026-01-02T10:00:00+03:00"}</script>'
        )
