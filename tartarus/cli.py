"""Interactive command-line entry point for the harness.

Reads a prompt (or argv for one-shot use), streams the agent loop against the
configured OpenAI-compatible backend, and prints the reply as it arrives. Tool
activity is echoed inline so the round-trip is visible. Each turn runs as a
cancellable task: Ctrl-C aborts the in-flight turn back to the prompt without
killing the process.
"""

import asyncio
import signal
import sys
from dataclasses import dataclass

from tartarus.agent_loop import AgentLoop, ToolFinished, ToolStarted
from tartarus.audit import FileAuditLog
from tartarus.background import BackgroundRegistry, Notice
from tartarus.broker import Broker
from tartarus.config import (
    Config,
    ConfigError,
    ResolvedRuntime,
    load_config,
    resolve_runtime,
    session_dir_from_env,
)
from tartarus.bundle import BundleError, base_env_from, load_bundle, resolve_bundle
from tartarus.jail import JailBuilder
from tartarus.manifest_loader import host_system
from tartarus.models import TextDelta, ToolOutputDelta
from tartarus.policy import PolicyEngine
from tartarus.provider.openai_compat import OpenAICompatProvider, ProviderError
from tartarus.session import SessionError, SessionStore


@dataclass
class SessionFlags:
    """How the user wants the session resolved (parsed from argv)."""

    resume: str | None = None  # --resume <id>: reopen this session
    continue_latest: bool = False  # --continue: reopen the most recent
    disabled: bool = False  # --no-session: don't persist
    list_sessions: bool = False  # --list-sessions: print and exit


def _parse_session_flags(argv: list[str]) -> tuple[SessionFlags, list[str]]:
    """Strip session flags from argv, leaving the selector/prompt arguments.

    Hand-parsed (no argparse) to coexist with the `.#agent` selector and the
    freeform one-shot prompt.
    """
    flags = SessionFlags()
    rest: list[str] = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--continue":
            flags.continue_latest = True
        elif arg == "--no-session":
            flags.disabled = True
        elif arg == "--list-sessions":
            flags.list_sessions = True
        elif arg == "--resume":
            if i + 1 >= len(argv):
                raise ConfigError("--resume requires a session id")
            flags.resume = argv[i + 1]
            i += 1
        elif arg.startswith("--resume="):
            flags.resume = arg[len("--resume=") :]
        else:
            rest.append(arg)
        i += 1
    return flags, rest


def _parse_agent_selector(argv: list[str]) -> tuple[str | None, list[str]]:
    """Allow `.#<agent>` as the first positional argument to select a flake agent.

    Returns the overridden agent name and the remaining prompt arguments.
    """
    if argv and argv[0].startswith(".#"):
        agent_name = argv[0][2:]
        if not agent_name:
            raise ConfigError("agent selector '.#' is missing a name")
        return agent_name, argv[1:]
    return None, argv


def _build_provider(runtime: ResolvedRuntime) -> OpenAICompatProvider:
    if runtime.provider_type != "openai-compat":
        raise ConfigError(
            f"unsupported provider '{runtime.provider_type}' "
            "(this harness currently ships only 'openai-compat')"
        )
    return OpenAICompatProvider(
        base_url=runtime.base_url,
        api_key=runtime.api_key,
        model=runtime.model,
        max_tokens=runtime.max_tokens,
        extra_headers=runtime.extra_headers,
        sampling=runtime.sampling,
    )


def _bundle_manifest_source(bundle_path: str) -> str:
    return f"{bundle_path}/manifest.json"


async def _consume_turn(loop: AgentLoop, messages: list[dict]) -> None:
    """Render one turn's events live: text to stdout, tool activity inline."""
    wrote_text = False
    async for event in loop.run_turn(messages):
        if isinstance(event, TextDelta):
            print(event.text, end="", flush=True)
            wrote_text = True
        elif isinstance(event, ToolStarted):
            if wrote_text:
                print()
                wrote_text = False
            print(f"  [tool] {event.call.name}({event.call.arguments}) ...", flush=True)
        elif isinstance(event, ToolOutputDelta):
            print(event.text, end="", flush=True)
        elif isinstance(event, ToolFinished):
            status = "error" if event.result.is_error else "ok"
            print(f"  [tool] {event.call.name} -> {status}", flush=True)
    if wrote_text:
        print()


async def _send(loop: AgentLoop, messages: list[dict], user_text: str) -> bool:
    """Run one turn as a cancellable task so Ctrl-C aborts it, not the process.

    On Ctrl-C the task is cancelled mid-stream; the loop never committed a partial
    assistant message, so `messages` stays a valid transcript for the next turn.
    Returns True if the turn completed, False if it was cancelled — callers use
    this to avoid persisting an abandoned turn.
    """
    messages.append({"role": "user", "content": user_text})
    event_loop = asyncio.get_running_loop()
    task = event_loop.create_task(_consume_turn(loop, messages))
    try:
        event_loop.add_signal_handler(signal.SIGINT, task.cancel)
    except (NotImplementedError, ValueError):
        # No signal handler available (e.g. not the main thread); Ctrl-C falls
        # back to default KeyboardInterrupt behavior.
        pass
    try:
        await task
        return True
    except asyncio.CancelledError:
        print("\n^C (turn cancelled)", file=sys.stderr)
        return False
    finally:
        try:
            event_loop.remove_signal_handler(signal.SIGINT)
        except (NotImplementedError, ValueError):
            pass


def _persist(store: SessionStore | None, messages: list[dict]) -> None:
    """Flush newly committed messages, warning (not failing) on write errors."""
    if store is None:
        return
    try:
        store.append(messages)
    except SessionError as error:
        print(f"warning: could not save session: {error}", file=sys.stderr)


async def _run_one_shot(
    loop: AgentLoop,
    prompt: str,
    messages: list[dict],
    store: SessionStore | None,
    registry: BackgroundRegistry,
    notices: "asyncio.Queue[Notice]",
) -> int:
    try:
        if await _send(loop, messages, prompt):
            _persist(store, messages)
        # A one-shot run that launched background work waits it out, reacting to
        # each completion, so the task is not killed the instant the turn ends.
        failed = False
        while registry.has_running or not notices.empty():
            if not await _drain_notice(loop, messages, store, notices):
                failed = True
        return 1 if failed else 0
    except ProviderError as error:
        print(f"provider error: {error}", file=sys.stderr)
        return 1


async def _run_repl(
    loop: AgentLoop,
    messages: list[dict],
    store: SessionStore | None,
    registry: BackgroundRegistry,
    notices: "asyncio.Queue[Notice]",
) -> int:
    print("Type a message, Ctrl-C to cancel a turn, or Ctrl-D to exit.\n")
    # Read the line in a worker thread so the event loop keeps running while we
    # wait — that lets background monitors fire and a completion notice interrupt
    # an idle prompt. The same input task is reused across notices, so its blocked
    # stdin read is never orphaned.
    input_task: asyncio.Task[str] | None = None
    while True:
        if input_task is None:
            input_task = asyncio.create_task(asyncio.to_thread(_read_line))
        notice_task = asyncio.create_task(notices.get())
        try:
            done, _ = await asyncio.wait(
                {input_task, notice_task}, return_when=asyncio.FIRST_COMPLETED
            )
        except KeyboardInterrupt:
            # Ctrl-C at an idle prompt: clear the line, keep the input task alive.
            notice_task.cancel()
            print()
            continue

        if notice_task in done:
            await _react_to_notice(loop, messages, store, notice_task.result())
            continue

        # notice_task did not win, so the input task is the one that completed.
        notice_task.cancel()
        try:
            user_text = input_task.result()
        except EOFError:
            print()
            return 0
        finally:
            input_task = None
        if not user_text:
            continue
        try:
            if await _send(loop, messages, user_text):
                _persist(store, messages)
        except ProviderError as error:
            print(f"provider error: {error}", file=sys.stderr)


def _read_line() -> str:
    return input("> ").strip()


async def _drain_notice(
    loop: AgentLoop,
    messages: list[dict],
    store: SessionStore | None,
    notices: "asyncio.Queue[Notice]",
) -> bool:
    return await _react_to_notice(loop, messages, store, await notices.get())


async def _react_to_notice(
    loop: AgentLoop,
    messages: list[dict],
    store: SessionStore | None,
    notice: Notice,
) -> bool:
    """Turn one background completion into a transcript message + follow-up turn.

    The launch's tool result was just the handle; completion arrives out of band,
    so it is injected as a user-role message and the model is given a turn to
    react. Turns are never concurrent — this runs only between turns.

    Returns True if the follow-up turn completed, False if it failed or was
    cancelled.
    """
    summary = f" [{notice.network_summary}]" if notice.network_summary else ""
    body = notice.output_tail.strip() or "(no output)"
    text = (
        f"[background] {notice.task_id} ({notice.capability}) finished with "
        f"exit code {notice.exit_code}{summary}:\n{body}"
    )
    print(f"\n  [background] {notice.task_id} finished (exit {notice.exit_code})")
    try:
        if await _send(loop, messages, text):
            _persist(store, messages)
            return True
        return False
    except ProviderError as error:
        print(f"provider error: {error}", file=sys.stderr)
        return False


def _print_session_list(session_dir: str) -> None:
    try:
        ids = SessionStore.list_ids(session_dir)
    except SessionError as error:
        print(f"warning: could not list sessions: {error}", file=sys.stderr)
        return
    if not ids:
        print(f"no sessions in {session_dir}", file=sys.stderr)
        return
    for session_id in ids:
        preview = ""
        try:
            preview = SessionStore(session_dir, session_id).first_user_message() or ""
        except SessionError as error:
            print(
                f"warning: could not preview session {session_id}: {error}",
                file=sys.stderr,
            )
        preview = preview.replace("\n", " ")
        if len(preview) > 70:
            preview = preview[:67] + "..."
        print(f"{session_id}  {preview}")


def _open_session(
    config: Config, flags: SessionFlags
) -> tuple[SessionStore | None, list[dict]]:
    """Resolve flags to a (store, seed messages) pair.

    --no-session → (None, []); --resume/--continue → an existing session loaded
    into messages; otherwise a fresh session with a new id.
    """
    if flags.disabled:
        return None, []

    if flags.resume is not None:
        session_id = SessionStore.resolve(config.session_dir, flags.resume)
    elif flags.continue_latest:
        session_id = SessionStore.latest(config.session_dir)
        if session_id is None:
            raise SessionError(f"no sessions to continue in {config.session_dir}")
    else:
        return SessionStore(config.session_dir, SessionStore.new_id()), []

    store = SessionStore(config.session_dir, session_id)
    messages = store.load()
    print(f"resumed {session_id} ({len(messages)} messages)", file=sys.stderr)
    return store, messages


async def _async_main(argv: list[str]) -> int:
    try:
        session_flags, argv = _parse_session_flags(argv)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 1

    # Listing is read-only and needs no API key, so handle it before load_config.
    if session_flags.list_sessions:
        _print_session_list(session_dir_from_env())
        return 0

    try:
        config = load_config()
        agent_override, argv = _parse_agent_selector(argv)
        if agent_override:
            config.agent_name = agent_override
        store, messages = _open_session(config, session_flags)
    except (ConfigError, SessionError) as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 1

    print("loading agent bundle...", file=sys.stderr)
    try:
        bundle_path = resolve_bundle(config)
        manifest = load_bundle(bundle_path)
        base_env = {
            **base_env_from(manifest.ca_bundle_file, manifest.shell_env),
            "HOME": "/work",
        }
    except BundleError as error:
        print(f"startup error: {error}", file=sys.stderr)
        return 1

    # The provider binding is resolved only after the manifest loads, so the
    # agent's declared profile can supply the model/base_url (config.py §9).
    try:
        runtime = resolve_runtime(config, manifest)
        provider = _build_provider(runtime)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return 1

    jail = JailBuilder(
        config.work_tree,
        manifest.shell_path,
        base_env=base_env,
        shell_closure=manifest.shell_closure,
        shell_hook=manifest.shell_hook,
    )
    policy = PolicyEngine(headless=config.headless)
    # Background tasks: the registry monitors detached runs on this async loop.
    # Completion notices land on `notices`, which the run drains to inject
    # follow-up turns; `shutdown_all` reaps every task on exit.
    notices: asyncio.Queue[Notice] = asyncio.Queue()
    audit = FileAuditLog(config.audit_path)
    registry = BackgroundRegistry(
        notices=notices,
        loop=asyncio.get_running_loop(),
        output_truncate=config.output_truncate,
        audit=audit,
    )
    broker = Broker(
        manifest,
        jail,
        policy,
        output_truncate=config.output_truncate,
        audit=audit,
        registry=registry,
    )
    # The agent's Nix definition owns its persona; the config default is a fallback.
    system_prompt = manifest.system_prompt or config.system_prompt
    loop = AgentLoop(provider, broker, manifest, system_prompt)

    tool_names = ", ".join(tool["name"] for tool in manifest.tools)
    mode = "headless" if config.headless else "interactive"
    print(
        f"Tartarus ({mode}) — model={runtime.model} "
        f"base_url={runtime.base_url} agent={config.agent_name} system={host_system()} work_tree={config.work_tree}"
    )
    print(
        f"tools from {_bundle_manifest_source(bundle_path)}: {tool_names}",
        file=sys.stderr,
    )
    print(f"audit log: {config.audit_path}", file=sys.stderr)
    if store is not None:
        print(f"session: {store.session_id} ({store.path})", file=sys.stderr)

    prompt = " ".join(argv).strip()
    try:
        if prompt:
            return await _run_one_shot(loop, prompt, messages, store, registry, notices)
        return await _run_repl(loop, messages, store, registry, notices)
    finally:
        registry.shutdown_all()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    return asyncio.run(_async_main(argv))
