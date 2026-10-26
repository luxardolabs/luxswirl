"""Agent configuration: the defaults, a YAML or JSON file with ${VAR} / $VAR
expanded from the environment, and the validation that refuses a config the
agent cannot run (a missing field, a duplicate check, an external plain-HTTP
server URL).

Expansion happens once: a value taken from the environment is used as it is,
so a secret containing `$` (a password, a token) is never expanded again.
"""

from __future__ import annotations

import json

import pytest
from app.shared.config import (
    get_config,
    load_config_file,
    validate_config,
)

pytestmark = pytest.mark.unit


def test_each_component_gets_a_copy_of_its_defaults() -> None:
    agent = get_config("agent")
    agent["interval"] = -1

    assert get_config("AGENT")["interval"] == 60
    assert get_config("server")["port"] == 9000
    with pytest.raises(ValueError, match="Unknown component"):
        get_config("collector")


def test_a_yaml_file_is_loaded_with_its_environment_expanded(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LS_HOST", "monitor.internal")
    monkeypatch.setenv("LS_KEY", "k3y")
    path = tmp_path / "agent.yml"
    path.write_text(
        "server_url: http://${LS_HOST}:9000\n"
        "auth_key: $LS_KEY\n"
        "checks:\n"
        "  - name: web\n"
        "    tags: [$LS_KEY, plain]\n"
        "interval: 30\n"
    )

    config = load_config_file(path)

    assert config["server_url"] == "http://monitor.internal:9000"
    assert config["auth_key"] == "k3y"
    assert config["checks"][0]["tags"] == ["k3y", "plain"]
    assert config["interval"] == 30


def test_a_json_file_is_loaded(tmp_path) -> None:
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"agent_name": "edge", "checks": []}))

    assert load_config_file(str(path)) == {"agent_name": "edge", "checks": []}


def test_an_environment_value_is_not_expanded_again(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password like `pa$$word` or `x$HOME` comes from the environment as it is."""
    monkeypatch.setenv("LS_DB_PASSWORD", "x$HOME$$y")
    monkeypatch.setenv("HOME", "/root")
    path = tmp_path / "agent.yml"
    path.write_text("a: ${LS_DB_PASSWORD}\nb: $LS_DB_PASSWORD\n")

    config = load_config_file(path)

    assert config == {"a": "x$HOME$$y", "b": "x$HOME$$y"}


def test_an_unset_variable_expands_to_nothing(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LS_UNSET", raising=False)
    path = tmp_path / "agent.yml"
    path.write_text("a: pre-${LS_UNSET}-post\n")

    assert load_config_file(path) == {"a": "pre--post"}


@pytest.mark.parametrize(
    ("name", "error"),
    [("missing.yml", FileNotFoundError), ("agent.toml", ValueError)],
)
def test_a_missing_or_unsupported_file_is_refused(tmp_path, name, error) -> None:
    path = tmp_path / name
    if name.endswith(".toml"):
        path.write_text("a = 1\n")
    with pytest.raises(error):
        load_config_file(path)


def _agent(**overrides):
    config = {
        "agent_name": "edge",
        "server_url": "http://luxswirl_server:9000",
        "checks": [
            {"name": "web", "check_type": "http", "target": "https://example.com"},
            {"name": "db", "check_type": "tcp", "target": "db", "port": 5432},
        ],
    }
    config.update(overrides)
    return config


def test_a_runnable_agent_config_is_valid() -> None:
    assert validate_config(_agent()) is True


@pytest.mark.parametrize(
    "config",
    [
        {"agent_name": "edge", "checks": []},  # no server_url
        _agent(server_url="http://monitor.example.com"),
        _agent(checks="web"),
        _agent(
            checks=[
                {"name": "web", "check_type": "ping", "target": "a"},
                {"name": "web", "check_type": "ping", "target": "b"},
            ]
        ),
        _agent(checks=[{"name": "web", "check_type": "ping"}]),
        _agent(checks=[{"name": "web", "check_type": "http", "target": "example.com"}]),
        _agent(checks=[{"name": "db", "check_type": "tcp", "target": "db"}]),
        _agent(
            checks=[{"name": "db", "check_type": "tcp", "target": "db", "port": 70000}]
        ),
        _agent(
            checks=[{"name": "db", "check_type": "tcp", "target": "db", "port": "22"}]
        ),
    ],
)
def test_an_agent_config_it_cannot_run_is_invalid(config, monkeypatch) -> None:
    monkeypatch.delenv("LUXSWIRL_ALLOW_INSECURE_HTTP", raising=False)
    assert validate_config(config) is False


@pytest.mark.parametrize(
    ("config", "valid"),
    [
        ({"host": "0.0.0.0", "port": 9000}, True),
        ({"host": "0.0.0.0"}, False),
        ({"host": "0.0.0.0", "port": 0}, False),
        ({"host": "0.0.0.0", "port": "9000"}, False),
    ],
)
def test_server_config_validation(config, valid) -> None:
    assert validate_config(config, "server") is valid


def test_an_unknown_component_is_invalid() -> None:
    assert validate_config({}, "collector") is False
