"""Validate history pagination without making any network requests.

Provider docs: https://docs.onlyfansapi.com/api-reference/chat-messages/list-chat-messages
An HTTP client must also disable redirects or validate each redirect target.
"""
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

from conversation_engine.identity import require_identifier


@dataclass(frozen=True, slots=True)
class HistoryPage:
    messages: tuple[object, ...]
    next_page: str | None


def _path_component(value: str, name: str) -> str:
    require_identifier(value, name)
    if value in {".", ".."} or any(character in value for character in "/\\%?#"):
        raise ValueError(f"unsafe {name}")
    return quote(value, safe="")


def validate_history_next_page(value: str, *, connection_id: str, chat_id: str) -> str:
    expected_path = f"/api/{_path_component(connection_id, 'connection_id')}/chats/{_path_component(chat_id, 'chat_id')}/messages"
    if type(value) is not str or not value or value != value.strip() or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("invalid continuation URL")
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise ValueError("invalid continuation URL") from exc
    if (parts.scheme != "https" or parts.hostname != "app.onlyfansapi.com" or port not in (None, 443) or
            parts.username is not None or parts.password is not None or parts.fragment or parts.path != expected_path):
        raise ValueError("continuation URL is outside the allowed account and chat")
    return value


def parse_history_page(body: object, *, connection_id: str, chat_id: str) -> HistoryPage:
    _path_component(connection_id, "connection_id")
    _path_component(chat_id, "chat_id")
    if type(body) is not dict or type(body.get("data")) is not list or type(body.get("_pagination")) is not dict:
        raise ValueError("malformed history page")
    pagination = body["_pagination"]
    if "next_page" not in pagination:
        raise ValueError("history completeness is unknown")
    next_page = pagination["next_page"]
    if next_page is not None:
        next_page = validate_history_next_page(next_page, connection_id=connection_id, chat_id=chat_id)
    return HistoryPage(tuple(body["data"]), next_page)
