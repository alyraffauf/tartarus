from tartarus.manifest import (
    Capability,
    Grant,
    Param,
    build_manifest,
    tool_from_capability,
)
from tests.manifest_fixtures import echo_manifest


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
                "string", "Which way.", required=True, enum=["up", "down"]
            ),
            "steps": Param("integer", "How many."),
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
        "open": Capability("open", "ok", "auto", {}, Grant(), "true"),
        "locked": Capability("locked", "no", "deny", {}, Grant(), "true"),
    }

    manifest = build_manifest(capabilities)

    tool_names = [tool["name"] for tool in manifest.tools]
    assert tool_names == ["open"]
    assert "locked" in manifest.capabilities
