"""Manifest data types and provider-neutral tool projection helpers.

The tools the model sees and the capabilities the broker enforces are derived
from the same source: `build_manifest` projects every non-deny capability into a
tool.
"""

from typing import Any

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Grant:
    """The host reach a capability opens. Empty means "nothing beyond the shell"."""

    package_bins: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    writable: list[str] = field(default_factory=list)
    unrestricted: bool = False
    # The store path of the `closureInfo` `store-paths` file for this grant's
    # packages (emitted by Nix). `closure_paths` is its realized contents — the
    # exact store paths the jail binds, so the capability reaches its declared
    # closure and nothing else. Populated after realization (manifest_loader).
    closure_file: str = ""
    closure_paths: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Param:
    type: str  # JSON Schema scalar: "string" | "integer" | "boolean" | "array"
    description: str
    required: bool = False
    enum: list[Any] | None = None


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    policy: str  # "auto" | "ask-once" | "ask-always" | "deny"
    params: dict[str, Param]
    grants: Grant
    runner: str
    # Per-capability wall-clock budget in seconds. None means the capability runs
    # unbounded; a declared value caps it at that many seconds.
    timeout: int | None = None
    # How the broker runs this capability (PLAN.md §6.5):
    #   "command"    — run in the jail, capture output, return one result (default).
    #   "background" — launch detached, return a handle; track in the registry.
    #   "control"    — operate on the background registry (see `control`); no jail.
    kind: str = "command"
    # For kind == "control": which registry operation this tool performs,
    # one of "status" | "output" | "stop". None for every other kind.
    control: str | None = None


Sampling = dict[str, int | float]


@dataclass(frozen=True)
class ModelConfig:
    """The model an agent declares: backend binding + inference knobs (PLAN.md §9).

    A model id is only meaningful next to the base_url that serves it, so they
    travel together alongside provider-portable inference knobs. Secrets and
    deployment-specific headers are sourced from the environment, never from the
    Nix store.
    """

    base_url: str | None = None
    name: str | None = None
    provider: str | None = None  # provider type, e.g. "openai-compat"
    max_tokens: int | None = None
    sampling: Sampling | None = None


@dataclass(frozen=True)
class Manifest:
    tools: list[dict]  # provider-neutral tool defs (name/description/parameters)
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
    shell_closure: list[str] = field(default_factory=list)


def _params_to_json_schema(params: dict[str, Param]) -> dict[str, Any]:
    """Project typed params into a provider-neutral JSON Schema object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in params.items():
        schema: dict[str, Any] = {"type": param.type, "description": param.description}
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
