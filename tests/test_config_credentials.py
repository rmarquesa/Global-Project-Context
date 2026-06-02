from __future__ import annotations

import pytest

import gpc.config as config


@pytest.fixture
def restore_config():
    saved = {
        name: getattr(config, name)
        for name in (
            "POSTGRES_DSN",
            "NEO4J_URI",
            "NEO4J_PASSWORD",
            "STRICT_CREDENTIALS",
        )
    }
    yield
    for name, value in saved.items():
        setattr(config, name, value)


def test_no_warnings_for_local_defaults(restore_config, monkeypatch) -> None:
    monkeypatch.setattr(config, "POSTGRES_DSN", config.DEFAULT_POSTGRES_DSN)
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://localhost:7687")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", config.DEFAULT_NEO4J_PASSWORD)
    assert config.insecure_credential_warnings() == []


def test_warns_when_default_neo4j_password_on_remote_host(
    restore_config, monkeypatch
) -> None:
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://10.0.0.5:7687")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", config.DEFAULT_NEO4J_PASSWORD)
    warnings = config.insecure_credential_warnings()
    assert any("Neo4j" in w for w in warnings)


def test_warns_when_default_postgres_password_on_remote_host(
    restore_config, monkeypatch
) -> None:
    monkeypatch.setattr(
        config, "POSTGRES_DSN", "postgresql://gpc:gpcpass@db.example.com:5432/gpc"
    )
    warnings = config.insecure_credential_warnings()
    assert any("Postgres" in w for w in warnings)


def test_no_warning_when_remote_host_uses_custom_password(
    restore_config, monkeypatch
) -> None:
    monkeypatch.setattr(
        config, "POSTGRES_DSN", "postgresql://gpc:s3cret@db.example.com:5432/gpc"
    )
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://10.0.0.5:7687")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", "another-secret")
    assert config.insecure_credential_warnings() == []


def test_enforce_raises_in_strict_mode(restore_config, monkeypatch) -> None:
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://10.0.0.5:7687")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", config.DEFAULT_NEO4J_PASSWORD)
    monkeypatch.setattr(config, "STRICT_CREDENTIALS", True)
    with pytest.raises(RuntimeError):
        config.enforce_credentials()


def test_enforce_only_warns_when_not_strict(
    restore_config, monkeypatch, capsys
) -> None:
    monkeypatch.setattr(config, "NEO4J_URI", "bolt://10.0.0.5:7687")
    monkeypatch.setattr(config, "NEO4J_PASSWORD", config.DEFAULT_NEO4J_PASSWORD)
    monkeypatch.setattr(config, "STRICT_CREDENTIALS", False)
    warnings = config.enforce_credentials()
    assert warnings
    assert "WARNING" in capsys.readouterr().err
