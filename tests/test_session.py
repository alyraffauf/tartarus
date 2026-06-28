import re

import pytest

from tartarus.session import SessionError, SessionStore


def test_new_id_is_sortable_and_unique():
    ids = sorted(SessionStore.new_id() for _ in range(50))
    # Shape: YYYYMMDD-HHMMSS-ffffff-xxxxxxxx
    assert all(re.fullmatch(r"\d{8}-\d{6}-\d{6}-[0-9a-f]{8}", i) for i in ids)
    assert len(set(ids)) == len(ids)


def test_append_then_load_round_trips(tmp_path):
    store = SessionStore(str(tmp_path), "20260627-120000-aaaa")
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    store.append(messages)

    reopened = SessionStore(str(tmp_path), "20260627-120000-aaaa")
    assert reopened.load() == messages


def test_append_is_incremental_and_does_not_duplicate(tmp_path):
    store = SessionStore(str(tmp_path), "s1")
    store.append([{"role": "user", "content": "one"}])
    store.append(
        [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    )

    assert SessionStore(str(tmp_path), "s1").load() == [
        {"role": "user", "content": "one"},
        {"role": "assistant", "content": "two"},
    ]


def test_load_after_resume_marks_existing_as_flushed(tmp_path):
    SessionStore(str(tmp_path), "s1").append([{"role": "user", "content": "old"}])

    resumed = SessionStore(str(tmp_path), "s1")
    messages = resumed.load()
    messages.append({"role": "assistant", "content": "new"})
    resumed.append(messages)

    # Only the new message was written; the old one is not duplicated.
    assert SessionStore(str(tmp_path), "s1").load() == [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "new"},
    ]


def test_load_missing_session_returns_empty(tmp_path):
    assert SessionStore(str(tmp_path), "nope").load() == []


def test_append_nothing_writes_no_file(tmp_path):
    store = SessionStore(str(tmp_path), "empty")
    store.append([])
    assert not (tmp_path / "empty.jsonl").exists()


def test_latest_and_list_ids_order_newest_first(tmp_path):
    for session_id in ("20260101-000000-aaaa", "20260627-120000-bbbb"):
        SessionStore(str(tmp_path), session_id).append(
            [{"role": "user", "content": "x"}]
        )

    assert SessionStore.latest(str(tmp_path)) == "20260627-120000-bbbb"
    assert SessionStore.list_ids(str(tmp_path)) == [
        "20260627-120000-bbbb",
        "20260101-000000-aaaa",
    ]


def test_latest_and_list_ids_on_missing_dir(tmp_path):
    missing = str(tmp_path / "nope")
    assert SessionStore.latest(missing) is None
    assert SessionStore.list_ids(missing) == []


def test_resolve_matches_exact_then_unique_prefix(tmp_path):
    for session_id in ("20260627-120000-aaaa", "20260627-130000-bbbb"):
        SessionStore(str(tmp_path), session_id).append(
            [{"role": "user", "content": "x"}]
        )

    assert (
        SessionStore.resolve(str(tmp_path), "20260627-120000-aaaa")
        == "20260627-120000-aaaa"
    )
    # Unambiguous prefix resolves.
    assert SessionStore.resolve(str(tmp_path), "20260627-13") == "20260627-130000-bbbb"


def test_resolve_rejects_missing_and_ambiguous(tmp_path):
    for session_id in ("20260627-120000-aaaa", "20260627-130000-bbbb"):
        SessionStore(str(tmp_path), session_id).append(
            [{"role": "user", "content": "x"}]
        )

    with pytest.raises(SessionError, match="no session"):
        SessionStore.resolve(str(tmp_path), "nope")
    with pytest.raises(SessionError, match="ambiguous"):
        SessionStore.resolve(str(tmp_path), "20260627")


def test_list_ids_surfaces_os_errors_as_session_error(tmp_path):
    bad_path = str(tmp_path / "not_a_dir")
    (tmp_path / "not_a_dir").write_text("i am a file, not a directory")
    with pytest.raises(SessionError, match="cannot list sessions"):
        SessionStore.list_ids(bad_path)


def test_new_id_order_is_chronological():
    ids = [SessionStore.new_id() for _ in range(10)]
    assert ids == sorted(ids)


def test_first_user_message_preview(tmp_path):
    store = SessionStore(str(tmp_path), "s1")
    store.append(
        [
            {"role": "user", "content": "summarize the repo"},
            {"role": "assistant", "content": "sure"},
        ]
    )
    assert (
        SessionStore(str(tmp_path), "s1").first_user_message() == "summarize the repo"
    )


def test_first_user_message_returns_none_for_assistant_only(tmp_path):
    store = SessionStore(str(tmp_path), "s1")
    store.append([{"role": "assistant", "content": "hello"}])

    assert SessionStore(str(tmp_path), "s1").first_user_message() is None


def test_list_ids_ignores_non_jsonl_files(tmp_path):
    for name in ("a.jsonl", "b.txt", "c.log"):
        (tmp_path / name).write_text("{}")

    ids = SessionStore.list_ids(str(tmp_path))
    assert ids == ["a"]
