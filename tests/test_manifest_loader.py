import pytest

from tartarus.manifest_loader import (
    ManifestError,
    _build_capability,
    build_manifest_from_raw,
    host_system,
    resolve_realized_closures,
    validate_realized_package_bins,
)


def _valid_raw():
    """A minimal manifest that satisfies every §5 rule."""
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
            },
            "shell_escape": {
                "description": "Documented-off escape.",
                "policy": "deny",
                "params": {},
                "grants": {
                    "packageBins": [],
                    "network": {"allowedHosts": []},
                    "writable": [],
                    "unrestricted": True,
                },
                "runner": "bash",
            },
        },
        "caBundle": "/nix/store/cacert/etc/ssl/certs/ca-bundle.crt",
        "shellClosure": "/nix/store/shell-closure/store-paths",
    }


# --- happy path -------------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "operating_system", "expected"),
    [
        ("x86_64", "Linux", "x86_64-linux"),
        ("amd64", "Linux", "x86_64-linux"),
        ("aarch64", "Linux", "aarch64-linux"),
        ("arm64", "Linux", "aarch64-linux"),
        ("aarch64", "Darwin", "aarch64-darwin"),
        ("arm64", "Darwin", "aarch64-darwin"),
    ],
)
def test_host_system_maps_supported_platforms(
    monkeypatch, machine, operating_system, expected
):
    monkeypatch.setattr("platform.machine", lambda: machine)
    monkeypatch.setattr("platform.system", lambda: operating_system)

    assert host_system() == expected


def test_host_system_rejects_unsupported_platform(monkeypatch):
    monkeypatch.setattr("platform.machine", lambda: "riscv64")
    monkeypatch.setattr("platform.system", lambda: "Linux")

    with pytest.raises(ManifestError, match="unsupported host platform"):
        host_system()


def test_valid_manifest_builds():
    manifest = build_manifest_from_raw(_valid_raw())

    assert [tool["name"] for tool in manifest.tools] == ["echo"]
    assert manifest.capabilities["echo"].params["message"].required is True
    # deny capability is present but not exposed as a tool.
    assert "shell_escape" in manifest.capabilities
    # systemPrompt is optional; absent means fall back to the harness default.
    assert manifest.system_prompt is None


def test_ca_bundle_is_read():
    manifest = build_manifest_from_raw(_valid_raw())

    assert manifest.ca_bundle_file == "/nix/store/cacert/etc/ssl/certs/ca-bundle.crt"


def test_missing_ca_bundle_is_rejected():
    raw = _valid_raw()
    del raw["caBundle"]

    with pytest.raises(ManifestError, match="ca_bundle_file"):
        build_manifest_from_raw(raw)


def test_non_store_ca_bundle_is_rejected():
    raw = _valid_raw()
    raw["caBundle"] = "/etc/ssl/certs/ca-certificates.crt"

    with pytest.raises(ManifestError, match="caBundle.*under /nix/store"):
        build_manifest_from_raw(raw)


@pytest.mark.parametrize(
    ("modification", "expected_match"),
    [
        (None, "shellClosure.*required"),
        ("/tmp/store-paths", "shellClosure.*under /nix/store"),
        ("/nix/store/shell-closure/paths", "shellClosure.*store-paths"),
    ],
)
def test_shell_closure_validation(modification, expected_match):
    raw = _valid_raw()
    if modification is None:
        del raw["shellClosure"]
    else:
        raw["shellClosure"] = modification

    with pytest.raises(ManifestError, match=expected_match):
        build_manifest_from_raw(raw)


def test_system_prompt_is_read_when_present():
    raw = _valid_raw()
    raw["systemPrompt"] = "You are a focused agent."

    manifest = build_manifest_from_raw(raw)

    assert manifest.system_prompt == "You are a focused agent."


def test_non_string_system_prompt_is_rejected():
    raw = _valid_raw()
    raw["systemPrompt"] = ["not", "a", "string"]

    with pytest.raises(ManifestError, match="system_prompt"):
        build_manifest_from_raw(raw)


# --- model block ------------------------------------------------------------


def test_model_block_absent_leaves_model_none():
    # The model block is optional; absent means the harness defaults apply.
    assert build_manifest_from_raw(_valid_raw()).model is None


def test_model_block_is_read_when_present():
    raw = _valid_raw()
    raw["model"] = {
        "provider": "openai-compat",
        "baseUrl": "https://opencode.ai/zen/v1",
        "name": "glm-5.2",
        "maxTokens": 15360,
        "sampling": {"temperature": 0},
    }

    model = build_manifest_from_raw(raw).model

    assert model is not None
    assert model.base_url == "https://opencode.ai/zen/v1"
    assert model.name == "glm-5.2"
    assert model.provider == "openai-compat"
    assert model.max_tokens == 15360
    assert model.sampling == {"temperature": 0}


def test_model_block_is_partial_friendly():
    # Declaring only some fields is allowed; the rest stay None for resolution.
    raw = _valid_raw()
    raw["model"] = {"name": "glm-5.2"}

    model = build_manifest_from_raw(raw).model

    assert model is not None
    assert model.name == "glm-5.2"
    assert model.base_url is None
    assert model.max_tokens is None


def test_non_object_model_block_is_rejected():
    raw = _valid_raw()
    raw["model"] = "glm-5.2"

    with pytest.raises(ManifestError, match="model"):
        build_manifest_from_raw(raw)


def test_empty_model_name_is_rejected():
    raw = _valid_raw()
    raw["model"] = {"name": ""}

    with pytest.raises(ManifestError, match="name"):
        build_manifest_from_raw(raw)


def test_non_positive_max_tokens_is_rejected():
    raw = _valid_raw()
    raw["model"] = {"maxTokens": 0}

    with pytest.raises(ManifestError, match="maxTokens"):
        build_manifest_from_raw(raw)


def test_non_numeric_sampling_value_is_rejected():
    raw = _valid_raw()
    raw["model"] = {"sampling": {"temperature": "hot"}}

    with pytest.raises(ManifestError, match="sampling"):
        build_manifest_from_raw(raw)


@pytest.mark.parametrize(
    "reserved_key",
    ["model", "max_tokens", "messages", "stream", "tools", "tool_choice"],
)
def test_reserved_sampling_key_is_rejected(reserved_key):
    raw = _valid_raw()
    raw["model"] = {"sampling": {reserved_key: 0.5}}

    with pytest.raises(
        ManifestError, match=f"model sampling key '{reserved_key}' is reserved"
    ):
        build_manifest_from_raw(raw)


def test_model_extra_headers_are_rejected():
    raw = _valid_raw()
    raw["model"] = {"extraHeaders": {"X-Test": "yes"}}

    with pytest.raises(ManifestError, match="unsupported keys: extraHeaders"):
        build_manifest_from_raw(raw)


# --- fail-closed validation -------------------------------------------------


def test_deny_capability_exposed_as_tool_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["policy"] = "deny"

    with pytest.raises(ManifestError, match="deny"):
        build_manifest_from_raw(raw)


def test_tool_without_capability_is_rejected():
    raw = _valid_raw()
    raw["tools"].append({"name": "ghost", "description": "", "parameters": {}})

    with pytest.raises(ManifestError, match="no matching capability"):
        build_manifest_from_raw(raw)


def test_invalid_policy_literal_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["policy"] = "sometimes"

    with pytest.raises(ManifestError, match="policy"):
        build_manifest_from_raw(raw)


def test_non_object_grants_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"] = "bad"

    with pytest.raises(ManifestError, match="grants.*object"):
        build_manifest_from_raw(raw)


def test_non_object_network_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["network"] = "bad"

    with pytest.raises(ManifestError, match="network.*object"):
        build_manifest_from_raw(raw)


def test_non_object_capabilities_is_rejected():
    raw = _valid_raw()
    raw["capabilities"] = ["not", "an", "object"]

    with pytest.raises(ManifestError, match="capabilities.*object"):
        build_manifest_from_raw(raw)


def test_non_string_writable_entry_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["writable"] = [1]

    with pytest.raises(ManifestError, match="writable.*valid string"):
        build_manifest_from_raw(raw)


def test_non_string_package_bin_entry_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["packageBins"] = [1]

    with pytest.raises(ManifestError, match="package_bins.*valid string"):
        build_manifest_from_raw(raw)


def test_non_string_capability_description_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["description"] = ["bad"]

    with pytest.raises(ManifestError, match="description.*valid string"):
        build_manifest_from_raw(raw)


@pytest.mark.parametrize(
    ("field_path", "bad_value", "expected_match"),
    [
        (("echo", "params"), "bad", "params.*object"),
        (("echo", "params", "message"), "bad", "params.message"),
        (("echo", "params", "message", "required"), "yes", "required.*valid boolean"),
        (("echo", "params", "message", "description"), ["bad"], "description.*valid string"),
        (("echo", "params", "message", "enum"), "red", "enum.*valid list"),
    ],
)
def test_param_shape_validation(field_path, bad_value, expected_match):
    raw = _valid_raw()
    target = raw["capabilities"]
    for key in field_path[:-1]:
        target = target[key]
    target[field_path[-1]] = bad_value

    with pytest.raises(ManifestError, match=expected_match):
        build_manifest_from_raw(raw)


def test_param_enum_entries_must_match_type():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["params"]["message"]["enum"] = ["ok", 1]

    with pytest.raises(ManifestError, match="enum entries must match"):
        build_manifest_from_raw(raw)


def test_integer_param_enum_rejects_boolean_entries():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["params"]["message"] = {
        "type": "integer",
        "description": "count",
        "required": True,
        "enum": [1, True],
    }

    with pytest.raises(ManifestError, match="enum entries must match"):
        build_manifest_from_raw(raw)


def test_runner_placeholder_without_param_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["runner"] = "echo {message} {missing}"

    with pytest.raises(ManifestError, match="undeclared param"):
        build_manifest_from_raw(raw)


def test_absolute_writable_path_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["writable"] = ["/etc"]

    with pytest.raises(ManifestError, match="must be relative"):
        build_manifest_from_raw(raw)


def test_writable_path_escaping_work_tree_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["writable"] = ["../secrets"]

    with pytest.raises(ManifestError, match="escapes the work tree"):
        build_manifest_from_raw(raw)


def test_unrestricted_under_auto_policy_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["unrestricted"] = True

    with pytest.raises(ManifestError, match="unrestricted"):
        build_manifest_from_raw(raw)


def test_relative_package_bin_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["packageBins"] = ["bin"]

    with pytest.raises(ManifestError, match="absolute path"):
        build_manifest_from_raw(raw)


def test_non_store_package_bin_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["packageBins"] = ["/usr/bin"]

    with pytest.raises(ManifestError, match="/nix/store"):
        build_manifest_from_raw(raw)


def test_package_bin_without_bin_suffix_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["packageBins"] = ["/nix/store/example"]

    with pytest.raises(ManifestError, match="/bin"):
        build_manifest_from_raw(raw)


def test_validate_realized_package_bins_fails_when_grant_env_is_incomplete(monkeypatch):
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["packageBins"] = [
        "/nix/store/example-tool/bin"
    ]
    manifest = build_manifest_from_raw(raw)

    monkeypatch.setattr("tartarus.manifest_loader.os.path.isdir", lambda _: False)

    with pytest.raises(ManifestError, match="missing directories"):
        validate_realized_package_bins(manifest)


# --- closure resolution (PLAN.md §13) ---------------------------------------


def test_malformed_closure_reference_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["closure"] = "/nix/store/x/not-store-paths"

    with pytest.raises(ManifestError, match="must end with /store-paths"):
        build_manifest_from_raw(raw)


def test_non_store_closure_reference_is_rejected():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["grants"]["closure"] = "/tmp/store-paths"

    with pytest.raises(ManifestError, match="under /nix/store"):
        build_manifest_from_raw(raw)


def test_resolve_realized_closures_reads_store_paths(tmp_path):
    shell_file = tmp_path / "shell-store-paths"
    shell_file.write_text("/nix/store/bash\n/nix/store/coreutils\n")
    grant_file = tmp_path / "grant-store-paths"
    grant_file.write_text("/nix/store/coreutils\n/nix/store/jq\n")

    # Inject temp file paths via model_copy so the read is exercised without forcing
    # the store-path shape rule (covered separately) onto a temp file.
    base = build_manifest_from_raw(_valid_raw())
    echo = base.capabilities["echo"]
    manifest = base.model_copy(
        update={
            "shell_closure_file": str(shell_file),
            "capabilities": {
                **base.capabilities,
                "echo": echo.model_copy(
                    update={
                        "grants": echo.grants.model_copy(
                            update={"closure_file": str(grant_file)}
                        )
                    }
                ),
            },
        }
    )

    resolved = resolve_realized_closures(manifest)

    assert resolved.shell_closure == ["/nix/store/bash", "/nix/store/coreutils"]
    assert resolved.capabilities["echo"].grants.closure_paths == [
        "/nix/store/coreutils",
        "/nix/store/jq",
    ]
    # A capability with no closure reference binds nothing extra (fail-safe).
    assert resolved.capabilities["shell_escape"].grants.closure_paths == []


def test_resolve_realized_closures_fails_closed_on_missing_file():
    raw = _valid_raw()
    raw["shellClosure"] = "/nix/store/absent/store-paths"
    manifest = build_manifest_from_raw(raw)

    with pytest.raises(ManifestError, match="unreadable"):
        resolve_realized_closures(manifest)


def test_resolve_realized_closures_rejects_non_store_contents(tmp_path):
    bad_file = tmp_path / "store-paths"
    bad_file.write_text("/nix/store/ok\n/etc/passwd\n")
    manifest = build_manifest_from_raw(_valid_raw()).model_copy(
        update={"shell_closure_file": str(bad_file)}
    )

    with pytest.raises(ManifestError, match="non-store path"):
        resolve_realized_closures(manifest)


def test_tool_params_inconsistent_with_capability_is_rejected():
    raw = _valid_raw()
    raw["tools"][0]["parameters"]["properties"]["extra"] = {"type": "string"}

    with pytest.raises(ManifestError, match="do not match"):
        build_manifest_from_raw(raw)


def test_non_object_tool_entry_is_rejected():
    raw = _valid_raw()
    raw["tools"][0] = "bad"

    with pytest.raises(ManifestError, match="tools.0.*valid dictionary"):
        build_manifest_from_raw(raw)


def test_non_object_tool_parameters_is_rejected():
    raw = _valid_raw()
    raw["tools"][0]["parameters"] = "bad"

    with pytest.raises(ManifestError, match="parameters.*object"):
        build_manifest_from_raw(raw)


def test_non_object_tool_schema_properties_is_rejected():
    raw = _valid_raw()
    raw["tools"][0]["parameters"]["properties"] = "bad"

    with pytest.raises(ManifestError, match="properties.*object"):
        build_manifest_from_raw(raw)


def test_non_list_tool_schema_required_is_rejected():
    raw = _valid_raw()
    raw["tools"][0]["parameters"]["required"] = "message"

    with pytest.raises(ManifestError, match="required.*list"):
        build_manifest_from_raw(raw)


def test_non_object_manifest_is_rejected():
    with pytest.raises(ManifestError):
        build_manifest_from_raw([])


# --- per-capability timeout -------------------------------------------------


def test_timeout_absent_leaves_capability_timeout_none():
    manifest = build_manifest_from_raw(_valid_raw())

    assert manifest.capabilities["echo"].timeout is None


def test_timeout_is_read_when_present():
    raw = _valid_raw()
    raw["capabilities"]["echo"]["timeout"] = 300

    manifest = build_manifest_from_raw(raw)

    assert manifest.capabilities["echo"].timeout == 300


@pytest.mark.parametrize("bad", [0, -5, True, 1.5, "30"])
def test_invalid_timeout_is_rejected(bad):
    raw = _valid_raw()
    raw["capabilities"]["echo"]["timeout"] = bad

    with pytest.raises(ManifestError, match="timeout must be a positive integer"):
        build_manifest_from_raw(raw)


# --- capability kinds -------------------------------------------------------


def _cap_body(**overrides):
    body = {"policy": "auto", "params": {}, "grants": {}, "runner": "echo hi"}
    body.update(overrides)
    return body


def test_kind_defaults_to_command():
    cap = _build_capability("c", _cap_body())
    assert cap.kind == "command"
    assert cap.control is None


def test_background_kind_with_network_is_accepted():
    cap = _build_capability(
        "fetch_bg",
        _cap_body(
            policy="ask-always",
            kind="background",
            grants={"network": {"allowedHosts": ["pypi.org:443"]}},
            runner="curl https://pypi.org",
        ),
    )
    assert cap.kind == "background"
    assert cap.grants.allowed_hosts == ["pypi.org:443"]


def test_control_kind_is_accepted_with_empty_runner_and_grants():
    cap = _build_capability(
        "bg_status",
        {
            "policy": "auto",
            "kind": "control",
            "control": "status",
            "params": {"task": {"type": "string", "description": "", "required": True}},
            "grants": {},
            "runner": "",
        },
    )
    assert cap.kind == "control"
    assert cap.control == "status"


def test_invalid_kind_is_rejected():
    with pytest.raises(ManifestError, match="kind"):
        _build_capability("c", _cap_body(kind="weird"))


def test_control_op_on_non_control_kind_is_rejected():
    with pytest.raises(ManifestError, match="only valid for kind 'control'"):
        _build_capability("c", _cap_body(control="status"))


def test_invalid_control_op_is_rejected():
    with pytest.raises(ManifestError, match="control"):
        _build_capability(
            "c", _cap_body(kind="control", control="frobnicate", runner="")
        )


def test_control_capability_with_runner_is_rejected():
    with pytest.raises(ManifestError, match="must not declare a runner"):
        _build_capability("c", _cap_body(kind="control", control="status"))


def test_control_capability_with_grants_is_rejected():
    with pytest.raises(ManifestError, match="must not declare grants"):
        _build_capability(
            "c",
            _cap_body(
                kind="control", control="status", runner="", grants={"writable": ["."]}
            ),
        )


def test_background_capability_cannot_be_unrestricted():
    with pytest.raises(ManifestError, match="cannot be unrestricted"):
        _build_capability(
            "c",
            _cap_body(
                policy="ask-always", kind="background", grants={"unrestricted": True}
            ),
        )


def test_background_capability_cannot_declare_timeout():
    with pytest.raises(ManifestError, match="cannot declare a timeout"):
        _build_capability(
            "c", _cap_body(policy="ask-always", kind="background", timeout=30)
        )


def test_shell_path_non_store_entry_is_rejected():
    raw = _valid_raw()
    raw["shellPath"] = "/nix/store/bash/bin:/usr/bin"

    with pytest.raises(ManifestError, match="under /nix/store"):
        build_manifest_from_raw(raw)


def test_shell_path_entry_without_bin_suffix_is_rejected():
    raw = _valid_raw()
    raw["shellPath"] = "/nix/store/bash/sbin"

    with pytest.raises(ManifestError, match="must end with /bin"):
        build_manifest_from_raw(raw)
