"""Tests for :mod:`src.fee_manager`: gas estimation, EIP-1559, legacy, validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fee_manager import VALID_SPEEDS, FeeManager
from src.utils import FeeError, NetworkError, gwei_to_wei


# ----------------------------------------------------------------------
# Configuration parsing


def test_unknown_network_raises(test_config_dir: Path, patch_web3):
    with pytest.raises(NetworkError):
        FeeManager("does-not-exist", config_dir=test_config_dir)


def test_network_fee_types_match_config(test_config_dir: Path, patch_web3):
    eip = FeeManager("test_offline", config_dir=test_config_dir)
    legacy = FeeManager("test_offline_legacy", config_dir=test_config_dir)
    assert eip.network_fee_type() == "eip1559"
    assert legacy.network_fee_type() == "legacy"


def test_default_speed_and_multipliers(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    assert fm.default_speed() == "medium"
    assert fm.speed_multiplier("fast") > fm.speed_multiplier("slow")
    assert fm.priority_fee_gwei("urgent") > fm.priority_fee_gwei("slow")


def test_default_gas_limits(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    assert fm.default_gas_limit("native") == 21000
    assert fm.default_gas_limit("erc20_transfer") == 65000
    assert fm.default_gas_limit("erc20_approve") == 60000


# ----------------------------------------------------------------------
# Gas estimation + multiplier


def test_estimate_gas_applies_multiplier(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.estimate_gas_value = 100_000
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    estimate = fm.estimate_gas({"from": "0x0", "to": "0x0", "value": 0, "data": b""})
    assert estimate >= 110_000  # 100_000 * 1.1
    assert estimate >= mock_eth.estimate_gas_value


def test_estimate_gas_wraps_error(test_config_dir: Path, patch_web3, mock_eth):
    def boom(*_: object, **__: object) -> int:
        raise RuntimeError("rpc down")

    mock_eth.estimate_gas = boom  # type: ignore[assignment]
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    with pytest.raises(FeeError):
        fm.estimate_gas({"from": "0x0"})


# ----------------------------------------------------------------------
# Gas pricing - EIP-1559


def test_eip1559_pricing_uses_base_fee(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.base_fee_wei = gwei_to_wei(20)  # 20 Gwei base fee
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    pricing = fm.get_gas_price("medium")
    assert pricing["type"] == "eip1559"
    assert pricing["maxFeePerGas"] > pricing["maxPriorityFeePerGas"]
    # max_fee = 2 * base + priority
    assert pricing["maxFeePerGas"] >= 2 * mock_eth.base_fee_wei


def test_eip1559_pricing_respects_speed(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.base_fee_wei = gwei_to_wei(10)
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    slow = fm.get_gas_price("slow")
    fast = fm.get_gas_price("fast")
    assert fast["maxFeePerGas"] >= slow["maxFeePerGas"]
    assert fast["maxPriorityFeePerGas"] >= slow["maxPriorityFeePerGas"]


def test_eip1559_falls_back_to_legacy_without_base_fee(
    test_config_dir: Path, patch_web3, mock_eth
):
    # Force the block to lack baseFeePerGas.
    mock_eth.get_block = lambda *_args, **_kw: {}  # type: ignore[assignment]
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    pricing = fm.get_gas_price("medium")
    assert pricing["type"] == "legacy"
    assert "gasPrice" in pricing


# ----------------------------------------------------------------------
# Gas pricing - Legacy


def test_legacy_pricing_uses_eth_gas_price(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.gas_price_wei = gwei_to_wei(5)
    fm = FeeManager("test_offline_legacy", config_dir=test_config_dir)
    pricing = fm.get_gas_price("medium")
    assert pricing["type"] == "legacy"
    assert pricing["gasPrice"] == gwei_to_wei(5)


def test_legacy_pricing_speed_multipliers(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.gas_price_wei = gwei_to_wei(10)
    fm = FeeManager("test_offline_legacy", config_dir=test_config_dir)
    slow = fm.get_gas_price("slow")["gasPrice"]
    fast = fm.get_gas_price("fast")["gasPrice"]
    assert fast > slow


# ----------------------------------------------------------------------
# Validation + caps


def test_validate_gas_price_within_cap(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    assert fm.validate_gas_price(gwei_to_wei(10)) is True


def test_validate_gas_price_above_cap_raises(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    cap = fm.max_gas_price_wei()
    with pytest.raises(FeeError):
        fm.validate_gas_price(cap * 2)


def test_validate_gas_price_zero_or_negative(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    with pytest.raises(FeeError):
        fm.validate_gas_price(0)
    with pytest.raises(FeeError):
        fm.validate_gas_price(-1)


def test_cap_clamps_when_compute_exceeds(test_config_dir: Path, patch_web3, mock_eth):
    # Force an absurd base fee so the computed gas price exceeds the cap.
    mock_eth.base_fee_wei = gwei_to_wei(1_000_000)
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    pricing = fm.get_gas_price("urgent")
    assert pricing["maxFeePerGas"] <= fm.max_gas_price_wei()


# ----------------------------------------------------------------------
# calculate_fee


def test_calculate_fee_basic():
    assert FeeManager.calculate_fee(21000, 50_000_000_000) == 21000 * 50_000_000_000


def test_calculate_fee_rejects_non_positive():
    with pytest.raises(FeeError):
        FeeManager.calculate_fee(0, 1)
    with pytest.raises(FeeError):
        FeeManager.calculate_fee(1, 0)


# ----------------------------------------------------------------------
# build_fee_params + preview_fee


def test_build_fee_params_eip1559_with_estimate(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.estimate_gas_value = 50_000
    mock_eth.base_fee_wei = gwei_to_wei(5)
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    params = fm.build_fee_params({"from": "0x0", "to": "0x0", "value": 0, "data": b""})
    assert "gas" in params
    assert params["gas"] >= 50_000
    assert "maxFeePerGas" in params
    assert "maxPriorityFeePerGas" in params


def test_build_fee_params_legacy_with_override(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline_legacy", config_dir=test_config_dir)
    params = fm.build_fee_params(
        {"from": "0x0", "to": "0x0", "value": 0, "data": b""},
        gas_price=gwei_to_wei(15),
        gas_limit=80_000,
    )
    assert params["gas"] == 80_000
    assert params["gasPrice"] == gwei_to_wei(15)


def test_build_fee_params_override_above_cap_raises(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline_legacy", config_dir=test_config_dir)
    with pytest.raises(FeeError):
        fm.build_fee_params(
            {"from": "0x0", "to": "0x0", "value": 0, "data": b""},
            gas_price=gwei_to_wei(10_000),
        )


def test_preview_fee_returns_human_readable(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.estimate_gas_value = 21_000
    mock_eth.base_fee_wei = gwei_to_wei(5)
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    preview = fm.preview_fee({"from": "0x0", "to": "0x0", "value": 0, "data": b""})
    assert preview["network"] == "test_offline"
    assert preview["fee_type"] == "eip1559"
    assert preview["gas_limit"] >= 21_000
    assert preview["estimated_fee_wei"] > 0
    assert preview["estimated_fee_ether"] > 0


def test_invalid_speed_rejected(test_config_dir: Path, patch_web3):
    fm = FeeManager("test_offline", config_dir=test_config_dir)
    with pytest.raises(FeeError):
        fm.get_gas_price("warp-speed")
    assert VALID_SPEEDS == {"slow", "medium", "fast", "urgent"}
