from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


@dataclass
class MusicState:
    url: str = ""
    playing: bool = False
    position_seconds: float = 0.0
    started_at: datetime | None = None
    requested_by: str = ""

    def snapshot(self) -> dict[str, Any]:
        position = self.position_seconds
        if self.playing and self.started_at:
            position += max(0.0, (utc_now() - self.started_at).total_seconds())
        return {
            "url": self.url,
            "playing": self.playing,
            "position_seconds": round(position, 2),
            "requested_by": self.requested_by,
            "started_at": isoformat(self.started_at) if self.started_at else None,
        }


@dataclass
class QueueItem:
    queue_id: int
    url: str
    requested_by: str
    enqueued_at: datetime = field(default_factory=utc_now)

    def snapshot(self) -> dict[str, Any]:
        return {
            "queue_id": self.queue_id,
            "url": self.url,
            "requested_by": self.requested_by,
            "enqueued_at": isoformat(self.enqueued_at),
        }


@dataclass
class BoardState:
    content: str = "Welcome to Scratch House"
    updated_at: datetime = field(default_factory=utc_now)
    updated_by: str = "system"

    def snapshot(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "updated_at": isoformat(self.updated_at),
            "updated_by": self.updated_by,
        }


@dataclass
class UserSession:
    name: str
    joined_at: datetime = field(default_factory=utc_now)
    muted: bool = False
    speaking: bool = False

    def active_seconds(self) -> int:
        return int((utc_now() - self.joined_at).total_seconds())

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "muted": self.muted,
            "speaking": self.speaking,
            "joined_at": isoformat(self.joined_at),
        }
