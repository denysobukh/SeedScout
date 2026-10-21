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
    FOUND = "found"
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
class SearchBatch:
    results: tuple[TorrentResult, ...]
    offset: int = 0
    next_offset: int | None = None
    total_results: int | None = None

    def __post_init__(self) -> None:
        if self.total_results is not None and (
            type(self.total_results) is not int or self.total_results < 0
        ):
            raise ValueError("Invalid total result count")
        if type(self.offset) is not int or not 0 <= self.offset <= 2**31 - 1:
            raise ValueError("Invalid server search offset")
        if self.next_offset is not None and (
            type(self.next_offset) is not int or not self.offset < self.next_offset <= 2**31 - 1
        ):
            raise ValueError("Invalid next server search offset")


@dataclass(frozen=True)
class SearchSession:
    id: str
    user_id: int
    chat_id: int
    query: str
    results: tuple[TorrentResult, ...]
    next_offset: int | None
    fetched_offsets: tuple[int, ...]
    current_page: int
    created_at: float
    expires_at: float
    total_results: int | None = None

    @property
    def total_pages(self) -> int | None:
        if self.next_offset is None:
            return max(1, (len(self.results) + 9) // 10)
        if self.total_results is not None:
            return max(self.current_page, (self.total_results + 9) // 10)
        return None

    @property
    def has_next(self) -> bool:
        return len(self.results) > self.current_page * 10 or self.next_offset is not None


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
