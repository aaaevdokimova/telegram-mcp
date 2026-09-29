"""One bounded, free-only public channel search per call.

Native offset strings stay in a bounded daemon-local cursor cache. No chat is
joined, no read state is changed, no media is fetched, and no Stars payment is
ever authorized. Telegram supplies every link; no message-ID arithmetic occurs.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import secrets
import time
from urllib.parse import urlsplit

from .models import PublicPostRecord, PublicPostSearchQuota, PublicPostSearchResult, UntrustedText
from .tdjson import TdApi, TdlibError, TdlibSchema

CURSOR_TTL = 600.0
CURSOR_LIMIT = 64
MAX_PAGES = 50
MAX_NATIVE_OFFSET = 4096


@dataclass(frozen=True)
class CursorState:
    account_id: int
    query: str
    search_type: str
    offset: str
    visited: frozenset[str]
    seen_messages: frozenset[tuple[int, int]]
    expires: float


def _text(value: str, maximum: int) -> UntrustedText:
    return UntrustedText(value=value[:maximum], truncated=len(value) > maximum,
                         original_character_count=len(value))


def _quota(response: dict) -> PublicPostSearchQuota:
    if not isinstance(response, dict) or response.get("@type") != "publicPostSearchLimits":
        raise ValueError("Invalid public search limits")
    return PublicPostSearchQuota.model_validate({key: response[key] for key in PublicPostSearchQuota.model_fields})


def _public_url(response: dict) -> str | None:
    if not isinstance(response, dict) or response.get("@type") != "messageLink" or response.get("is_public") is not True:
        return None
    link = response.get("link")
    if not isinstance(link, str) or len(link) > 2048 or any(ord(c) < 33 for c in link):
        return None
    try:
        parsed = urlsplit(link)
        if (parsed.scheme != "https" or parsed.hostname not in {"t.me", "telegram.me"}
                or parsed.username or parsed.password or parsed.port is not None or parsed.fragment
                or not re.fullmatch(r"/[A-Za-z0-9_]+/[0-9]+", parsed.path)
                or parsed.path.startswith("/c/")):
            return None
    except ValueError:
        return None
    return link


def _failure(exc: TdlibError) -> tuple[str, str, int | None]:
    # TDLib explicitly prohibits processing/displaying error messages for 406.
    if exc.code == 406:
        return "unavailable", "telegram_refused", None
    message = exc.message.upper()
    if (any(value in message for value in ("UNKNOWN METHOD", "UNKNOWN FUNCTION", "UNKNOWN CLASS",
            "METHOD_INVALID", "METHOD_NOT_FOUND", "FEATURE_NOT_SUPPORTED", "METHOD_NOT_AVAILABLE"))
            and "FROZEN_METHOD_INVALID" not in message):
        return "unsupported_feature", "unsupported_feature", None
    wait = re.search(r"(?:FLOOD_WAIT_|RETRY AFTER )(\d{1,9})", message)
    if exc.code == 429 or wait:
        return "unavailable", "rate_limited", int(wait[1]) if wait else None
    if "PREMIUM" in message:
        return "unavailable", "premium_required", None
    if exc.code == 401:
        return "unavailable", "authorization_required", None
    if "FROZEN_METHOD_INVALID" in message:
        return "unavailable", "account_restricted", None
    return "unavailable", "telegram_refused", None


class PublicPostSearch:
    """Only the profile's serial worker uses this bounded ephemeral state."""

    def __init__(self) -> None:
        self.cursors: OrderedDict[str, CursorState] = OrderedDict()

    def _cursor(self, token: str | None, query: str, account: int) -> CursorState:
        now = time.monotonic()
        for key in list(self.cursors):
            if self.cursors[key].expires <= now:
                del self.cursors[key]
        if token is None:
            return CursorState(account, query, "public_channel_posts", "", frozenset(), frozenset(), now + CURSOR_TTL)
        value = self.cursors.get(token)
        if (value is None or value.account_id != account or value.query != query
                or value.search_type != "public_channel_posts"):
            raise ValueError("Public-post cursor expired, was already used, or belongs to another account/query; start a new search")
        return value

    def _remember(self, value: CursorState) -> str:
        token = "public:" + secrets.token_hex(24)
        self.cursors[token] = value
        while len(self.cursors) > CURSOR_LIMIT:
            self.cursors.popitem(last=False)
        return token

    def search(self, session, *, query: str, cursor: str | None = None, limit: int = 20,
               schema: TdlibSchema = TdlibSchema.CURRENT, timeout: float = 50.0) -> dict:
        if (not isinstance(query, str) or not 2 <= len(query.strip()) <= 200
                or type(limit) is not int or not 1 <= limit <= 20
                or (cursor is not None and (not isinstance(cursor, str) or not 8 <= len(cursor) <= 512))):
            raise ValueError("Invalid public-post search bounds")
        query = query.strip()
        result = dict(status="ok", reason=None, search_performed=False, quota=None, quota_source="unavailable",
                      items=[], count=0, requested_limit=limit, next_cursor=None)
        def output(**changes):
            return PublicPostSearchResult.model_validate({**result, **changes}).model_dump(mode="json")
        if schema is not TdlibSchema.CURRENT:
            return output(status="unsupported_feature", reason="unsupported_feature")
        if type(session.user_id) is not int or session.user_id <= 0:
            raise ValueError("A verified Telegram account is required")
        state = self._cursor(cursor, query, session.user_id)
        deadline = time.monotonic() + timeout
        def remaining(cap=10.0):
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError()
            return min(left, cap)
        api = TdApi(schema)
        try:
            quota = _quota(session.request(api.get_public_post_search_limits(query), timeout=remaining(15)))
            result.update(quota=quota, quota_source="preflight")
            # Successful native continuations are free by contract. The cursor
            # proves the query/account already succeeded; it is not an offset
            # supplied by a model attempting to bypass a quota check.
            free = bool(state.offset) or quota.is_current_query_free or (
                quota.remaining_free_query_count > 0 and quota.next_free_query_in == 0)
            if not free:
                return output(status="unavailable", reason="free_quota_unavailable",
                              retry_after_seconds=quota.next_free_query_in or None)
            search_timeout = remaining(20)
            result["search_performed"] = True
            response = session.request(api.search_public_posts(query, offset=state.offset, limit=limit), timeout=search_timeout)
            if not isinstance(response, dict) or response.get("@type") != "foundPublicPosts":
                raise ValueError("Invalid native public search result")
            if response.get("search_limits") is not None:
                quota = _quota(response["search_limits"])
                result.update(quota=quota, quota_source="post_search")
            if type(response.get("are_limits_exceeded")) is not bool:
                raise ValueError("Invalid quota status")
            # TDLib can return is_current_query_free=true even in this failure
            # response. The explicit failure flag always takes precedence.
            if response["are_limits_exceeded"]:
                return output(status="unavailable", reason="free_quota_unavailable",
                              retry_after_seconds=quota.next_free_query_in or None)
            messages, offset = response.get("messages"), response.get("next_offset")
            if (not isinstance(messages, list) or len(messages) > limit
                    or not isinstance(offset, str) or len(offset) > MAX_NATIVE_OFFSET):
                raise ValueError("Invalid native public search page")
            items, seen, chats, groups, duplicates = [], set(state.seen_messages), {}, {}, 0
            for message in messages:
                if not isinstance(message, dict):
                    raise ValueError("Invalid public post")
                chat_id, message_id = message.get("chat_id"), message.get("id")
                if (type(chat_id) is not int or not -(2**53) < chat_id < 2**53 or chat_id == 0
                        or type(message_id) is not int or not 0 < message_id < 2**53):
                    raise ValueError("Invalid public post identity")
                key = chat_id, message_id
                if key in seen:
                    duplicates += 1
                    continue
                seen.add(key)
                items.append(self._record(session, message, chats, groups, remaining))
            result.update(items=items, count=len(items), duplicates_omitted_count=duplicates)
            if cursor is not None:
                # One successful consumption prevents a stale cursor replay from
                # returning duplicates or walking the same native page again.
                del self.cursors[cursor]
            visited = state.visited | {state.offset}
            if offset and offset in visited:
                return output(status="failed", reason="pagination_loop")
            if offset and len(visited) >= MAX_PAGES:
                return output(status="failed", reason="pagination_bound")
            if offset:
                result["next_cursor"] = self._remember(CursorState(state.account_id, query, state.search_type,
                    offset, frozenset(visited), frozenset(seen), time.monotonic() + CURSOR_TTL))
            return output()
        except TdlibError as exc:
            status, reason, wait = _failure(exc)
            return output(status=status, reason=reason, retry_after_seconds=wait)
        except TimeoutError:
            return output(status="failed", reason="native_timeout")
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, OSError):
            return output(status="failed", reason="invalid_response")

    @staticmethod
    def _record(session, message, chats, groups, remaining) -> PublicPostRecord:
        chat_id, message_id = message["chat_id"], message["id"]
        if chat_id not in chats:
            try:
                chats[chat_id] = session.get_chat(chat_id, timeout=remaining())
            except (TdlibError, TimeoutError):
                chats[chat_id] = {}
        chat = chats[chat_id]
        if not isinstance(chat, dict):
            raise ValueError("Invalid channel metadata")
        kind = chat.get("type", {})
        if not isinstance(kind, dict) or ("id" in chat and chat["id"] != chat_id):
            raise ValueError("Invalid channel metadata")
        if kind and (kind.get("@type") != "chatTypeSupergroup" or kind.get("is_channel") is not True):
            raise ValueError("Public search returned a non-channel chat")
        username = None
        group_id = kind.get("supergroup_id")
        if type(group_id) is int and group_id > 0:
            if group_id not in groups:
                try:
                    groups[group_id] = session.request({"@type": "getSupergroup", "supergroup_id": group_id}, timeout=remaining())
                except (TdlibError, TimeoutError):
                    groups[group_id] = {}
            group = groups[group_id]
            if not isinstance(group, dict):
                raise ValueError("Invalid supergroup metadata")
            usernames = group.get("usernames") or {}
            if not isinstance(usernames, dict):
                raise ValueError("Invalid channel usernames")
            names = usernames.get("active_usernames", [])
            if not isinstance(names, list):
                raise ValueError("Invalid channel usernames")
            if names and isinstance(names[0], str) and re.fullmatch(r"[A-Za-z0-9_]{1,32}", names[0]):
                username = _text("@" + names[0], 33)
        try:
            link = _public_url(session.request({"@type": "getMessageLink", "chat_id": chat_id,
                "message_id": message_id, "media_timestamp": 0, "for_album": False,
                "in_message_thread": False}, timeout=remaining()))
        except (TdlibError, TimeoutError):
            link = None
        content = message.get("content", {})
        if not isinstance(content, dict):
            raise ValueError("Invalid public post content")
        content_type = content.get("@type", "messageUnsupported")
        formatted = content.get("text") if content_type == "messageText" else content.get("caption")
        if formatted is None:
            formatted = {}
        if not isinstance(formatted, dict):
            raise ValueError("Invalid public post text")
        text = formatted.get("text", "")
        title, date = chat.get("title", ""), message.get("date")
        if (not isinstance(text, str) or not isinstance(title, str) or not isinstance(content_type, str)
                or type(date) is not int or not 0 < date < 2**31):
            raise ValueError("Invalid public post content")
        return PublicPostRecord(chat_id=chat_id, message_id=message_id, channel_title=_text(title, 256),
            username=username, sent_at=datetime.fromtimestamp(date, timezone.utc), text=_text(text, 4000),
            content_type=content_type[:80], public_url=link)
