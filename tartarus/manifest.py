"""Manifest data types and provider-neutral tool projection helpers.

The tools the model sees and the capabilities the broker enforces are derived
from the same source: `build_manifest` projects every non-deny capability into a
tool.
"""

from __future__ import annotations

import string
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing_extensions import Self


# ── Grant ────────────────────────────────────────────────────────────────────


class Grant(BaseModel):
    """The host reach a capability opens. Empty means "nothing beyond the shell"."""

    model_config = ConfigDict(frozen=True, strict=True)

    package_bins: list[str] = Field(default_factory=list)
    allowed_hosts: list[str] = Field(default_factory=list)
    writable: list[str] = Field(default_factory=list)
    unrestricted: bool = Field(default=False)
    # The store path of the `closureInfo` `store-paths` file for this grant's
    # packages (emitted by Nix). `closure_paths` is its realized contents — the
    # exact store paths the jail binds, so the capability reaches its declared
    # closure and nothing else. Populated after realization (manifest_loader).
    closure_file: str = ""
    closure_paths: list[str] = Field(default_factory=list)

    @field_validator("package_bins")
    @classmethod
    def _validate_package_bins(cls, v: list[str]) -> list[str]:
        for entry in v:
            if not entry.startswith("/"):
                raise ValueError(
                    f"packageBins entry '{entry}' must be an absolute path"
                )
            if not entry.startswith("/nix/store/"):
                raise ValueError(
                    f"packageBins entry '{entry}' must be under /nix/store"
                )
            if not entry.endswith("/bin"):
                raise ValueError(f"packageBins entry '{entry}' must end with /bin")
        return v

    @field_validator("writable")
    @classmethod
    def _validate_writable(cls, v: list[str]) -> list[str]:
        for entry in v:
            if entry.startswith("/"):
                raise ValueError(f"writable path '{entry}' must be relative")
            if ".." in entry.split("/"):
                raise ValueError(f"writable path '{entry}' escapes the work tree")
        return v

    @field_validator("closure_file")
    @classmethod
    def _validate_closure_file(cls, v: str) -> str:
        if not v:
            return v
        if not v.startswith("/nix/store/"):
            raise ValueError("closure must be under /nix/store")
        if not v.endswith("/store-paths"):
            raise ValueError("closure must end with /store-paths")
        return v


# ── Param ────────────────────────────────────────────────────────────────────


class Param(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    type: Literal["string", "integer", "boolean", "array"]
    description: str = ""
    required: bool = Field(default=False)
    enum: list[Any] | None = None

    @model_validator(mode="after")
    def _validate_enum_entries(self) -> Self:
        if self.enum is None:
            return self
        for entry in self.enum:
            if self.type == "string" and not isinstance(entry, str):
                raise ValueError("enum entries must match type 'string'")
            if self.type == "integer":
                if isinstance(entry, bool) or not isinstance(entry, int):
                    raise ValueError("enum entries must match type 'integer'")
            if self.type == "boolean" and not isinstance(entry, bool):
                raise ValueError("enum entries must match type 'boolean'")
            if self.type == "array" and not isinstance(entry, list):
                raise ValueError("enum entries must match type 'array'")
        return self


# ── Capability ───────────────────────────────────────────────────────────────


class Capability(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    name: str
    description: str = ""
    policy: Literal["auto", "ask-once", "ask-always", "deny"]
    params: dict[str, Param]
    grants: Grant
    runner: str = ""
    # Per-capability wall-clock budget in seconds. None means the capability runs
    # unbounded; a declared value caps it at that many seconds.
    timeout: int | None = None
    # How the broker runs this capability (PLAN.md §6.5):
    #   "command"    — run in the jail, capture output, return one result (default).
    #   "background" — launch detached, return a handle; track in the registry.
    #   "control"    — operate on the background registry (see `control`); no jail.
    kind: Literal["command", "background", "control"] = "command"
    # For kind == "control": which registry operation this tool performs,
    # one of "status" | "output" | "stop". None for every other kind.
    control: Literal["status", "output", "stop"] | None = None

    @field_validator("timeout", mode="before")
    @classmethod
    def _reject_bool_timeout(cls, v: object) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool):
            raise ValueError("timeout must be a positive integer")
        if not isinstance(v, int) or v <= 0:
            raise ValueError("timeout must be a positive integer")
        return v

    @model_validator(mode="after")
    def _validate_unrestricted(self) -> Self:
        if self.grants.unrestricted and self.policy == "auto":
            raise ValueError(
                "unrestricted grant is not allowed under 'auto' policy; "
                "the unrestricted escape must never be silent"
            )
        return self

    @model_validator(mode="after")
    def _validate_kind_rules(self) -> Self:
        if self.kind == "control":
            if self.control is None:
                raise ValueError("control capability must declare a control op")
            if self.runner:
                raise ValueError("control capability must not declare a runner")
            if _grant_opens_reach(self.grants):
                raise ValueError("control capability must not declare grants")
        else:
            if self.control is not None:
                raise ValueError("'control' is only valid for kind 'control'")
        if self.kind == "background":
            if self.grants.unrestricted:
                raise ValueError("background capability cannot be unrestricted")
            if self.timeout is not None:
                raise ValueError(
                    "background capability cannot declare a timeout; "
                    "a background task runs until it exits or is stopped"
                )
        return self

    @model_validator(mode="after")
    def _validate_runner_placeholders(self) -> Self:
        for _, field_name, _, _ in string.Formatter().parse(self.runner):
            if field_name and field_name not in self.params:
                raise ValueError(f"runner references undeclared param '{field_name}'")
        return self


def _grant_opens_reach(grant: Grant) -> bool:
    return bool(
        grant.package_bins
        or grant.allowed_hosts
        or grant.writable
        or grant.unrestricted
    )


# ── ModelConfig ──────────────────────────────────────────────────────────────


Sampling = dict[str, int | float]


_RESERVED_SAMPLING_KEYS = frozenset(
    {"model", "max_tokens", "messages", "stream", "tools", "tool_choice"}
)


class ModelConfig(BaseModel):
    """The model an agent declares: backend binding + inference knobs (PLAN.md §9).

    A model id is only meaningful next to the base_url that serves it, so they
    travel together alongside provider-portable inference knobs.  Secrets and
    deployment-specific headers are sourced from the environment, never from the
    Nix store.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    base_url: str | None = None
    name: str | None = None
    provider: str | None = None  # provider type, e.g. "openai-compat"
    max_tokens: int | None = None
    sampling: Sampling | None = None

    @field_validator("base_url", "name", "provider")
    @classmethod
    def _reject_empty_strings(cls, v: str | None) -> str | None:
        if v is not None and (not isinstance(v, str) or v == ""):
            raise ValueError("must be a non-empty string")
        return v

    @field_validator("max_tokens", mode="before")
    @classmethod
    def _reject_bool_max_tokens(cls, v: object) -> int | None:
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ValueError("maxTokens must be a positive integer")
        return v

    @field_validator("sampling", mode="before")
    @classmethod
    def _validate_sampling(cls, v: object) -> object:
        if v is None:
            return None
        if not isinstance(v, dict):
            raise ValueError("sampling must be an object")
        for key, val in v.items():
            if key in _RESERVED_SAMPLING_KEYS:
                raise ValueError(
                    f"model sampling key '{key}' is reserved and cannot be overridden"
                )
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise ValueError(f"model sampling '{key}' must be a number")
        return v


# ── Manifest ─────────────────────────────────────────────────────────────────


class Manifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    tools: list[dict]
    capabilities: dict[str, Capability]
    # The agent's persona, declared in Nix. None when the agent declares none.
    system_prompt: str | None = None
    # The agent's model block, declared in Nix. None when the agent declares none,
    # in which case the harness defaults (or env overrides) supply everything.
    model: ModelConfig | None = None
    # The CA bundle path emitted by the agent flake. Exported into jailed tools
    # (base_env), and the Nix shell closure binds its store root.
    ca_bundle_file: str = ""
    # The baked baseline PATH for the jail (colon-joined `…/bin` store dirs),
    # emitted by Nix from the declared shell packages. Replaces a `nix develop`
    # resolution, so PATH always equals the bound shell closure.
    shell_path: str = ""
    # The baseline closure every jailed call binds (shell PATH packages + the CA
    # bundle), as the `store-paths` file path emitted by Nix and its realized
    # contents. `shell_closure` is filled after realization (manifest_loader).
    shell_closure_file: str = ""
    shell_closure: list[str] = Field(default_factory=list)

    @field_validator("ca_bundle_file")
    @classmethod
    def _validate_ca_bundle(cls, v: str) -> str:
        if not v or not v.startswith("/nix/store/"):
            raise ValueError("caBundle must be under /nix/store")
        return v

    @field_validator("shell_closure_file")
    @classmethod
    def _validate_shell_closure_file(cls, v: str) -> str:
        if not v:
            raise ValueError("shellClosure is required")
        if not v.startswith("/nix/store/"):
            raise ValueError("shellClosure must be under /nix/store")
        if not v.endswith("/store-paths"):
            raise ValueError("shellClosure must end with /store-paths")
        return v

    @field_validator("shell_path")
    @classmethod
    def _validate_shell_path_entries(cls, v: str) -> str:
        for entry in v.split(":"):
            if not entry:
                continue
            if not entry.startswith("/nix/store/"):
                raise ValueError(f"shellPath entry '{entry}' must be under /nix/store")
            if not entry.endswith("/bin"):
                raise ValueError(f"shellPath entry '{entry}' must end with /bin")
        return v

    @model_validator(mode="after")
    def _validate_tools_consistency(self) -> Self:
        for tool in self.tools:
            if not isinstance(tool, dict):
                raise ValueError("tool entries must be objects")
            name = tool.get("name")
            if not isinstance(name, str):
                raise ValueError("tool 'name' must be a string")
            capability = self.capabilities.get(name)
            if capability is None:
                raise ValueError(f"tool '{name}' has no matching capability")
            if capability.policy == "deny":
                raise ValueError(
                    f"tool '{name}' is exposed but its capability policy is 'deny'"
                )
            _validate_tool_schema_against_params(tool, capability)
        return self


def _validate_tool_schema_against_params(
    tool: dict[str, Any], capability: Capability
) -> None:
    schema = tool.get("parameters", {})
    if not isinstance(schema, dict):
        raise ValueError(f"tool '{capability.name}' parameters must be an object")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError(
            f"tool '{capability.name}' parameters properties must be an object"
        )
    required = schema.get("required", [])
    if not isinstance(required, list):
        raise ValueError(f"tool '{capability.name}' parameters required must be a list")

    schema_properties = set(properties.keys())
    schema_required = set(required)

    declared = set(capability.params.keys())
    declared_required = {
        name for name, param in capability.params.items() if param.required
    }

    if schema_properties != declared:
        raise ValueError(
            f"tool '{capability.name}' parameters {sorted(schema_properties)} "
            f"do not match capability params {sorted(declared)}"
        )
    if schema_required != declared_required:
        raise ValueError(
            f"tool '{capability.name}' required {sorted(schema_required)} does not "
            f"match capability required {sorted(declared_required)}"
        )


# ── Tool projection helpers ──────────────────────────────────────────────────


def _params_to_json_schema(params: dict[str, Param]) -> dict[str, Any]:
    """Project typed params into a provider-neutral JSON Schema object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in params.items():
        schema: dict[str, Any] = {
            "type": param.type,
            "description": param.description,
        }
        if param.enum is not None:
            schema["enum"] = param.enum
        properties[name] = schema
        if param.required:
            required.append(name)
    return {"type": "object", "properties": properties, "required": required}


def tool_from_capability(capability: Capability) -> dict:
    """The model-facing tool projection of a capability."""
    return {
        "name": capability.name,
        "description": capability.description,
        "parameters": _params_to_json_schema(capability.params),
    }


def build_manifest(capabilities: dict[str, Capability]) -> Manifest:
    """Build a Manifest, exposing every non-deny capability as a tool."""
    tools = [
        tool_from_capability(capability)
        for capability in capabilities.values()
        if capability.policy != "deny"
    ]
    return Manifest(tools=tools, capabilities=capabilities)
