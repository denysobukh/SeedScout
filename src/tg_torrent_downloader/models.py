"""Immutable snapshots used by the search and submission workflow."""

from dataclasses import dataclass
from enum import StrEnum


class SelectionStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    SUBMITTING = "submitting"
    UNCERTAIN = "uncertain"
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class TorrentResult:
    topic_id: int
    title: str
    size: str | None = None
    seeds: int | None = None
    quality: str | None = None
    language: str | None = None
    format: str | None = None

    def __post_init__(self) -> None:
        if type(self.topic_id) is not int or self.topic_id <= 0:
            raise ValueError("topic_id must be a positive integer")
        if not self.title.strip():
            raise ValueError("title must not be empty")
        if self.seeds is not None and (type(self.seeds) is not int or self.seeds < 0):
            raise ValueError("seeds must be a nonnegative integer")

    @property
    def topic_url(self) -> str:
        return f"https://rutracker.org/forum/viewtopic.php?t={self.topic_id}"


@dataclass(frozen=True)
class Search:
    id: str
    user_id: int
    chat_id: int
    query: str
    results: tuple[TorrentResult, ...]
    created_at: float
    expires_at: float


@dataclass(frozen=True)
class Selection:
    id: str
    search_id: str
    position: int
    user_id: int
    chat_id: int
    result: TorrentResult
    status: SelectionStatus
    created_at: float
    expires_at: float
    updated_at: float
    claim_token: str | None
    info_hash: str | None
    transmission_id: int | None
    transmission_name: str | None


@dataclass(frozen=True, repr=False)
class RuTrackerCredentials:
    username: str
    password: str

    def __post_init__(self) -> None:
        if not self.username.strip() or not self.password.strip():
            raise ValueError("Login and password must be nonempty")
