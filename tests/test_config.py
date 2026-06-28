import os

import pytest

from tartarus.config import (
    API_KEY_ENV_VARS,
    DEFAULT_BASE_URL,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    Config,
    ConfigError,
    load_config,
    resolve_runtime,
)
from tartarus.manifest import Manifest, ModelConfig


@pytest.fixture(autouse=True)
def _clear_harness_env(monkeypatch):
    # Config now reads ambient env (BaseSettings), so clear it for hermetic tests;
    # each test sets only the vars it exercises.
    for key in list(os.environ):
        if key.startswith("TARTARUS_") or key in API_KEY_ENV_VARS:
            monkeypatch.delenv(key, raising=False)


def _manifest(**kwargs) -> Manifest:
    return Manifest(tools=[], capabilities={}, **kwargs)


def test_load_config_reads_documented_env_values(monkeypatch, tmp_path):
    monkeypatch.setenv("TARTARUS_API_KEY", "secret")
    monkeypatch.setenv("TARTARUS_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("TARTARUS_MODEL", "test-model")
    monkeypatch.setenv("TARTARUS_MAX_TOKENS", "123")
    monkeypatch.setenv("TARTARUS_OUTPUT_TRUNCATE", "42")
    monkeypatch.setenv("TARTARUS_AGENT", "research")
    monkeypatch.setenv("TARTARUS_EXTRA_HEADERS", '{"X-Test": "yes"}')

    config = load_config()

    assert config.model == "test-model"
    assert config.max_tokens == 123
    assert config.output_truncate == 42
    assert config.agent_name == "research"
    assert config.extra_headers == {"X-Test": "yes"}
    assert config.audit_path == str(tmp_path / ".tartarus" / "audit.jsonl")
    assert config.session_dir == str(tmp_path / ".tartarus" / "sessions")


def test_session_dir_honors_explicit_override(monkeypatch, tmp_path):
    monkeypatch.setenv("TARTARUS_API_KEY", "secret")
    monkeypatch.setenv("TARTARUS_WORK_TREE", str(tmp_path))
    monkeypatch.setenv("TARTARUS_SESSIONS_DIR", "/custom/sessions")

    assert load_config().session_dir == "/custom/sessions"


def test_load_config_leaves_runtime_fields_unset_when_env_absent(monkeypatch):
    # Runtime fields are None when their env var is unset so the agent's profile
    # can supply them; defaults are applied later by resolve_runtime.
    monkeypatch.setenv("TARTARUS_API_KEY", "secret")
    for variable in (
        "TARTARUS_MODEL",
        "TARTARUS_MAX_TOKENS",
        "TARTARUS_BASE_URL",
        "TARTARUS_PROVIDER",
        "TARTARUS_OUTPUT_TRUNCATE",
        "TARTARUS_EXTRA_HEADERS",
        "TARTARUS_AGENT",
    ):
        monkeypatch.delenv(variable, raising=False)

    config = load_config()

    assert config.model is None
    assert config.max_tokens is None
    assert config.base_url is None
    assert config.provider is None
    assert config.extra_headers is None
    assert config.agent_name == "default"


def test_load_config_rejects_invalid_extra_headers(monkeypatch):
    monkeypatch.setenv("TARTARUS_API_KEY", "secret")
    monkeypatch.setenv("TARTARUS_EXTRA_HEADERS", "not-json")

    with pytest.raises(ConfigError, match="TARTARUS_EXTRA_HEADERS"):
        load_config()


def test_resolve_runtime_falls_back_to_defaults():
    # Neither env nor manifest declares anything: built-in defaults apply.
    runtime = resolve_runtime(Config(api_key="secret"), _manifest())

    assert runtime.model == DEFAULT_MODEL
    assert runtime.base_url == DEFAULT_BASE_URL
    assert runtime.max_tokens == DEFAULT_MAX_TOKENS
    assert runtime.provider_type == "openai-compat"
    assert runtime.extra_headers == {}
    assert runtime.sampling is None
    assert runtime.api_key == "secret"


def test_resolve_runtime_uses_manifest_model_over_defaults():
    manifest = _manifest(
        model=ModelConfig(
            base_url="https://agent.example/v1",
            name="agent-model",
            max_tokens=4096,
            sampling={"temperature": 0.0},
        ),
    )

    runtime = resolve_runtime(Config(api_key="secret"), manifest)

    assert runtime.base_url == "https://agent.example/v1"
    assert runtime.model == "agent-model"
    assert runtime.max_tokens == 4096
    assert runtime.extra_headers == {}
    assert runtime.sampling == {"temperature": 0.0}


def test_resolve_runtime_env_overrides_manifest_model():
    # An explicitly-set env value wins over the agent's declared model block.
    config = Config(
        api_key="secret",
        base_url="https://env.example/v1",
        model="env-model",
        max_tokens=2048,
    )
    manifest = _manifest(
        model=ModelConfig(
            base_url="https://agent.example/v1", name="agent-model", max_tokens=4096
        ),
    )

    runtime = resolve_runtime(config, manifest)

    assert runtime.base_url == "https://env.example/v1"
    assert runtime.model == "env-model"
    assert runtime.max_tokens == 2048
