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
from typing import Any, cast

from pydantic import ValidationError

from tartarus.manifest import Capability, Manifest


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
    """Validate decoded manifest JSON and build typed objects.

    Maps JSON camelCase keys to Python snake_case, flattens the nested
    `network.allowedHosts` grant field, then delegates all type/shape/cross-field
    validation to Pydantic models.
    """
    if not isinstance(raw, dict):
        raise ManifestError("manifest must be a JSON object")

    mapped = _map_manifest_raw(cast(dict[str, Any], raw))

    try:
        return Manifest.model_validate(mapped)
    except ValidationError as error:
        raise ManifestError(_format_validation_error(error)) from error


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


def validate_realized_shell_hook(manifest: Manifest) -> None:
    hook = manifest.shell_hook
    if not hook:
        return
    if not os.path.isfile(hook):
        raise ManifestError(
            f"shell hook '{hook}' is missing; the bundle is not fully realized"
        )


def resolve_realized_closures(manifest: Manifest) -> Manifest:
    """Read each emitted `store-paths` file into its grant's `closure_paths`.

    Runs after the bundle is realized, so the files exist on disk. The jail
    binds exactly these paths plus the agent's baseline `shell_closure`, so a
    capability reaches its declared closure and nothing else. Fails closed: a
    missing or malformed closure file refuses to start (mirrors
    `validate_realized_package_bins`).
    """
    validate_realized_shell_hook(manifest)
    shell_closure = _read_closure_file("agent shell", manifest.shell_closure_file)
    capabilities = {
        name: capability.model_copy(
            update={
                "grants": capability.grants.model_copy(
                    update={
                        "closure_paths": _read_closure_file(
                            f"capability '{name}'", capability.grants.closure_file
                        ),
                    }
                )
            }
        )
        for name, capability in manifest.capabilities.items()
    }
    return manifest.model_copy(
        update={"capabilities": capabilities, "shell_closure": shell_closure}
    )


def _build_capability(name: str, body: object) -> Capability:
    """Build a single Capability from a raw body. Thin wrapper around Pydantic.

    Exposed so tests can validate capability-level rules in isolation
    without building a full manifest.
    """
    mapped = _map_capability_raw(name, body)
    try:
        return Capability.model_validate(mapped)
    except ValidationError as error:
        raise ManifestError(_format_validation_error(error)) from error


# ── JSON → Python key mapping ────────────────────────────────────────────────


def _map_manifest_raw(raw: dict[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {
        "tools": raw.get("tools", []),
        "capabilities": _map_capabilities(raw.get("capabilities", {})),
        "ca_bundle_file": raw.get("caBundle", ""),
        "shell_closure_file": raw.get("shellClosure", ""),
        "shell_path": raw.get("shellPath", ""),
        "shell_env": raw.get("shellEnv", {}),
        "shell_hook": raw.get("shellHook", ""),
    }
    if "systemPrompt" in raw:
        mapped["system_prompt"] = raw["systemPrompt"]
    if "model" in raw:
        mapped["model"] = _map_model_raw(raw["model"])
    return mapped


def _map_capabilities(raw: object) -> dict[str, Any]:
    capabilities = _require_object(raw, "manifest 'capabilities'")
    return {
        name: _map_capability_raw(name, body) for name, body in capabilities.items()
    }


def _map_capability_raw(name: str, body: object) -> dict[str, Any]:
    capability = _require_object(body, f"capability '{name}'")
    return {
        "name": name,
        "description": capability.get("description", ""),
        "policy": capability.get("policy"),
        "params": _require_object(
            capability.get("params", {}), f"capability '{name}' params"
        ),
        "grants": _map_grant_raw(name, capability.get("grants", {})),
        "runner": capability.get("runner", ""),
        "timeout": capability.get("timeout"),
        "kind": capability.get("kind", "command"),
        "control": capability.get("control"),
    }


def _map_grant_raw(capability_name: str, body: object) -> dict[str, Any]:
    grants = _require_object(body, f"capability '{capability_name}' grants")
    network = _require_object(
        grants.get("network", {}), f"capability '{capability_name}' grants network"
    )
    return {
        "package_bins": grants.get("packageBins", []),
        "allowed_hosts": network.get("allowedHosts", []),
        "writable": grants.get("writable", []),
        "unrestricted": grants.get("unrestricted", False),
        "closure_file": grants.get("closure", ""),
    }


_KNOWN_MODEL_KEYS = frozenset({"provider", "baseUrl", "name", "maxTokens", "sampling"})


def _map_model_raw(raw: object) -> dict[str, Any]:
    body = _require_object(raw, "manifest 'model'")

    unknown_keys = sorted(set(body) - _KNOWN_MODEL_KEYS)
    if unknown_keys:
        raise ManifestError(
            "manifest 'model' has unsupported keys: " + ", ".join(unknown_keys)
        )

    mapped: dict[str, Any] = {}

    for json_key, py_key in (
        ("baseUrl", "base_url"),
        ("maxTokens", "max_tokens"),
    ):
        if json_key in body:
            mapped[py_key] = body[json_key]

    for key in ("name", "provider", "sampling"):
        if key in body:
            mapped[key] = body[key]

    return mapped


def _require_object(value: object, label: str) -> dict[str, Any]:
    """Return value as a dict, or fail closed with a contextual message.

    The mapping layer must traverse nested objects (`grants`, `network`,
    `params`) to rename their keys, so a non-object there would crash before
    Pydantic ever sees it. Guarding here keeps every shape error a ManifestError.
    """
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be an object")
    return cast(dict[str, Any], value)


def _format_validation_error(error: ValidationError) -> str:
    """Condense a Pydantic ValidationError into one readable clause per problem.

    Pydantic's default string form is a multi-line dump carrying internal type
    codes and a docs URL. The manifest is authored in Nix, so surface only the
    field location and the message a human needs to fix it.
    """
    problems: list[str] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail["loc"])
        # Pydantic prefixes every validator ValueError with "Value error, "; drop it.
        message = detail["msg"].removeprefix("Value error, ")
        if location:
            problems.append(f"{location}: {message}")
        else:
            problems.append(message)
    return "; ".join(problems)


# ── Closure file I/O ─────────────────────────────────────────────────────────


def _read_closure_file(label: str, closure_file: str) -> list[str]:
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
