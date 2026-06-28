"""Harness configuration, loaded from environment variables (PLAN.md §9).

Provider config is the only thing that changes to switch LLM backends. API keys
come from the environment and are never embedded in code. Defaults target OpenCode
Zen so a single env var (OPENCODE_API_KEY) yields a working setup.
"""

from __future__ import annotations

import os

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing_extensions import Self

from tartarus.manifest import Manifest, Sampling

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "glm-5.2"
# A coding agent writes whole files and long diffs in one turn, so the completion
# cap is generous. Sampling is left to the backend's own default here; an agent
# tunes its own feel via the `model.sampling` block (see agent.nix).
DEFAULT_MAX_TOKENS = 16384
DEFAULT_STATE_DIR = ".tartarus"
# Leaf names under <work_tree>/.tartarus, shared by every path-deriving call site.
AUDIT_LOG_LEAF = "audit.jsonl"
SESSIONS_LEAF = "sessions"
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


class Config(BaseSettings):
    """Harness config, loaded from TARTARUS_* environment variables (PLAN.md §9).

    Each field reads from TARTARUS_<FIELD>; a few keep legacy env names via an
    explicit alias. Runtime fields (provider/base_url/model/max_tokens) stay None
    when unset so the agent's `model` block can supply them (resolve_runtime); an
    explicit env value still wins.
    """

    model_config = SettingsConfigDict(
        env_prefix="TARTARUS_", extra="ignore", populate_by_name=True
    )

    # The one secret: env-only, accepted under either the TARTARUS_ name or the
    # provider's own OPENCODE_API_KEY. Empty is allowed here so non-auth paths can
    # build a Config; load_config enforces a non-empty key (fail closed).
    api_key: str = ""
    provider: str | None = None
    base_url: str | None = None
    model: str | None = None
    max_tokens: int | None = None
    extra_headers: dict[str, str] | None = None
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    # Absolute path the agent operates on; bound into the jail as /work.
    work_tree: str = Field(default_factory=os.getcwd)
    # Flake reference containing the selected agent output. Used to build the
    # agent bundle when no realized `bundle_path` is given.
    flake_ref: str = DEFAULT_FLAKE_REF
    # A realized agent bundle store path (e.g. received via `nix copy`). When set,
    # the harness loads it directly and never touches the flake (PLAN.md §14).
    bundle_path: str = Field("", validation_alias=AliasChoices("TARTARUS_BUNDLE"))
    # Agent name under #agents.<system> to load.
    agent_name: str = Field(
        DEFAULT_AGENT_NAME, validation_alias=AliasChoices("TARTARUS_AGENT")
    )
    # When true there is no human to approve ask-* policies, so they fail closed.
    headless: bool = False
    # Append-only JSONL audit log for brokered tool calls. Empty -> derived below.
    audit_path: str = ""
    # Directory holding per-conversation transcript files (<id>.jsonl).
    session_dir: str = Field("", validation_alias=AliasChoices("TARTARUS_SESSIONS_DIR"))
    output_truncate: int = DEFAULT_OUTPUT_TRUNCATE_CHARS

    @model_validator(mode="after")
    def _derive_state_paths(self) -> Self:
        # The audit log and sessions dir default under <work_tree>/.tartarus unless
        # the environment supplied an explicit path.
        if not self.audit_path:
            self.audit_path = _default_state_path(self.work_tree, AUDIT_LOG_LEAF)
        if not self.session_dir:
            self.session_dir = _default_state_path(self.work_tree, SESSIONS_LEAF)
        return self


def session_dir_from_env() -> str:
    """Resolve the sessions directory from the environment, no API key required.

    Shared by load_config (via Config) and the CLI's read-only `--list-sessions`
    path, which runs before any API key is required, so it cannot build a Config.
    """
    work_tree = os.environ.get("TARTARUS_WORK_TREE") or os.getcwd()
    return os.environ.get("TARTARUS_SESSIONS_DIR") or _default_state_path(
        work_tree, SESSIONS_LEAF
    )


def _default_state_path(work_tree: str, leaf: str) -> str:
    return os.path.join(work_tree, DEFAULT_STATE_DIR, leaf)


def load_config() -> Config:
    """Build a Config from the environment.

    Fails closed: a missing API key or malformed value raises ConfigError rather
    than attempting an unauthenticated or misconfigured request.
    """
    try:
        config = Config()
    except ValidationError as error:
        raise _config_error(error) from error
    if not config.api_key:
        # TARTARUS_API_KEY is read via env_prefix; fall back to the alternates here.
        for variable in API_KEY_ENV_VARS:
            value = os.environ.get(variable, "")
            if value:
                config.api_key = value
                break
    if not config.api_key:
        raise ConfigError(f"no API key found; set {' or '.join(API_KEY_ENV_VARS)}")
    return config


def _config_error(error: ValidationError) -> ConfigError:
    """Translate a settings ValidationError into a user-facing ConfigError."""
    failed = {str(location) for entry in error.errors() for location in entry["loc"]}
    if "extra_headers" in failed:
        return ConfigError("TARTARUS_EXTRA_HEADERS must be a JSON object")
    # loc + msg only: ValidationError's str/input fields echo the offending value,
    # which would leak secrets like api_key into logs.
    details = "; ".join(
        f"{' -> '.join(str(loc) for loc in entry['loc'])}: {entry['msg']}"
        for entry in error.errors()
    )
    return ConfigError(f"invalid configuration: {details}")


class ResolvedRuntime(BaseModel):
    """The effective backend binding for a run, after applying precedence.

    Per field: an explicit env var wins; otherwise the agent's `model` block;
    otherwise the built-in default. api_key is env-only (a secret) and required.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

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
