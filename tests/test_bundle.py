"""Bundle loading (PLAN.md §14): build/locate a bundle, read it with no nix."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from tartarus.bundle import (
    BundleError,
    base_env_from,
    load_bundle,
    resolve_bundle,
)
from tartarus.config import Config
from tartarus.manifest_loader import host_system

REPO_ROOT = Path(__file__).resolve().parent.parent

_NEEDS_NIX = pytest.mark.skipif(shutil.which("nix") is None, reason="requires nix")


def _minimal_manifest(shell_closure_file: str) -> dict:
    return {
        "tools": [
            {
                "name": "echo",
                "description": "Echo.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message": {"type": "string", "description": "text"}
                    },
                    "required": ["message"],
                },
            }
        ],
        "capabilities": {
            "echo": {
                "description": "Echo.",
                "policy": "auto",
                "params": {
                    "message": {
                        "type": "string",
                        "description": "text",
                        "required": True,
                        "enum": None,
                    }
                },
                "grants": {
                    "packageBins": [],
                    "network": {"allowedHosts": []},
                    "writable": [],
                    "unrestricted": False,
                },
                "runner": "echo {message}",
            }
        },
        "shellClosure": shell_closure_file,
        "shellPath": "",
        "caBundle": "/nix/store/cacert/etc/ssl/certs/ca-bundle.crt",
    }


# --- load_bundle ------------------------------------------------------------


def test_load_bundle_reads_manifest_and_resolves_closures(tmp_path, monkeypatch):
    def fake_read_closure_file(label: str, closure_file: str) -> list[str]:
        assert (
            closure_file == "/nix/store/shell-closure/store-paths" or not closure_file
        )
        if label == "agent shell":
            return ["/nix/store/bash", "/nix/store/coreutils"]
        return []

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps(_minimal_manifest("/nix/store/shell-closure/store-paths"))
    )
    monkeypatch.setattr(
        "tartarus.manifest_loader._read_closure_file", fake_read_closure_file
    )

    manifest = load_bundle(str(bundle))

    assert {tool["name"] for tool in manifest.tools} == {"echo"}
    assert manifest.shell_closure == ["/nix/store/bash", "/nix/store/coreutils"]


def test_load_bundle_missing_manifest_fails_closed(tmp_path):
    with pytest.raises(BundleError, match="cannot read bundle manifest"):
        load_bundle(str(tmp_path))


def test_load_bundle_invalid_json_fails_closed(tmp_path):
    (tmp_path / "manifest.json").write_text("{not json")

    with pytest.raises(BundleError, match="not valid JSON"):
        load_bundle(str(tmp_path))


def test_load_bundle_propagates_contract_violation(tmp_path):
    bad = _minimal_manifest("")
    bad["capabilities"]["echo"]["policy"] = "sometimes"
    (tmp_path / "manifest.json").write_text(json.dumps(bad))

    with pytest.raises(BundleError, match="invalid bundle manifest"):
        load_bundle(str(tmp_path))


def test_load_bundle_rejects_missing_ca_bundle(tmp_path):
    shell_file = tmp_path / "shell-store-paths"
    shell_file.write_text("/nix/store/bash\n")
    manifest = _minimal_manifest(str(shell_file))
    del manifest["caBundle"]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))

    with pytest.raises(BundleError, match="caBundle.*required"):
        load_bundle(str(tmp_path))


# --- base_env_from ----------------------------------------------------------


def test_base_env_from_sets_cert_vars_and_locale():
    cert = "/nix/store/cacert/etc/ssl/certs/ca-bundle.crt"

    assert base_env_from(cert) == {
        "SSL_CERT_FILE": cert,
        "NIX_SSL_CERT_FILE": cert,
        "CURL_CA_BUNDLE": cert,
        "REQUESTS_CA_BUNDLE": cert,
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
    }


def test_base_env_from_fails_closed_without_bundle():
    with pytest.raises(BundleError, match="caBundle"):
        base_env_from("")


# --- resolve_bundle ---------------------------------------------------------


def test_resolve_bundle_uses_configured_path_without_nix(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("nix must not run when bundle_path is set")

    monkeypatch.setattr("tartarus.bundle.run_checked", fail)

    config = Config(bundle_path="/nix/store/abc-bundle")
    assert resolve_bundle(config) == "/nix/store/abc-bundle"


def test_resolve_bundle_builds_from_flake(monkeypatch):
    calls = []

    def fake_run_checked(command: list[str]) -> str:
        calls.append(command)
        return "/nix/store/xyz-bundle\n"

    monkeypatch.setattr("tartarus.bundle.run_checked", fake_run_checked)

    config = Config(flake_ref="path:.", agent_name="research")
    out = resolve_bundle(config)

    system = host_system()
    assert out == "/nix/store/xyz-bundle"
    assert calls == [
        [
            "nix",
            "build",
            f"path:.#agents.{system}.research.bundle",
            "--no-link",
            "--print-out-paths",
        ]
    ]


# --- integration: build + load the example flake's bundle -------------------


@_NEEDS_NIX
def test_default_flake_bundle_loads_and_is_self_contained():
    config = Config(flake_ref=f"path:{REPO_ROOT}")
    bundle_path = resolve_bundle(config)
    manifest = load_bundle(bundle_path)

    # The contract survives the round-trip through the bundle.
    assert manifest.capabilities["git_status"].policy == "auto"
    assert "--end-of-options" in manifest.capabilities["git_show"].runner
    assert (
        "artifact path must stay under artifacts"
        in manifest.capabilities["write_artifact"].runner
    )
    assert any(
        "git" in package_bin
        for package_bin in manifest.capabilities["git_status"].grants.package_bins
    )
    assert manifest.capabilities["shell_escape"].grants.unrestricted is True
    assert manifest.system_prompt and "Tartarus" in manifest.system_prompt
    # PATH is baked and every entry is a store bin dir.
    assert manifest.shell_path
    assert all(
        entry.startswith("/nix/store/") and entry.endswith("/bin")
        for entry in manifest.shell_path.split(":")
    )
    assert not any("git" in entry for entry in manifest.shell_path.split(":"))
    assert not any(
        broad_tool in store_path
        for broad_tool in ("git", "python3", "perl", "curl")
        for store_path in manifest.shell_closure
    )

    # The bundle's closure is self-contained: the cert and a grant's closure
    # member are reachable from the single root, so `nix copy` ships everything.
    closure = subprocess.run(
        ["nix-store", "--query", "--requisites", bundle_path],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert any("ca-cert" in line or "cacert" in line for line in closure.splitlines())
    assert manifest.ca_bundle_file.split("/")[3] in {
        line.split("/")[-1] for line in closure.splitlines()
    }


@_NEEDS_NIX
def test_bad_flake_ref_fails_closed():
    config = Config(flake_ref="path:/nonexistent/flake/dir")
    with pytest.raises(BundleError):
        resolve_bundle(config)
