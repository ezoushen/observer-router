"""The deterministic prompt guard.

Requests are sanitised before provider selection: image payloads and large base64 blobs are
replaced with markers, individual fields are folded, and older messages are dropped only when
the remaining conversation exceeds the character budget. System messages and the newest
conversation context receive priority. This bounds KV allocation without depending on another
model call, so it still works when every upstream quota is exhausted.
"""

from __future__ import annotations

import json
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass

from observer_router.config import Provider

_DATA_URL = re.compile(r"data:image/[^;,\s]+;base64,[^\s\"']+", re.IGNORECASE)
_IMAGE_PART_TYPES = {"image", "image_url", "input_image"}
_TEXT_PART_TYPES = {"text", "input_text"}
_FOLD_MARKER = "\n[... content folded by observer-router ...]\n"


def _no_log(message: str) -> None:
    return


@dataclass(frozen=True)
class PromptLimits:
    """Character budgets the guard enforces; a provider limit can only tighten them."""

    max_prompt_chars: int = 240_000
    max_field_chars: int = 80_000
    min_retained_chars: int = 1_024
    base64_blob_chars: int = 16_384


def content_chars(content) -> int:
    """Count the characters a message part contributes to the budget."""
    if isinstance(content, str):
        return len(content)
    if isinstance(content, list):
        return sum(content_chars(item) for item in content)
    if isinstance(content, dict):
        return sum(len(str(key)) + content_chars(value) for key, value in content.items())
    return len(str(content)) if content is not None else 0


def message_chars(messages: list[dict]) -> int:
    """The characters a conversation spends from the prompt budget."""
    return sum(content_chars(message.get("content")) for message in messages)


def _fold_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    if limit <= 0:
        return ""
    if len(_FOLD_MARKER) >= limit:
        return _FOLD_MARKER[:limit]
    available = limit - len(_FOLD_MARKER)
    head = max(1, available * 3 // 5)
    tail = available - head
    return text[:head] + _FOLD_MARKER + (text[-tail:] if tail else "")


class PromptGuard:
    """Sanitas and bounds a conversation for one character budget.

    `limits` are the router-wide budgets. `compact` may be called with a tighter limit when a
    provider sets its own; `fit` applies that provider limit to a request. A change is counted
    once through `record`, and `snapshot` reports the limits and the counters.
    """

    def __init__(self, limits: PromptLimits = PromptLimits(), log: Callable[[str], None] = _no_log):
        self.limits = limits
        self._log = log
        self._blob = re.compile(
            rf"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{{{limits.base64_blob_chars},}}={{0,2}}"
            rf"(?![A-Za-z0-9+/=])"
        )
        self._lock = threading.Lock()
        self._totals = {
            "compacted_requests": 0,
            "input_chars": 0,
            "output_chars": 0,
            "dropped_messages": 0,
            "removed_image_parts": 0,
            "removed_blobs": 0,
        }

    def _clean_text(self, text: str, stats: dict) -> str:
        def omit_data_url(match: re.Match) -> str:
            stats["removed_blobs"] += 1
            return f"[image binary payload omitted: {len(match.group(0)):,} chars]"

        def omit_blob(match: re.Match) -> str:
            stats["removed_blobs"] += 1
            return f"[binary payload omitted: {len(match.group(0)):,} chars]"

        cleaned = _DATA_URL.sub(omit_data_url, text)
        cleaned = self._blob.sub(omit_blob, cleaned)
        if len(cleaned) > self.limits.max_field_chars:
            stats["folded_fields"] += 1
            cleaned = _fold_text(cleaned, self.limits.max_field_chars)
        return cleaned

    def _clean_content(self, content, stats: dict) -> str:
        if isinstance(content, str):
            return self._clean_text(content, stats)
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and item.get("type") in _IMAGE_PART_TYPES:
                    stats["removed_image_parts"] += 1
                    parts.append("[image omitted from observer prompt]")
                elif isinstance(item, dict) and item.get("type") in _TEXT_PART_TYPES:
                    parts.append(self._clean_text(str(item.get("text") or ""), stats))
                else:
                    parts.append(self._clean_text(json.dumps(item, ensure_ascii=False), stats))
            return "\n".join(part for part in parts if part)
        if isinstance(content, dict):
            if content.get("type") in _IMAGE_PART_TYPES:
                stats["removed_image_parts"] += 1
                return "[image omitted from observer prompt]"
            return self._clean_text(json.dumps(content, ensure_ascii=False), stats)
        return self._clean_text(str(content), stats) if content is not None else ""

    def compact(self, messages: list[dict],
                limit: int | None = None) -> tuple[list[dict], dict]:
        """Return a bounded conversation while preserving system and newest context.

        `limit` is the character budget; by default the router-wide one.
        """
        limit = self.limits.max_prompt_chars if limit is None else limit
        stats = {
            "changed": False,
            "input_chars": message_chars(messages),
            "output_chars": 0,
            "dropped_messages": 0,
            "folded_fields": 0,
            "removed_image_parts": 0,
            "removed_blobs": 0,
        }
        sanitized = []
        for message in messages:
            if not isinstance(message, dict):
                stats["dropped_messages"] += 1
                continue
            cleaned = dict(message)
            cleaned["content"] = self._clean_content(message.get("content"), stats)
            sanitized.append(cleaned)

        if message_chars(sanitized) <= limit:
            stats["output_chars"] = message_chars(sanitized)
            stats["changed"] = sanitized != messages
            return sanitized, stats

        system_indexes = [
            index for index, message in enumerate(sanitized) if message.get("role") == "system"
        ]
        recent_indexes = [
            index for index in reversed(range(len(sanitized))) if index not in system_indexes
        ]
        # Give the primary system contract and newest non-system message first claim on the
        # budget; processing every system message first could otherwise discard the request
        # that the observer is meant to answer.
        priority = system_indexes[:1] + recent_indexes[:1] + system_indexes[1:] + recent_indexes[1:]
        selected = {}
        remaining = limit
        # The newest non-system message is the request the model must answer, so an oversized
        # system message may not take the whole budget: up to half is held back for it.
        newest = recent_indexes[0] if recent_indexes else None
        reserved = 0
        if newest is not None and system_indexes:
            reserved = min(content_chars(sanitized[newest].get("content")), remaining // 2)
        for index in priority:
            message = dict(sanitized[index])
            size = content_chars(message.get("content"))
            if index == newest:
                reserved = 0
            available = remaining - reserved
            if size <= available:
                selected[index] = message
                remaining -= size
            elif available >= self.limits.min_retained_chars:
                message["content"] = _fold_text(str(message.get("content") or ""), available)
                selected[index] = message
                stats["folded_fields"] += 1
                remaining -= available

        compacted = [selected[index] for index in sorted(selected)]
        stats["dropped_messages"] += len(sanitized) - len(selected)
        stats["output_chars"] = message_chars(compacted)
        stats["changed"] = True
        return compacted, stats

    def fit(self, provider: Provider, body: dict) -> dict:
        """Return the request body sized for this provider.

        A provider's `max_prompt_chars` can only tighten the router-wide guard, which has
        already run, so a provider without one gets the body as is.
        """
        messages = body.get("messages") or []
        limit = provider.max_prompt_chars
        if not limit or message_chars(messages) <= limit:
            return body
        compacted, stats = self.compact(messages, limit)
        self._log(
            f"compacted prompt for tier {provider.name} chars={stats['input_chars']}->"
            f"{stats['output_chars']} dropped_messages={stats['dropped_messages']} "
            f"folded_fields={stats['folded_fields']}"
        )
        return {**body, "messages": compacted}

    def record(self, stats: dict) -> None:
        """Count a changed request once; an unchanged one is not counted."""
        if not stats["changed"]:
            return
        with self._lock:
            self._totals["compacted_requests"] += 1
            for key in ("input_chars", "output_chars", "dropped_messages",
                        "removed_image_parts", "removed_blobs"):
                self._totals[key] += stats[key]

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "max_prompt_chars": self.limits.max_prompt_chars,
                "max_field_chars": self.limits.max_field_chars,
                **self._totals,
            }
