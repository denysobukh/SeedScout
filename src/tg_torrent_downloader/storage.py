"""Durable workflow state. Call synchronous operations via asyncio.to_thread in handlers.

Each operation owns its connection and transaction. No connections cross thread boundaries.
Only claim_confirmation authorizes torrent retrieval. begin_submission must commit before RPC.
"""

import fcntl
import json
import math
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from tg_torrent_downloader.logging import log_state
from tg_torrent_downloader.models import (
    RuTrackerCredentials,
    Search,
    Selection,
    SelectionStatus,
    TorrentResult,
)


class StateError(ValueError):
    """The requested transition is forbidden or the state is unavailable."""


class UnresolvedTorrent(StateError):
    """A prior submission of this hash still has an unresolved outcome."""


class Unavailable(StateError):
    """Unknown ID or wrong user/chat; intentionally indistinguishable."""


class Expired(StateError):
    """The search or pending selection has expired."""


class InvalidTransition(StateError):
    """The operation has already been claimed or cannot change state."""


_SCHEMA = (
    """CREATE TABLE searches (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
        query TEXT NOT NULL, results TEXT NOT NULL,
        created_at REAL NOT NULL, expires_at REAL NOT NULL CHECK(expires_at > created_at)
    )""",
    """CREATE TABLE selections (
        id TEXT PRIMARY KEY, search_id TEXT NOT NULL REFERENCES searches(id),
        position INTEGER NOT NULL CHECK(position >= 0),
        user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL, result TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN
            ('pending','claimed','submitting','uncertain','accepted','duplicate','found','failed','cancelled')),
        created_at REAL NOT NULL, expires_at REAL NOT NULL, updated_at REAL NOT NULL,
        claim_token TEXT, info_hash TEXT, transmission_id INTEGER, transmission_name TEXT,
        UNIQUE(search_id, position),
        CHECK(status NOT IN ('claimed','submitting','uncertain','accepted','duplicate','found')
            OR claim_token IS NOT NULL),
        CHECK(status NOT IN ('submitting','uncertain','accepted','duplicate','found')
            OR info_hash IS NOT NULL)
    )""",
    "CREATE INDEX selections_status ON selections(status)",
)


class WorkflowStore:
    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time) -> None:
        self.path = Path(path)
        self.clock = clock

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create schema once; never silently downgrade a newer database."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create privately before SQLite can write credentials; tighten existing files too.
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self._transaction() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for statement in _SCHEMA:
                    db.execute(statement)
                version = 1
            if version == 1:
                db.execute("""CREATE TABLE rutracker_credentials (
                    user_id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                    password TEXT NOT NULL, validated_at REAL NOT NULL
                )""")
                version = 2
            if version == 2:
                db.execute(
                    _SCHEMA[1].replace(
                        "CREATE TABLE selections (", "CREATE TABLE selections_v3 (", 1
                    )
                )
                db.execute("INSERT INTO selections_v3 SELECT * FROM selections")
                db.execute("DROP TABLE selections")
                db.execute("ALTER TABLE selections_v3 RENAME TO selections")
                db.execute(_SCHEMA[2])
                db.execute("CREATE INDEX selections_hash_status ON selections(info_hash,status)")
                db.execute("PRAGMA user_version = 3")
            elif version != 3:
                raise StateError("Unsupported workflow database schema version")

    @contextmanager
    def runtime_lock(self) -> Iterator[None]:
        """Keep a single bot worker/recovery process per database (POSIX host)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        path = self.path.with_name(self.path.name + ".runtime.lock")
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise StateError("Another bot process is using this workflow database") from None
            yield
        finally:
            os.close(descriptor)

    def history(self, user_id: int, chat_id: int, *, limit: int = 10) -> tuple[Selection, ...]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("History limit must be between 1 and 100")
        with self._transaction() as db:
            rows = db.execute(
                """SELECT * FROM selections WHERE user_id=? AND chat_id=?
                AND (status IN ('claimed','submitting','uncertain') OR id IN (
                    SELECT id FROM selections WHERE user_id=? AND chat_id=?
                    AND status IN ('accepted','duplicate','found','failed')
                    ORDER BY updated_at DESC,rowid DESC LIMIT ?
                )) ORDER BY updated_at DESC,rowid DESC""",
                (user_id, chat_id, user_id, chat_id, limit),
            ).fetchall()
            return tuple(self._selection(row) for row in rows)

    def get_credentials(self, user_id: int) -> RuTrackerCredentials | None:
        with self._transaction() as db:
            row = db.execute(
                "SELECT username, password FROM rutracker_credentials WHERE user_id=?", (user_id,)
            ).fetchone()
            return RuTrackerCredentials(**dict(row)) if row else None

    def save_credentials(self, user_id: int, credentials: RuTrackerCredentials) -> None:
        """Persist a pair only after the caller has confirmed authentication."""
        if type(user_id) is not int or not 0 < user_id < 2**52:
            raise ValueError("user_id must be a positive Telegram ID")
        with self._transaction() as db:
            db.execute(
                """INSERT INTO rutracker_credentials VALUES (?,?,?,?)
                ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
                password=excluded.password, validated_at=excluded.validated_at""",
                (user_id, credentials.username, credentials.password, self.clock()),
            )

    def forget_credentials(self, user_id: int, credentials: RuTrackerCredentials) -> None:
        """Remove a rejected pair without deleting newer credentials from another chat."""
        with self._transaction() as db:
            db.execute(
                "DELETE FROM rutracker_credentials WHERE user_id=? AND username=? AND password=?",
                (user_id, credentials.username, credentials.password),
            )

    @staticmethod
    def _owned(row: sqlite3.Row | None, user_id: int, chat_id: int) -> sqlite3.Row:
        if row is None or row["user_id"] != user_id or row["chat_id"] != chat_id:
            raise Unavailable("State is unavailable for this user and chat")
        return row

    def _active(self, row: sqlite3.Row) -> None:
        if row["expires_at"] <= self.clock():
            raise Expired("Selection expired; search again")

    @staticmethod
    def _search(row: sqlite3.Row) -> Search:
        data = dict(row)
        data["results"] = tuple(TorrentResult(**item) for item in json.loads(data["results"]))
        return Search(**data)

    @staticmethod
    def _selection(row: sqlite3.Row) -> Selection:
        data = dict(row)
        data["result"] = TorrentResult(**json.loads(data["result"]))
        data["status"] = SelectionStatus(data["status"])
        return Selection(**data)

    def save_search(
        self,
        user_id: int,
        chat_id: int,
        query: str,
        results: Sequence[TorrentResult],
        *,
        ttl_seconds: float = 900,
    ) -> Search:
        if not query.strip() or not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("A query and a finite positive TTL are required")
        if type(user_id) is not int or not 0 < user_id < 2**52:
            raise ValueError("user_id must be a positive Telegram ID")
        if type(chat_id) is not int or chat_id == 0 or abs(chat_id) >= 2**52:
            raise ValueError("chat_id must be a nonzero Telegram chat ID")
        now = self.clock()
        search = Search(
            uuid4().hex, user_id, chat_id, query, tuple(results), now, now + ttl_seconds
        )
        with self._transaction() as db:
            db.execute(
                "INSERT INTO searches VALUES (?,?,?,?,?,?,?)",
                (
                    search.id,
                    user_id,
                    chat_id,
                    query,
                    json.dumps([asdict(result) for result in search.results]),
                    now,
                    search.expires_at,
                ),
            )
        return search

    def get_search(self, search_id: str, user_id: int, chat_id: int) -> Search:
        with self._transaction() as db:
            row = self._owned(
                db.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone(),
                user_id,
                chat_id,
            )
            self._active(row)
            return self._search(row)

    def select(self, search_id: str, position: int, user_id: int, chat_id: int) -> Selection:
        with self._transaction() as db:
            row = self._owned(
                db.execute("SELECT * FROM searches WHERE id=?", (search_id,)).fetchone(),
                user_id,
                chat_id,
            )
            self._active(row)
            search = self._search(row)
            if type(position) is not int or not 0 <= position < len(search.results):
                raise Unavailable("Result is unavailable")
            now = self.clock()
            db.execute(
                """INSERT INTO selections
                (id,search_id,position,user_id,chat_id,result,status,created_at,expires_at,updated_at)
                VALUES (?,?,?,?,?,?,'pending',?,?,?)
                ON CONFLICT(search_id,position) DO NOTHING""",
                (
                    uuid4().hex,
                    search_id,
                    position,
                    user_id,
                    chat_id,
                    json.dumps(asdict(search.results[position])),
                    now,
                    search.expires_at,
                    now,
                ),
            )
            selection = self._selection(
                db.execute(
                    "SELECT * FROM selections WHERE search_id=? AND position=?",
                    (search_id, position),
                ).fetchone()
            )
        log_state(
            "selection",
            user_id=selection.user_id,
            chat_id=selection.chat_id,
            topic_id=selection.result.topic_id,
            selection_id=selection.id,
            submission_status=selection.status,
        )
        return selection

    def get_selection(self, selection_id: str, user_id: int, chat_id: int) -> Selection:
        """Return owned history, including expired selections, without authorizing work."""
        with self._transaction() as db:
            return self._selection(
                self._owned(
                    db.execute("SELECT * FROM selections WHERE id=?", (selection_id,)).fetchone(),
                    user_id,
                    chat_id,
                )
            )

    def _user_transition(
        self,
        selection_id: str,
        user_id: int,
        chat_id: int,
        *,
        cancel: bool,
        claim_token: str | None = None,
    ) -> Selection:
        with self._transaction() as db:
            row = self._owned(
                db.execute("SELECT * FROM selections WHERE id=?", (selection_id,)).fetchone(),
                user_id,
                chat_id,
            )
            self._active(row)
            if row["status"] != SelectionStatus.PENDING:
                raise InvalidTransition("Selection is no longer pending")
            db.execute(
                "UPDATE selections SET status=?,claim_token=?,updated_at=? WHERE id=?",
                (
                    SelectionStatus.CANCELLED if cancel else SelectionStatus.CLAIMED,
                    None if cancel else (claim_token or uuid4().hex),
                    self.clock(),
                    selection_id,
                ),
            )
            selection = self._selection(
                db.execute("SELECT * FROM selections WHERE id=?", (selection_id,)).fetchone()
            )
        log_state(
            "selection",
            user_id=selection.user_id,
            chat_id=selection.chat_id,
            topic_id=selection.result.topic_id,
            selection_id=selection.id,
            submission_status=selection.status,
        )
        return selection

    def claim_confirmation(
        self, selection_id: str, user_id: int, chat_id: int, *, claim_token: str | None = None
    ) -> Selection:
        if claim_token is not None and not re.fullmatch(r"[0-9a-f]{32}", claim_token):
            raise ValueError("Worker claim token is invalid")
        return self._user_transition(
            selection_id, user_id, chat_id, cancel=False, claim_token=claim_token
        )

    def cancel(self, selection_id: str, user_id: int, chat_id: int) -> Selection:
        return self._user_transition(selection_id, user_id, chat_id, cancel=True)

    def _worker_transition(
        self,
        selection_id: str,
        claim_token: str,
        expected: tuple[SelectionStatus, ...],
        target: SelectionStatus,
        *,
        info_hash: str | None = None,
        transmission_id: int | None = None,
        transmission_name: str | None = None,
    ) -> Selection:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM selections WHERE id=?", (selection_id,)).fetchone()
            if (
                row is None
                or not claim_token
                or row["claim_token"] != claim_token
                or row["status"] not in expected
            ):
                raise InvalidTransition("Worker claim or state is invalid")
            if (
                target == SelectionStatus.SUBMITTING
                and db.execute(
                    """SELECT 1 FROM selections WHERE info_hash=? AND id<>?
                AND status IN ('submitting','uncertain') LIMIT 1""",
                    (info_hash, selection_id),
                ).fetchone()
            ):
                raise UnresolvedTorrent("An earlier submission of this torrent is still unresolved")
            db.execute(
                """UPDATE selections SET status=?,info_hash=?,transmission_id=?,
                transmission_name=?,updated_at=? WHERE id=?""",
                (
                    target,
                    info_hash or row["info_hash"],
                    transmission_id,
                    transmission_name,
                    self.clock(),
                    selection_id,
                ),
            )
            selection = self._selection(
                db.execute("SELECT * FROM selections WHERE id=?", (selection_id,)).fetchone()
            )
        log_state(
            "selection",
            user_id=selection.user_id,
            chat_id=selection.chat_id,
            topic_id=selection.result.topic_id,
            selection_id=selection.id,
            submission_status=selection.status,
        )
        return selection

    def begin_submission(self, selection_id: str, claim_token: str, info_hash: str) -> Selection:
        """Persist identity before add RPC, after retrieving and validating torrent bytes."""
        if not re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", info_hash):
            raise ValueError("info_hash must be a hexadecimal torrent hash")
        return self._worker_transition(
            selection_id,
            claim_token,
            (SelectionStatus.CLAIMED,),
            SelectionStatus.SUBMITTING,
            info_hash=info_hash.lower(),
        )

    def mark_uncertain(self, selection_id: str, claim_token: str) -> Selection:
        return self._worker_transition(
            selection_id, claim_token, (SelectionStatus.SUBMITTING,), SelectionStatus.UNCERTAIN
        )

    def finish(
        self,
        selection_id: str,
        claim_token: str,
        *,
        duplicate: bool,
        transmission_id: int,
        transmission_name: str,
        found: bool = False,
    ) -> Selection:
        if duplicate and found:
            raise ValueError("A reconciled result cannot claim a known duplicate outcome")
        if type(transmission_id) is not int or transmission_id < 0 or not transmission_name.strip():
            raise ValueError("A Transmission ID and name are required")
        return self._worker_transition(
            selection_id,
            claim_token,
            (SelectionStatus.SUBMITTING, SelectionStatus.UNCERTAIN),
            SelectionStatus.FOUND
            if found
            else (SelectionStatus.DUPLICATE if duplicate else SelectionStatus.ACCEPTED),
            transmission_id=transmission_id,
            transmission_name=transmission_name,
        )

    def fail(self, selection_id: str, claim_token: str) -> Selection:
        """Only for a known failure; a missing immediate RPC lookup is not proof of failure.

        A failed selection cannot be reclaimed; a fresh search/confirmation is required.
        Do not store raw upstream errors here, since they may contain credentials.
        """
        return self._worker_transition(
            selection_id,
            claim_token,
            (SelectionStatus.CLAIMED, SelectionStatus.SUBMITTING, SelectionStatus.UNCERTAIN),
            SelectionStatus.FAILED,
        )

    def list_incomplete(self) -> tuple[Selection, ...]:
        """Internal recovery API, not user-facing. Never resets or reclaims interrupted work.

        Once prior workers have stopped, step 6 can fail claimed work (no add RPC yet),
        and look up submitting/uncertain hashes in Transmission before deciding the outcome.
        """
        with self._transaction() as db:
            rows = db.execute("""SELECT * FROM selections
                WHERE status IN ('claimed','submitting','uncertain')
                ORDER BY created_at,id""").fetchall()
            return tuple(self._selection(row) for row in rows)
