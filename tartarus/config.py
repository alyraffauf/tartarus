"""Harness configuration, loaded from environment variables (PLAN.md §9).

Provider config is the only thing that changes to switch LLM backends. API keys
come from the environment and are never embedded in code. Defaults target OpenCode
Zen so a single env var (OPENCODE_API_KEY) yields a working setup.
"""

import json
import os
from dataclasses import dataclass, field

from tartarus.manifest import Manifest, Sampling

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "glm-5.2"
# A coding agent writes whole files and long diffs in one turn, so the completion
# cap is generous. Sampling is left to the backend's own default here; an agent
# tunes its own feel via the `model.sampling` block (see agent.nix).
DEFAULT_MAX_TOKENS = 16384
DEFAULT_STATE_DIR = ".tartarus"
DEFAULT_AUDIT_PATH = f"{DEFAULT_STATE_DIR}/audit.jsonl"
DEFAULT_SESSIONS_DIR = f"{DEFAULT_STATE_DIR}/sessions"
DEFAULT_OUTPUT_TRUNCATE_CHARS = 10_000
# `path:` copies the directory regardless of git tracking, which keeps local
# capability edits visible before they are committed.
DEFAULT_FLAKE_REF = "path:."
DEFAULT_AGENT_NAME = "default"
DEFAULT_SYSTEM_PROMPT = (
    "You are a coding agent running inside the Tartarus harness. "
    "Use the provided tools when they help you answer the user. "
    "Package grants make binaries available only for the current tool call; "
    "never describe them as permanently installed."
)

# Environment variables consulted for the API key, in priority order.
API_KEY_ENV_VARS = ("TARTARUS_API_KEY", "OPENCODE_API_KEY")


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass
class Config:
    # The runtime fields are None when their env var is unset, so the agent's
    # `model` block can supply the value (resolve_runtime). An explicit env var
    # still wins. api_key is the exception: env-only, since it is a secret.
    api_key: str = ""
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    extra_headers: dict[str, str] | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Absolute path the agent operates on; bound into the jail as /work.
    work_tree: str = field(default_factory=os.getcwd)
    # Flake reference containing the selected agent output. Used to build the
    # agent bundle when no realized `bundle_path` is given.
    flake_ref: str = DEFAULT_FLAKE_REF
    # A realized agent bundle store path (e.g. received via `nix copy`). When set,
    # the harness loads it directly and never touches the flake (PLAN.md §14).
    bundle_path: str = ""
    # Agent name under #agents.<system> to load.
    agent_name: str = DEFAULT_AGENT_NAME
    # When true there is no human to approve ask-* policies, so they fail closed.
    headless: bool = False
    # Append-only JSONL audit log for brokered tool calls.
    audit_path: str = ""
    # Directory holding per-conversation transcript files (<id>.jsonl).
    session_dir: str = ""
    output_truncate: int = DEFAULT_OUTPUT_TRUNCATE_CHARS


def session_dir_from_env() -> str:
    """Resolve the sessions directory from the environment, no API key required.

    Shared by load_config and the CLI's read-only `--list-sessions` path.
    """
    work_tree = _read_env("WORK_TREE", os.getcwd()) or os.getcwd()
    return _read_env("SESSIONS_DIR", _default_state_path(work_tree, "sessions")) or ""


def _read_env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(f"TARTARUS_{name}", default)


def _read_bool_env(name: str) -> bool:
    return (_read_env(name, "") or "").lower() in {"1", "true", "yes"}


def _default_state_path(work_tree: str, leaf: str) -> str:
    return os.path.join(work_tree, DEFAULT_STATE_DIR, leaf)


def _read_api_key() -> str:
    for variable in API_KEY_ENV_VARS:
        value = os.environ.get(variable)
        if value:
            return value
    return ""


def _read_extra_headers() -> dict[str, str] | None:
    raw_headers = _read_env("EXTRA_HEADERS")
    if not raw_headers:
        return None
    try:
        parsed = json.loads(raw_headers)
    except json.JSONDecodeError as error:
        raise ConfigError(f"TARTARUS_EXTRA_HEADERS must be JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ConfigError("TARTARUS_EXTRA_HEADERS must be a JSON object")
    return {str(key): str(value) for key, value in parsed.items()}


def load_config() -> Config:
    """Build a Config from the environment, applying defaults.

    Fails closed: a missing API key raises ConfigError rather than attempting an
    unauthenticated request.
    """
    api_key = _read_api_key()
    if not api_key:
        names = " or ".join(API_KEY_ENV_VARS)
        raise ConfigError(f"no API key found; set {names}")

    work_tree = _read_env("WORK_TREE", os.getcwd()) or os.getcwd()
    default_audit_path = _default_state_path(work_tree, "audit.jsonl")
    audit_path = _read_env("AUDIT_PATH", default_audit_path) or default_audit_path
    session_dir = session_dir_from_env()

    raw_max_tokens = _read_env("MAX_TOKENS")
    return Config(
        # Left None when unset so the agent's profile can supply them; an explicit
        # value here still overrides the profile (resolve_runtime).
        provider=_read_env("PROVIDER"),
        base_url=_read_env("BASE_URL"),
        api_key=api_key,
        model=_read_env("MODEL"),
        max_tokens=int(raw_max_tokens) if raw_max_tokens else None,
        extra_headers=_read_extra_headers(),
        work_tree=work_tree,
        flake_ref=_read_env("FLAKE_REF", DEFAULT_FLAKE_REF) or DEFAULT_FLAKE_REF,
        bundle_path=_read_env("BUNDLE", "") or "",
        agent_name=_read_env("AGENT", DEFAULT_AGENT_NAME) or DEFAULT_AGENT_NAME,
        headless=_read_bool_env("HEADLESS"),
        audit_path=audit_path,
        session_dir=session_dir,
        output_truncate=int(
            _read_env("OUTPUT_TRUNCATE", str(DEFAULT_OUTPUT_TRUNCATE_CHARS))
            or DEFAULT_OUTPUT_TRUNCATE_CHARS
        ),
    )


@dataclass(frozen=True)
class ResolvedRuntime:
    """The effective backend binding for a run, after applying precedence.

    Per field: an explicit env var wins; otherwise the agent's `model` block;
    otherwise the built-in default. api_key is env-only (a secret) and required.
    """

    provider_type: str
    base_url: str
    model: str
    max_tokens: int
    api_key: str
    extra_headers: dict[str, str]
    sampling: Sampling | None


def resolve_runtime(config: Config, manifest: Manifest) -> ResolvedRuntime:
    """Combine env config and the agent's `model` block by precedence (§9).

    Per field: an explicit env var wins; otherwise the agent's declared value;
    otherwise the built-in default. api_key is env-only.
    """
    model = manifest.model
    return ResolvedRuntime(
        provider_type=(
            config.provider or (model.provider if model else None) or "openai-compat"
        ),
        base_url=(
            config.base_url or (model.base_url if model else None) or DEFAULT_BASE_URL
        ),
        model=(config.model or (model.name if model else None) or DEFAULT_MODEL),
        max_tokens=(
            config.max_tokens
            if config.max_tokens is not None
            else model.max_tokens
            if model and model.max_tokens is not None
            else DEFAULT_MAX_TOKENS
        ),
        api_key=config.api_key,
        extra_headers=config.extra_headers or {},
        # Sampling has no env override today: the agent's value, else None so the
        # backend applies its own default.
        sampling=model.sampling if model else None,
    )
