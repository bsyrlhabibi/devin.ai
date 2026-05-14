"""Tests for the RPC URL selection / Alchemy fallback logic in ``src.utils``."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from src.utils import NetworkError, get_rpc_url, get_web3


@pytest.fixture
def rpc_config_dir(tmp_path: Path) -> Path:
    """Build a tiny config directory with a single network exposing every RPC
    field so each test can swap the env var without touching the real configs.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    networks = {
        "networks": {
            "demo": {
                "name": "Demo",
                "chain_id": 1,
                "native_currency": {
                    "name": "Ether",
                    "symbol": "ETH",
                    "decimals": 18,
                },
                "rpc": {
                    "alchemy": "https://demo.alchemy.example/v2/${ALCHEMY_API_KEY:-}",
                    "public": "https://demo.public.example",
                    "public_backup": "https://demo.backup.example",
                },
            },
            "public_only": {
                "name": "Public Only",
                "chain_id": 2,
                "rpc": {
                    "public": "https://only.public.example",
                    "public_backup": "https://only.backup.example",
                },
            },
            "backup_only": {
                "name": "Backup Only",
                "chain_id": 3,
                "rpc": {
                    "public": "",
                    "public_backup": "https://only-backup.example",
                },
            },
            "alchemy_unresolved": {
                "name": "Alchemy Unresolved",
                "chain_id": 4,
                "rpc": {
                    "alchemy": "https://placeholder.example/v2/${ALCHEMY_API_KEY:-}",
                    "public": "https://placeholder-public.example",
                },
            },
            "legacy": {
                "name": "Legacy flat rpc_url",
                "chain_id": 5,
                "rpc_url": "https://legacy.example",
            },
            "empty": {
                "name": "Misconfigured",
                "chain_id": 6,
            },
        }
    }

    (config_dir / "networks.yaml").write_text(
        yaml.safe_dump(networks, sort_keys=False), encoding="utf-8"
    )
    return config_dir


def test_alchemy_selected_when_key_present(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """With ALCHEMY_API_KEY set and use_alchemy=True we get the alchemy URL."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "secret-key")
    url = get_rpc_url("demo", config_dir=rpc_config_dir, use_alchemy=True)
    assert url == "https://demo.alchemy.example/v2/secret-key"


def test_falls_back_to_public_when_alchemy_key_missing(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """Without ALCHEMY_API_KEY the public URL is returned even if use_alchemy=True."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    url = get_rpc_url("demo", config_dir=rpc_config_dir, use_alchemy=True)
    assert url == "https://demo.public.example"


def test_use_alchemy_false_forces_public(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """Even with an API key, use_alchemy=False picks the public RPC."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "secret-key")
    url = get_rpc_url("demo", config_dir=rpc_config_dir, use_alchemy=False)
    assert url == "https://demo.public.example"


def test_empty_alchemy_key_treated_as_missing(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """An empty / whitespace ALCHEMY_API_KEY is treated as "not set"."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "   ")
    url = get_rpc_url("demo", config_dir=rpc_config_dir, use_alchemy=True)
    assert url == "https://demo.public.example"


def test_backup_used_when_public_is_empty(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """An empty ``public`` falls through to ``public_backup``."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    url = get_rpc_url("backup_only", config_dir=rpc_config_dir, use_alchemy=True)
    assert url == "https://only-backup.example"


def test_unresolved_placeholder_url_is_skipped(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """A URL still containing ``${...}`` is treated as unusable and skipped."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    url = get_rpc_url(
        "alchemy_unresolved", config_dir=rpc_config_dir, use_alchemy=True
    )
    # We must NOT get the alchemy URL (placeholder unresolved) — fall through
    # to the resolved public URL.
    assert url == "https://placeholder-public.example"


def test_legacy_rpc_url_still_supported(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """Legacy flat ``rpc_url`` fields remain supported for older configs."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    url = get_rpc_url("legacy", config_dir=rpc_config_dir, use_alchemy=True)
    assert url == "https://legacy.example"


def test_unknown_network_raises(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    with pytest.raises(NetworkError):
        get_rpc_url("not_a_network", config_dir=rpc_config_dir)


def test_misconfigured_network_raises(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """A network with no usable RPC entries should raise NetworkError."""
    monkeypatch.delenv("ALCHEMY_API_KEY", raising=False)
    with pytest.raises(NetworkError):
        get_rpc_url("empty", config_dir=rpc_config_dir)


def test_get_web3_uses_selected_rpc(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """``get_web3`` propagates ``use_alchemy`` and binds the resolved URL."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "another-key")
    w3_alchemy = get_web3("demo", config_dir=rpc_config_dir, use_alchemy=True)
    w3_public = get_web3("demo", config_dir=rpc_config_dir, use_alchemy=False)
    assert (
        w3_alchemy.provider.endpoint_uri  # type: ignore[union-attr]
        == "https://demo.alchemy.example/v2/another-key"
    )
    assert (
        w3_public.provider.endpoint_uri  # type: ignore[union-attr]
        == "https://demo.public.example"
    )


def test_default_use_alchemy_true(
    monkeypatch: pytest.MonkeyPatch, rpc_config_dir: Path
) -> None:
    """When no ``use_alchemy`` flag is passed the default behaviour is True."""
    monkeypatch.setenv("ALCHEMY_API_KEY", "yet-another-key")
    url = get_rpc_url("demo", config_dir=rpc_config_dir)
    assert url == "https://demo.alchemy.example/v2/yet-another-key"
