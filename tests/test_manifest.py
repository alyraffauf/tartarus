from tartarus.manifest import (
    Capability,
    Grant,
    Param,
    _RESERVED_SHELL_ENV_NAMES,
    build_manifest,
    tool_from_capability,
)
from tests.manifest_fixtures import echo_manifest


def test_reserved_shell_env_names_canonical():
    # Pins the Python side of the reserved-name set. lib/agents.nix
    # (shellEnvReservedNames) mirrors this list; if you change one, change both.
    assert _RESERVED_SHELL_ENV_NAMES == frozenset(
        {
            "BASH_ENV",
            "CURL_CA_BUNDLE",
            "HOME",
            "LANG",
            "LC_ALL",
            "NIX_SSL_CERT_FILE",
            "PATH",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
        }
    )


def test_echo_fixture_manifest_exposes_echo_tool():
    manifest = echo_manifest()

    assert [tool["name"] for tool in manifest.tools] == ["echo"]
    assert "echo" in manifest.capabilities
    assert manifest.capabilities["echo"].policy == "auto"


def test_tool_projection_builds_json_schema():
    capability = Capability(
        name="demo",
        description="A demo capability.",
        policy="auto",
        params={
            "direction": Param(
                type="string",
                description="Which way.",
                required=True,
                enum=["up", "down"],
            ),
            "steps": Param(type="integer", description="How many."),
        },
        grants=Grant(),
        runner="demo {direction}",
    )

    tool = tool_from_capability(capability)

    assert tool["name"] == "demo"
    assert tool["parameters"]["required"] == ["direction"]
    assert tool["parameters"]["properties"]["direction"]["enum"] == ["up", "down"]
    assert tool["parameters"]["properties"]["steps"]["type"] == "integer"


def test_deny_capabilities_are_not_projected_into_tools():
    capabilities = {
        "open": Capability(
            name="open",
            description="ok",
            policy="auto",
            params={},
            grants=Grant(),
            runner="true",
        ),
        "locked": Capability(
            name="locked",
            description="no",
            policy="deny",
            params={},
            grants=Grant(),
            runner="true",
        ),
    }

    manifest = build_manifest(capabilities)

    tool_names = [tool["name"] for tool in manifest.tools]
    assert tool_names == ["open"]
    assert "locked" in manifest.capabilities
