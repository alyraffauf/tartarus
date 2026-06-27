"""Manifest validation (§5). Pure contract checks over decoded manifest JSON.

The manifest is loaded from a realized agent bundle by `tartarus.bundle`; this
module holds the validation. `build_manifest_from_raw` is pure — it takes the
decoded JSON and needs no subprocess, so the contract rules are unit-testable
without Nix. **Fails closed**: any contract violation raises ManifestError, and
the harness refuses to start rather than run against an unverified manifest.
`resolve_realized_closures` / `validate_realized_package_bins` then read the
realized store artifacts the manifest points at.
"""

import os
import platform
import string
from dataclasses import replace
from typing import Any, cast

from tartarus.manifest import Capability, Grant, Manifest, ModelConfig, Param, Sampling

VALID_POLICIES = frozenset({"auto", "ask-once", "ask-always", "deny"})
VALID_PARAM_TYPES = frozenset({"string", "integer", "boolean", "array"})
VALID_KINDS = frozenset({"command", "background", "control"})
VALID_CONTROL_OPS = frozenset({"status", "output", "stop"})


class ManifestError(Exception):
    """Raised on a Nix eval failure or any manifest contract violation."""


def host_system() -> str:
    machine = platform.machine().lower()
    operating_system = platform.system().lower()

    if machine in {"x86_64", "amd64"} and operating_system == "linux":
        return "x86_64-linux"
    if machine in {"aarch64", "arm64"} and operating_system == "linux":
        return "aarch64-linux"
    if machine in {"aarch64", "arm64"} and operating_system == "darwin":
        return "aarch64-darwin"

    raise ManifestError(
        f"unsupported host platform machine={platform.machine()!r} "
        f"system={platform.system()!r}"
    )


def build_manifest_from_raw(raw: object) -> Manifest:
    """Validate decoded manifest JSON against §5 and build typed objects."""
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")

    typed_raw = cast(dict[str, Any], raw)
    tools = typed_raw.get("tools")
    raw_capabilities = typed_raw.get("capabilities")
    if not isinstance(tools, list):
        raise ManifestError("manifest 'tools' must be a list")
    if not isinstance(raw_capabilities, dict):
        raise ManifestError("manifest 'capabilities' must be an object")

    capabilities = {
        name: _build_capability(name, body) for name, body in raw_capabilities.items()
    }
    _validate_tools(tools, capabilities)

    system_prompt = typed_raw.get("systemPrompt")
    if system_prompt is not None and not isinstance(system_prompt, str):
        raise ManifestError("manifest 'systemPrompt' must be a string")

    model = _build_model_config(typed_raw.get("model"))

    ca_bundle_file = _required_store_path(typed_raw, "caBundle")

    shell_closure_file = _required_store_paths_file(typed_raw, "shellClosure")

    shell_path = typed_raw.get("shellPath", "")
    if not isinstance(shell_path, str):
        raise ManifestError("manifest 'shellPath' must be a string")
    _validate_shell_path(shell_path)

    return Manifest(
        tools=tools,
        capabilities=capabilities,
        system_prompt=system_prompt,
        model=model,
        ca_bundle_file=ca_bundle_file,
        shell_path=shell_path,
        shell_closure_file=shell_closure_file,
    )


def _required_store_path(raw: dict[str, Any], field_name: str) -> str:
    if field_name not in raw:
        raise ManifestError(f"manifest '{field_name}' is required")

    value = raw[field_name]
    if not isinstance(value, str):
        raise ManifestError(f"manifest '{field_name}' must be a string")
    if not value:
        raise ManifestError(f"manifest '{field_name}' is required")
    if not value.startswith("/nix/store/"):
        raise ManifestError(f"manifest '{field_name}' must be under /nix/store")
    return value


def _required_store_paths_file(raw: dict[str, Any], field_name: str) -> str:
    if field_name not in raw:
        raise ManifestError(f"manifest '{field_name}' is required")

    value = raw[field_name]
    _validate_store_paths_file(f"manifest '{field_name}'", value)
    return value


def _object_field(raw: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    value = raw.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ManifestError(f"{label} '{key}' must be an object")
    return cast(dict[str, Any], value)


def _list_field(raw: dict[str, Any], key: str, label: str) -> list[Any]:
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise ManifestError(f"{label} '{key}' must be a list")
    return value


def _optional_string_field(
    raw: dict[str, Any], key: str, label: str, default: str = ""
) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ManifestError(f"{label} '{key}' must be a string")
    return value


def _optional_bool_field(raw: dict[str, Any], key: str, label: str) -> bool:
    value = raw.get(key, False)
    if not isinstance(value, bool):
        raise ManifestError(f"{label} '{key}' must be a boolean")
    return value


def _validate_store_paths_file(label: str, value: object) -> None:
    if not isinstance(value, str):
        raise ManifestError(f"{label} must be a string")
    if not value:
        raise ManifestError(f"{label} is required")
    if not value.startswith("/nix/store/"):
        raise ManifestError(f"{label} must be under /nix/store")
    if not value.endswith("/store-paths"):
        raise ManifestError(f"{label} must end with /store-paths")


def _validate_shell_path(shell_path: str) -> None:
    """Each baked PATH entry must be an absolute `/nix/store/**/bin` dir.

    Absent is tolerated (the Phase-0 / no-Nix path); a real bundle always bakes
    it from the declared shell packages.
    """
    for entry in shell_path.split(":"):
        if not entry:
            continue
        if not entry.startswith("/nix/store/"):
            raise ManifestError(
                f"manifest shellPath entry '{entry}' must be under /nix/store"
            )
        if not entry.endswith("/bin"):
            raise ManifestError(
                f"manifest shellPath entry '{entry}' must end with /bin"
            )


def _build_model_config(raw: object) -> ModelConfig | None:
    """Validate the optional `model` block (PLAN.md §9). Fails closed."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("manifest 'model' must be an object")

    _reject_unknown_model_keys(raw)

    base_url = _optional_nonempty_str(raw, "baseUrl")
    name = _optional_nonempty_str(raw, "name")
    provider = _optional_nonempty_str(raw, "provider")

    return ModelConfig(
        base_url=base_url,
        name=name,
        provider=provider,
        max_tokens=_build_max_tokens(raw.get("maxTokens")),
        sampling=_build_sampling(raw.get("sampling")),
    )


_MODEL_KEYS = frozenset({"provider", "baseUrl", "name", "maxTokens", "sampling"})


def _reject_unknown_model_keys(raw: dict) -> None:
    unknown_keys = sorted(set(raw) - _MODEL_KEYS)
    if unknown_keys:
        raise ManifestError(
            "manifest 'model' has unsupported keys: " + ", ".join(unknown_keys)
        )


def _optional_nonempty_str(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ManifestError(f"model '{key}' must be a non-empty string")
    return value


def _build_max_tokens(raw: object) -> int | None:
    if raw is None:
        return None
    # bool is an int subclass; reject it so `maxTokens = true` does not pass.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ManifestError("model 'maxTokens' must be a positive integer")
    return raw


_RESERVED_SAMPLING_KEYS = frozenset(
    {"model", "max_tokens", "messages", "stream", "tools", "tool_choice"}
)


def _build_sampling(raw: object) -> Sampling | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ManifestError("model 'sampling' must be an object")
    sampling: Sampling = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            raise ManifestError("model 'sampling' keys must be strings")
        if key in _RESERVED_SAMPLING_KEYS:
            raise ManifestError(
                f"model sampling key '{key}' is reserved and cannot be overridden"
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ManifestError(f"model sampling '{key}' must be a number")
        sampling[key] = value
    return sampling


def _build_capability(name: str, body: object) -> Capability:
    if not isinstance(body, dict):
        raise ManifestError(f"capability '{name}' must be an object")
    typed_body = cast(dict[str, Any], body)

    policy = typed_body.get("policy")
    if policy not in VALID_POLICIES:
        raise ManifestError(
            f"capability '{name}' has invalid policy {policy!r}; "
            f"expected one of {sorted(VALID_POLICIES)}"
        )

    params = {
        param_name: _build_param(name, param_name, param_body)
        for param_name, param_body in _object_field(
            typed_body, "params", f"capability '{name}'"
        ).items()
    }
    grants = _build_grant(
        name, _object_field(typed_body, "grants", f"capability '{name}'")
    )
    description = _optional_string_field(
        typed_body, "description", f"capability '{name}'"
    )
    runner = _optional_string_field(typed_body, "runner", f"capability '{name}'")
    timeout = _build_timeout(name, typed_body.get("timeout"))
    kind = _build_kind(name, typed_body.get("kind"))
    control = _build_control(name, kind, typed_body.get("control"))

    _validate_runner_placeholders(name, runner, params)
    _validate_unrestricted(name, policy, grants)
    _validate_kind(name, kind, grants, runner, timeout)

    return Capability(
        name=name,
        description=description,
        policy=policy,
        params=params,
        grants=grants,
        runner=runner,
        timeout=timeout,
        kind=kind,
        control=control,
    )


def _build_kind(capability: str, raw: object) -> str:
    if raw is None:
        return "command"
    if not (isinstance(raw, str) and raw in VALID_KINDS):
        raise ManifestError(
            f"capability '{capability}' has invalid kind {raw!r}; "
            f"expected one of {sorted(VALID_KINDS)}"
        )
    return raw


def _build_control(capability: str, kind: str, raw: object) -> str | None:
    if kind != "control":
        if raw is not None:
            raise ManifestError(
                f"capability '{capability}' sets 'control' but kind is {kind!r}; "
                "'control' is only valid for kind 'control'"
            )
        return None
    if not (isinstance(raw, str) and raw in VALID_CONTROL_OPS):
        raise ManifestError(
            f"control capability '{capability}' has invalid control op {raw!r}; "
            f"expected one of {sorted(VALID_CONTROL_OPS)}"
        )
    return raw


def _validate_kind(
    capability: str, kind: str, grants: Grant, runner: str, timeout: int | None
) -> None:
    """Enforce the structural rules each capability kind requires."""
    if kind == "control":
        # A control capability acts on the registry, never the jail: it must
        # carry no runner and open no host reach.
        if runner:
            raise ManifestError(
                f"control capability '{capability}' must not declare a runner"
            )
        if _grant_opens_reach(grants):
            raise ManifestError(
                f"control capability '{capability}' must not declare grants"
            )
    elif kind == "background":
        # A detached run is unbounded by definition (stop it via a control tool),
        # and the unrestricted host-escape is out of scope for background launch.
        if grants.unrestricted:
            raise ManifestError(
                f"background capability '{capability}' cannot be unrestricted"
            )
        if timeout is not None:
            raise ManifestError(
                f"background capability '{capability}' cannot declare a timeout; "
                "a background task runs until it exits or is stopped"
            )


def _grant_opens_reach(grant: Grant) -> bool:
    return bool(
        grant.package_bins
        or grant.allowed_hosts
        or grant.writable
        or grant.unrestricted
    )


def _build_timeout(capability: str, raw: object) -> int | None:
    """Validate the optional per-capability timeout.

    Absent (None) means the capability runs unbounded; a declared value caps it
    at that many seconds.
    """
    if raw is None:
        return None
    # bool is an int subclass; reject it so `timeout = true` does not pass.
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ManifestError(
            f"capability '{capability}' timeout must be a positive integer"
        )
    return raw


def _build_param(capability: str, name: str, body: object) -> Param:
    if not isinstance(body, dict):
        raise ManifestError(f"param '{capability}.{name}' must be an object")
    typed_body = cast(dict[str, Any], body)

    param_type = typed_body.get("type")
    if param_type not in VALID_PARAM_TYPES:
        raise ManifestError(
            f"param '{capability}.{name}' has invalid type {param_type!r}; "
            f"expected one of {sorted(VALID_PARAM_TYPES)}"
        )
    description = _optional_string_field(
        typed_body, "description", f"param '{capability}.{name}'"
    )
    required = _optional_bool_field(
        typed_body, "required", f"param '{capability}.{name}'"
    )
    enum = _build_param_enum(capability, name, param_type, typed_body.get("enum"))

    return Param(
        type=param_type,
        description=description,
        required=required,
        enum=enum,
    )


def _build_param_enum(
    capability: str, name: str, param_type: str, raw: object
) -> list[Any] | None:
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise ManifestError(f"param '{capability}.{name}' enum must be a list")
    for entry in raw:
        if not _enum_entry_matches_type(entry, param_type):
            raise ManifestError(
                f"param '{capability}.{name}' enum entries must match "
                f"type '{param_type}'"
            )
    return raw


def _enum_entry_matches_type(entry: object, param_type: str) -> bool:
    if param_type == "string":
        return isinstance(entry, str)
    if param_type == "integer":
        return not isinstance(entry, bool) and isinstance(entry, int)
    if param_type == "boolean":
        return isinstance(entry, bool)
    if param_type == "array":
        return isinstance(entry, list)
    return False


def _build_grant(capability: str, body: dict[str, Any]) -> Grant:
    network = _object_field(body, "network", f"capability '{capability}' grants")
    package_bins = _list_field(body, "packageBins", f"capability '{capability}' grants")
    writable = _list_field(body, "writable", f"capability '{capability}' grants")
    allowed_hosts = _list_field(
        network, "allowedHosts", f"capability '{capability}' grants network"
    )
    closure_file = _optional_string_field(
        body, "closure", f"capability '{capability}' grants"
    )
    unrestricted = _optional_bool_field(
        body, "unrestricted", f"capability '{capability}' grants"
    )
    _validate_package_bins(capability, package_bins)
    _validate_writable_paths(capability, writable)
    _validate_allowed_hosts(capability, allowed_hosts)
    _validate_closure_file(capability, closure_file)

    return Grant(
        package_bins=package_bins,
        allowed_hosts=allowed_hosts,
        writable=writable,
        unrestricted=unrestricted,
        closure_file=closure_file,
    )


def _validate_closure_file(capability: str, closure_file: object) -> None:
    """Validate the closure store-paths reference's shape (contents read later).

    Absence is tolerated here (like an empty `packageBins`) so the pure builder
    stays testable without Nix; a real Nix manifest always emits it, and the
    realized read in `resolve_realized_closures` fails closed if it is missing.
    """
    if not closure_file:
        return
    _validate_store_paths_file(f"capability '{capability}' closure", closure_file)


def _validate_package_bins(capability: str, package_bins: list[str]) -> None:
    for package_bin in package_bins:
        if not isinstance(package_bin, str):
            raise ManifestError(
                f"capability '{capability}' packageBins entries must be strings"
            )
        if not package_bin.startswith("/"):
            raise ManifestError(
                f"capability '{capability}' packageBins entry '{package_bin}' "
                "must be an absolute path"
            )
        if not package_bin.startswith("/nix/store/"):
            raise ManifestError(
                f"capability '{capability}' packageBins entry '{package_bin}' "
                "must be under /nix/store"
            )
        if not package_bin.endswith("/bin"):
            raise ManifestError(
                f"capability '{capability}' packageBins entry '{package_bin}' "
                "must end with /bin"
            )


def _validate_allowed_hosts(capability: str, allowed_hosts: list[str]) -> None:
    for host in allowed_hosts:
        if not isinstance(host, str):
            raise ManifestError(
                f"capability '{capability}' allowedHosts entries must be strings"
            )


def validate_realized_package_bins(manifest: Manifest) -> None:
    missing_bins = [
        package_bin
        for capability in manifest.capabilities.values()
        for package_bin in capability.grants.package_bins
        if not os.path.isdir(package_bin)
    ]
    if missing_bins:
        raise ManifestError(
            "realized packageBins are missing directories: " + ", ".join(missing_bins)
        )


def resolve_realized_closures(manifest: Manifest) -> Manifest:
    """Read each emitted `store-paths` file into its grant's `closure_paths`.

    Runs after the bundle is realized, so the files exist on disk. The jail
    binds exactly these paths plus the agent's baseline `shell_closure`, so a
    capability reaches its declared closure and nothing else. Fails closed: a
    missing or malformed closure file refuses to start (mirrors
    `validate_realized_package_bins`).
    """
    shell_closure = _read_closure_file("agent shell", manifest.shell_closure_file)
    capabilities = {
        name: replace(
            capability,
            grants=replace(
                capability.grants,
                closure_paths=_read_closure_file(
                    f"capability '{name}'", capability.grants.closure_file
                ),
            ),
        )
        for name, capability in manifest.capabilities.items()
    }
    return replace(manifest, capabilities=capabilities, shell_closure=shell_closure)


def _read_closure_file(label: str, closure_file: str) -> list[str]:
    # An absent reference binds nothing extra (the Phase-0 / no-Nix path). That
    # under-permits — a tool whose closure is unbound simply fails to run — so it
    # is fail-safe. A *declared* reference that cannot be read is a real error.
    if not closure_file:
        return []
    try:
        with open(closure_file, encoding="utf-8") as handle:
            paths = [line.strip() for line in handle if line.strip()]
    except OSError as error:
        raise ManifestError(
            f"{label} closure file '{closure_file}' is unreadable: {error}"
        ) from error
    for path in paths:
        if not path.startswith("/nix/store/"):
            raise ManifestError(
                f"{label} closure file '{closure_file}' has a non-store path '{path}'"
            )
    return paths


def _validate_runner_placeholders(
    capability: str, runner: str, params: dict[str, Param]
) -> None:
    for _, field_name, _, _ in string.Formatter().parse(runner):
        if field_name and field_name not in params:
            raise ManifestError(
                f"capability '{capability}' runner references undeclared "
                f"param '{field_name}'"
            )


def _validate_writable_paths(capability: str, writable: list[str]) -> None:
    for path in writable:
        if not isinstance(path, str):
            raise ManifestError(
                f"capability '{capability}' writable entries must be strings"
            )
        if path.startswith("/"):
            raise ManifestError(
                f"capability '{capability}' writable path '{path}' must be relative"
            )
        if ".." in path.split("/"):
            raise ManifestError(
                f"capability '{capability}' writable path '{path}' escapes the "
                "work tree"
            )


def _validate_unrestricted(capability: str, policy: str, grants: Grant) -> None:
    if grants.unrestricted and policy == "auto":
        raise ManifestError(
            f"capability '{capability}' is unrestricted under 'auto' policy; "
            "the unrestricted escape must never be silent"
        )


def _validate_tools(tools: list, capabilities: dict[str, Capability]) -> None:
    for tool in tools:
        if not isinstance(tool, dict):
            raise ManifestError("tool entries must be objects")
        typed_tool = cast(dict[str, Any], tool)
        name = typed_tool.get("name")
        if not isinstance(name, str):
            raise ManifestError("tool 'name' must be a string")
        _optional_string_field(typed_tool, "description", f"tool '{name}'")
        capability = capabilities.get(name)
        if capability is None:
            raise ManifestError(f"tool '{name}' has no matching capability")
        if capability.policy == "deny":
            raise ManifestError(
                f"tool '{name}' is exposed but its capability policy is 'deny'"
            )
        _validate_tool_consistency(typed_tool, capability)


def _validate_tool_consistency(tool: dict[str, Any], capability: Capability) -> None:
    schema = _object_field(tool, "parameters", f"tool '{capability.name}'")
    properties = _object_field(
        schema, "properties", f"tool '{capability.name}' parameters"
    )
    required = _list_field(schema, "required", f"tool '{capability.name}' parameters")
    for required_name in required:
        if not isinstance(required_name, str):
            raise ManifestError(
                f"tool '{capability.name}' required entries must be strings"
            )
    schema_properties = set(properties.keys())
    schema_required = set(required)

    declared = set(capability.params.keys())
    declared_required = {
        name for name, param in capability.params.items() if param.required
    }

    if schema_properties != declared:
        raise ManifestError(
            f"tool '{capability.name}' parameters {sorted(schema_properties)} "
            f"do not match capability params {sorted(declared)}"
        )
    if schema_required != declared_required:
        raise ManifestError(
            f"tool '{capability.name}' required {sorted(schema_required)} does not "
            f"match capability required {sorted(declared_required)}"
        )
