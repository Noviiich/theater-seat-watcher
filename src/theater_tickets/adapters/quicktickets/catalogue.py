"""Pure parsers for the public QuickTickets catalogue and session pages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

SESSION_HREF = re.compile(r"/(?:[^/]+/)?s(?P<id>[A-Za-z0-9_-]+)(?:/|$)")
EVENT_HREF = re.compile(r"/(?:[^/]+/)?e(?P<id>[A-Za-z0-9_-]+)(?:/|$)")


@dataclass(frozen=True, slots=True)
class CatalogueSessionRef:
    session_id: str
    event_id: str | None
    title: str | None
    date_hint: str | None
    href: str


@dataclass(frozen=True, slots=True)
class CatalogueSnapshot:
    sessions: tuple[CatalogueSessionRef, ...]
    complete: bool
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SessionPageDetails:
    start_dates: tuple[datetime, ...]
    iframe_urls: tuple[str, ...]


class _CatalogueHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: dict[str, CatalogueSessionRef] = {}
        self.errors: list[str] = []
        self._event_id: str | None = None
        self._event_title: str | None = None
        self._event_depth = 0
        self._anchor: dict[str, str] | None = None
        self._anchor_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if values.get("data-elem-type") == "event":
            self._event_depth += 1
            self._event_id = values.get("data-elem-id") or None
            self._event_title = values.get("data-filter-name") or None
        session_id = values.get("data-session-id")
        if session_id:
            self._add_ref(session_id, values.get("data-href", ""), values.get("data-date") or None)
        if tag == "a" and values.get("href"):
            self._anchor = values
            self._anchor_text = []
        if tag == "iframe" and values.get("src"):
            self._add_iframe(values["src"])

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._anchor is not None:
            href = self._anchor["href"]
            session = SESSION_HREF.search(urlparse(href).path)
            if session:
                self._add_ref(session.group("id"), href, self._anchor.get("data-date") or None)
            self._anchor = None
        if self._event_depth and tag not in {"a", "span", "li", "div", "section"}:
            return
        if self._event_depth and tag in {"div", "section", "article"}:
            self._event_depth -= 1
            if not self._event_depth:
                self._event_id = None
                self._event_title = None

    def handle_data(self, data: str) -> None:
        if self._anchor is not None:
            value = " ".join(data.split())
            if value:
                self._anchor_text.append(value)

    def _add_ref(self, session_id: str, href: str, date_hint: str | None) -> None:
        session_id = session_id.strip()
        if not session_id:
            self.errors.append("empty session id")
            return
        ref = CatalogueSessionRef(session_id, self._event_id, self._event_title, date_hint, href)
        previous = self.refs.get(session_id)
        if previous and previous.date_hint != date_hint and date_hint:
            self.errors.append(f"conflicting date for session {session_id}")
            return
        self.refs[session_id] = previous or ref

    def _add_iframe(self, src: str) -> None:
        # Kept for parser completeness; catalogue snapshots do not expose iframe URLs.
        del src


def parse_catalogue(html: str) -> CatalogueSnapshot:
    """Parse every session reference, preserving a failed/empty distinction."""
    parser = _CatalogueHTMLParser()
    parser.feed(html)
    parser.close()
    if not html.strip():
        return CatalogueSnapshot((), False, ("empty HTML",))
    if "data-elem-type" not in html and not parser.refs:
        return CatalogueSnapshot((), False, ("catalogue event markers are missing",))
    return CatalogueSnapshot(tuple(parser.refs.values()), not parser.errors, tuple(parser.errors))


class _SessionPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.json_scripts: list[str] = []
        self.iframes: list[str] = []
        self._script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "script" and (values.get("type") or "").lower() == "application/ld+json":
            self._script = []
        iframe_src = values.get("src")
        if tag == "iframe" and iframe_src:
            self.iframes.append(iframe_src)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._script is not None:
            self.json_scripts.append("".join(self._script))
            self._script = None

    def handle_data(self, data: str) -> None:
        if self._script is not None:
            self._script.append(data)


def parse_session_page(html: str) -> SessionPageDetails:
    """Extract timezone-aware JSON-LD dates and iframe context from one session page."""
    parser = _SessionPageParser()
    parser.feed(html)
    starts: list[datetime] = []
    for raw in parser.json_scripts:
        try:
            payload: Any = json.loads(raw)
        except json.JSONDecodeError:
            continue
        values = payload if isinstance(payload, list) else [payload]
        for item in values:
            if isinstance(item, dict) and isinstance(item.get("startDate"), str):
                value = datetime.fromisoformat(item["startDate"].replace("Z", "+00:00"))
                if value.tzinfo is None or value.utcoffset() is None:
                    raise ValueError("JSON-LD startDate must have timezone")
                starts.append(value)
    unique = tuple(dict.fromkeys(starts))
    if len(unique) > 1:
        raise ValueError("conflicting JSON-LD startDate values")
    if not unique:
        raise ValueError("session page has no timezone-aware JSON-LD startDate")
    return SessionPageDetails(unique, tuple(parser.iframes))
