import asyncio
from typing import cast

import pytest

from tartarus.agent_loop import AgentLoop
from tartarus.background import BackgroundRegistry
from tartarus.broker import Broker
from tartarus.cli import (
    SessionFlags,
    _bundle_manifest_source,
    _parse_agent_selector,
    _parse_session_flags,
    _print_context_status,
    _run_one_shot,
)
from tartarus.config import ConfigError
from tartarus.jail import JailBuilder
from tartarus.policy import PolicyEngine
from tartarus.provider.base import Provider
from tests.manifest_fixtures import echo_manifest


def test_parse_agent_selector_picks_named_agent():
    agent_name, rest = _parse_agent_selector([".#research", "hello", "world"])
    assert agent_name == "research"
    assert rest == ["hello", "world"]


def test_parse_agent_selector_accepts_uppercase_names():
    agent_name, rest = _parse_agent_selector([".#readOnly"])
    assert agent_name == "readOnly"
    assert rest == []


def test_parse_agent_selector_leaves_plain_prompt_unchanged():
    agent_name, rest = _parse_agent_selector(["count", "lines", "in", "README.md"])
    assert agent_name is None
    assert rest == ["count", "lines", "in", "README.md"]


def test_bundle_manifest_source_names_realized_bundle_path():
    assert (
        _bundle_manifest_source("/nix/store/abc-agent-bundle")
        == "/nix/store/abc-agent-bundle/manifest.json"
    )


def test_parse_agent_selector_rejects_empty_name():
    with pytest.raises(ConfigError, match="missing a name"):
        _parse_agent_selector([".#"])


def test_parse_session_flags_extracts_continue_and_leaves_prompt():
    flags, rest = _parse_session_flags(["--continue", "what", "did", "I", "ask"])
    assert flags.continue_latest is True
    assert rest == ["what", "did", "I", "ask"]


def test_parse_session_flags_extracts_resume_id():
    flags, rest = _parse_session_flags([".#default", "--resume", "20260627-1200", "go"])
    assert flags.resume == "20260627-1200"
    # The selector and prompt survive untouched for downstream parsing.
    assert rest == [".#default", "go"]


def test_parse_session_flags_accepts_resume_equals_form():
    flags, rest = _parse_session_flags(["--resume=abc123"])
    assert flags.resume == "abc123"
    assert rest == []


def test_parse_session_flags_no_session_and_list():
    flags, _ = _parse_session_flags(["--no-session"])
    assert flags.disabled is True
    flags, _ = _parse_session_flags(["--list-sessions"])
    assert flags.list_sessions is True


def test_parse_session_flags_context_commands():
    flags, _ = _parse_session_flags(["--context-status"])
    assert flags.context_status is True
    flags, _ = _parse_session_flags(["--compact-context"])
    assert flags.compact_context is True


def test_parse_session_flags_resume_without_id_errors():
    with pytest.raises(ConfigError, match="--resume requires"):
        _parse_session_flags(["--resume"])


def test_parse_session_flags_default_is_inert():
    flags, rest = _parse_session_flags(["just", "a", "prompt"])
    assert flags == SessionFlags()
    assert rest == ["just", "a", "prompt"]


def test_print_session_list_empty_dir(tmp_path, capsys):
    from tartarus.cli import _print_session_list

    _print_session_list(str(tmp_path))
    captured = capsys.readouterr()
    assert "no sessions" in captured.err
    assert captured.out == ""


def test_print_session_list_ignores_unreadable_session(tmp_path, capsys):
    from tartarus.cli import _print_session_list
    from tartarus.session import SessionStore

    SessionStore(str(tmp_path), "good").append([{"role": "user", "content": "keep me"}])
    bad = SessionStore(str(tmp_path), "bad")
    bad.append([{"role": "user", "content": "x"}])
    bad_path = tmp_path / "bad.jsonl"
    bad_path.write_text("not valid json\n")

    _print_session_list(str(tmp_path))
    captured = capsys.readouterr()
    assert "good" in captured.out
    assert "keep me" in captured.out
    assert "warning: could not preview session bad" in captured.err


def test_print_session_list_reports_unreadable_dir(tmp_path, capsys):
    from tartarus.cli import _print_session_list

    session_dir = tmp_path / "sessions"
    session_dir.write_text("not a directory")
    _print_session_list(str(session_dir))
    captured = capsys.readouterr()
    assert "warning: could not list sessions" in captured.err


def test_print_context_status_uses_latest_session_without_api_key(
    tmp_path, monkeypatch, capsys
):
    from tartarus.session import SessionStore

    monkeypatch.setenv("TARTARUS_WORK_TREE", str(tmp_path))
    session_dir = tmp_path / ".tartarus" / "sessions"
    SessionStore(str(session_dir), "s1").append(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )

    result = _print_context_status(SessionFlags())

    captured = capsys.readouterr()
    assert result == 0
    assert "session: s1" in captured.out
    assert "messages: 2" in captured.out
    assert "ledger events: 0" in captured.out


def test_run_one_shot_returns_one_when_background_reaction_fails(monkeypatch):
    """A provider-level failure while reacting to a completion yields exit code 1."""

    state = {"running": True}

    class FakeRegistry:
        @property
        def has_running(self) -> bool:
            return state["running"]

    async def fake_send(_loop, _messages, _text):
        return True

    async def fake_drain(_loop, _messages, _store, _ledger, _notices):
        state["running"] = False
        return False

    monkeypatch.setattr("tartarus.cli._send", fake_send)
    monkeypatch.setattr("tartarus.cli._drain_notice", fake_drain)

    loop = AgentLoop(
        provider=cast(Provider, None),
        broker=Broker(echo_manifest(), cast(JailBuilder, None), PolicyEngine()),
        manifest=echo_manifest(),
        system_prompt="test",
    )

    result = asyncio.run(
        _run_one_shot(
            loop,
            "prompt",
            [],
            None,
            None,
            cast(BackgroundRegistry, FakeRegistry()),
            asyncio.Queue(),
        )
    )

    assert result == 1
