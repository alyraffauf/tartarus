import asyncio
from typing import cast

import pytest
from typer.testing import CliRunner

from tartarus.agent_loop import AgentLoop
from tartarus.background import BackgroundRegistry
from tartarus.broker import Broker
from tartarus.cli import (
    SessionFlags,
    _bundle_manifest_source,
    _parse_agent_selector,
    _persist_and_compact,
    _print_context_status,
    _run_one_shot,
    app,
    main,
)
from tartarus.config import ConfigError
from tartarus.context import ContextLedger, ContextLimits, ContextManager
from tartarus.jail import JailBuilder
from tartarus.policy import PolicyEngine
from tartarus.provider.base import Provider
from tartarus.session import SessionStore
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


@pytest.fixture
def cli_calls(monkeypatch):
    calls = []

    async def fake_main(argv, flags):
        calls.append((argv, flags))
        return 0

    monkeypatch.setattr("tartarus.cli._async_main", fake_main)
    return calls


@pytest.mark.parametrize(
    ("argv", "expected_prompt", "expected_flags"),
    [
        ([], [], SessionFlags()),
        ([".#research"], [".#research"], SessionFlags()),
        (["just", "a", "prompt"], ["just", "a", "prompt"], SessionFlags()),
        (
            ["--continue", "what", "did", "I", "ask"],
            ["what", "did", "I", "ask"],
            SessionFlags(continue_latest=True),
        ),
        (
            [".#default", "--resume", "20260627-1200", "go"],
            [".#default", "go"],
            SessionFlags(resume="20260627-1200"),
        ),
        (["--resume=abc123"], [], SessionFlags(resume="abc123")),
        (["--no-session"], [], SessionFlags(disabled=True)),
        (["--list-sessions"], [], SessionFlags(list_sessions=True)),
        (["--context-status"], [], SessionFlags(context_status=True)),
        (["--compact-context"], [], SessionFlags(compact_context=True)),
        (
            [".#default", "inspect", "--no-session", "README.md"],
            [".#default", "inspect", "README.md"],
            SessionFlags(disabled=True),
        ),
        (
            ["--", ".#research", "explain", "--continue", "--verbose"],
            [".#research", "explain", "--continue", "--verbose"],
            SessionFlags(),
        ),
        (["explain --verbose"], ["explain --verbose"], SessionFlags()),
    ],
)
def test_cli_parses_options_and_prompt(
    argv, expected_prompt, expected_flags, cli_calls
):
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 0, result.output
    assert cli_calls == [(expected_prompt, expected_flags)]


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["--resume"], "requires an argument"),
        (["--no-sesion"], "No such option"),
    ],
)
def test_cli_rejects_invalid_options_before_startup(argv, message, cli_calls):
    result = CliRunner().invoke(app, argv)

    assert result.exit_code == 2
    assert message in result.output
    assert cli_calls == []


def test_cli_help_does_not_start_runtime(cli_calls):
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    for option in (
        "--resume",
        "--continue",
        "--no-session",
        "--list-sessions",
        "--context-status",
        "--compact-context",
    ):
        assert option in result.output
    assert ".#AGENT" in result.output
    assert cli_calls == []


@pytest.mark.parametrize("exit_code", [0, 1])
def test_main_propagates_runtime_exit_code(exit_code, monkeypatch):
    async def fake_main(argv, flags):
        return exit_code

    monkeypatch.setattr("tartarus.cli._async_main", fake_main)
    with pytest.raises(SystemExit) as error:
        main([])
    assert error.value.code == exit_code


@pytest.mark.parametrize(
    ("option", "expected_output"),
    [
        ("--list-sessions", "s1  hi"),
        ("--context-status", "messages: 2"),
        ("--compact-context", "compaction: nothing to compact"),
    ],
)
def test_cli_inspects_sessions_without_loading_config(
    option, expected_output, tmp_path, monkeypatch
):
    monkeypatch.setenv("TARTARUS_WORK_TREE", str(tmp_path))

    def unexpected_load_config():
        pytest.fail("session inspection must not load API configuration")

    monkeypatch.setattr("tartarus.cli.load_config", unexpected_load_config)
    SessionStore(str(tmp_path / ".tartarus" / "sessions"), "s1").append(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
    )

    result = CliRunner().invoke(app, [option])

    assert result.exit_code == 0, result.output
    assert expected_output in result.stdout


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


def test_persist_and_compact_writes_summary_after_transcript_events(tmp_path):
    store = SessionStore(str(tmp_path / "sessions"), "s1")
    ledger = ContextLedger(str(tmp_path / "context"), "s1")
    manager = ContextManager(
        ledger,
        ContextLimits(max_chars=500, recent_turns=1),
        auto_compact=True,
    )
    loop = AgentLoop(
        provider=cast(Provider, None),
        broker=Broker(echo_manifest(), cast(JailBuilder, None), PolicyEngine()),
        manifest=echo_manifest(),
        system_prompt="test",
        context_manager=manager,
    )
    messages = []
    for index in range(5):
        messages.append({"role": "user", "content": f"question {index} " + "x" * 200})
        messages.append(
            {"role": "assistant", "content": f"answer {index} " + "y" * 200}
        )

    _persist_and_compact(loop, store, ledger, messages)

    events = ledger.load_events()
    assert events[-1]["type"] == "context_summary"
    assert [event["message_index"] for event in events[:-1]] == list(range(10))
    assert events[-1]["covered"] == {"start": 0, "end": 8}


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
