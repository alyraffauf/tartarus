"""Append-only JSONL persistence for conversation transcripts (PLAN.md §10).

A session is the provider-native `messages` list the AgentLoop maintains. The loop
only ever appends to it, and only at whole-round-trip boundaries, so a session
file is always a valid transcript: one JSON message per line, in order. Modeled on
`FileAuditLog` (audit.py) — makedirs on write, OSError surfaced as a typed error.
"""

import json
import os
import secrets
import time

SESSION_SUFFIX = ".jsonl"

# Bytes of random entropy appended to the timestamp in new_id().
# 8 bytes makes accidental collisions astronomically unlikely while keeping the
# id short enough to type/see in a file listing.
_ID_RANDOM_BYTES = 4


class SessionError(Exception):
    """Raised when a session cannot be read or written."""


class SessionStore:
    def __init__(self, session_dir: str, session_id: str):
        self._dir = os.path.abspath(session_dir)
        self.session_id = session_id
        self._path = os.path.join(self._dir, session_id + SESSION_SUFFIX)
        # Number of messages already on disk; the next append writes the tail
        # past this index so re-appends never duplicate.
        self._flushed = 0

    @property
    def path(self) -> str:
        return self._path

    @staticmethod
    def new_id() -> str:
        """A sortable, collision-resistant, typeable id.

        Timestamp prefix (down to microseconds) means lexical order equals
        chronological order, so `latest` is a plain max. The random suffix
        avoids extremely rare same-microsecond clashes.
        """
        now = time.time()
        secs = int(now)
        usecs = int((now - secs) * 1_000_000)
        timestamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(secs))
        return f"{timestamp}-{usecs:06d}-{secrets.token_hex(_ID_RANDOM_BYTES)}"

    @staticmethod
    def list_ids(session_dir: str) -> list[str]:
        """Existing session ids, newest first (lexical order == chronological)."""
        try:
            names = os.listdir(session_dir)
        except FileNotFoundError:
            return []
        except OSError as error:
            raise SessionError(
                f"cannot list sessions in {session_dir}: {error}"
            ) from error
        ids = [n[: -len(SESSION_SUFFIX)] for n in names if n.endswith(SESSION_SUFFIX)]
        return sorted(ids, reverse=True)

    @staticmethod
    def latest(session_dir: str) -> str | None:
        """The most recent session id, or None if there are none."""
        ids = SessionStore.list_ids(session_dir)
        return ids[0] if ids else None

    @staticmethod
    def resolve(session_dir: str, wanted: str) -> str:
        """Resolve `wanted` to a session id: exact match, else unique prefix.

        Raises SessionError if nothing matches or a prefix is ambiguous.
        """
        ids = SessionStore.list_ids(session_dir)
        if wanted in ids:
            return wanted
        matches = [i for i in ids if i.startswith(wanted)]
        if not matches:
            raise SessionError(f"no session matching '{wanted}'")
        if len(matches) > 1:
            raise SessionError(f"'{wanted}' is ambiguous: matches {', '.join(matches)}")
        return matches[0]

    def load(self) -> list[dict]:
        """Read the persisted transcript, marking those messages as flushed."""
        try:
            with open(self._path, encoding="utf-8") as session_file:
                messages = [json.loads(line) for line in session_file if line.strip()]
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as error:
            raise SessionError(f"cannot read session {self._path}: {error}") from error
        self._flushed = len(messages)
        return messages

    def append(self, messages: list[dict]) -> None:
        """Persist any messages added since the last flush."""
        tail = messages[self._flushed :]
        if not tail:
            return
        if self._dir:
            os.makedirs(self._dir, exist_ok=True)
        try:
            with open(self._path, "a", encoding="utf-8") as session_file:
                for message in tail:
                    session_file.write(json.dumps(message) + "\n")
        except OSError as error:
            raise SessionError(f"cannot write session {self._path}: {error}") from error
        self._flushed = len(messages)

    def first_user_message(self) -> str | None:
        """First user-authored text in the session, for listing previews."""
        for message in self.load():
            if message.get("role") == "user":
                content = message.get("content")
                if isinstance(content, str):
                    return content
        return None
