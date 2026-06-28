"""Load an agent from a realized bundle (PLAN.md §14).

A bundle is one store derivation whose runtime closure is the whole agent:
`manifest.json` plus every store path it references (package bins, per-capability
and shell closures, the CA bundle). The harness reads it with **no nix calls**, so
an agent built locally and one received via `nix copy` load identically.

`resolve_bundle` is the only place that may shell out to nix — and only to build
the bundle from a flake when no realized `bundle_path` was given. With a copied
bundle, loading is pure file reads.
"""

import json
import os

from tartarus.config import Config
from tartarus.manifest import Manifest
from tartarus.manifest_loader import (
    ManifestError,
    build_manifest_from_raw,
    host_system,
    resolve_realized_closures,
    validate_realized_package_bins,
)
from tartarus.process import ProcessError, run_checked


class BundleError(Exception):
    """Raised when an agent bundle cannot be resolved or read."""


def resolve_bundle(config: Config) -> str:
    """Return a realized bundle directory for the selected agent.

    A configured `bundle_path` (a received/copied store path) is used directly,
    with no nix. Otherwise the bundle is built from the flake — the single
    remaining nix call in the startup path.
    """
    if config.bundle_path:
        return config.bundle_path

    system = host_system()
    attr = f"{config.flake_ref}#agents.{system}.{config.agent_name}.config.build.bundle"
    try:
        out = run_checked(["nix", "build", attr, "--no-link", "--print-out-paths"])
    except ProcessError as error:
        raise BundleError(f"cannot build agent bundle: {error}") from error
    paths = [line for line in out.splitlines() if line.strip()]
    if not paths:
        raise BundleError(f"`nix build {attr}` produced no store path")
    return paths[-1]


def load_bundle(bundle_path: str) -> Manifest:
    """Read and validate the manifest from a realized bundle directory."""
    manifest_file = os.path.join(bundle_path, "manifest.json")
    try:
        with open(manifest_file, encoding="utf-8") as handle:
            raw = json.load(handle)
    except OSError as error:
        raise BundleError(
            f"cannot read bundle manifest '{manifest_file}': {error}"
        ) from error
    except json.JSONDecodeError as invalid:
        raise BundleError(
            f"bundle manifest '{manifest_file}' is not valid JSON: {invalid}"
        ) from invalid

    try:
        manifest = build_manifest_from_raw(raw)
        validate_realized_package_bins(manifest)
        return resolve_realized_closures(manifest)
    except ManifestError as error:
        raise BundleError(f"invalid bundle manifest: {error}") from error


# Certificate environment variables every TLS client in the jail consults; all
# point at the manifest's CA bundle (whose store root the shell closure binds).
_CERT_ENV_VARS = (
    "SSL_CERT_FILE",
    "NIX_SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
)


def base_env_from(ca_bundle_file: str) -> dict[str, str]:
    """The jail's baseline environment: CA bundle cert vars + a fixed locale.

    Fails closed if the bundle declares no CA bundle, rather than leaving TLS to
    an unset/unbound certificate. `C.UTF-8` is built into glibc, so it needs no
    locale package in the closure.
    """
    if not ca_bundle_file:
        raise BundleError(
            "bundle declares no CA bundle (caBundle); refusing to run with an "
            "unset certificate"
        )
    base_env = {var: ca_bundle_file for var in _CERT_ENV_VARS}
    base_env["LC_ALL"] = "C.UTF-8"
    base_env["LANG"] = "C.UTF-8"
    return base_env
