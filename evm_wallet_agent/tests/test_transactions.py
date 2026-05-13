"""Tests for :mod:`src.transactions`: send/transfer/approve, fees, nonce, status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import storage, transactions as transactions_module
from src.transactions import (
    approve_token,
    estimate_transaction_fee,
    get_transaction_status,
    send_erc20,
    send_native,
    update_result_from_receipt,
)
from src.utils import TransactionError, TransactionResult
from src.wallet import Wallet

_PRIVATE_KEY = (
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
)
_RECIPIENT = "0x000000000000000000000000000000000000dEaD"


# ----------------------------------------------------------------------
# Helpers


@pytest.fixture
def wallet(test_config_dir: Path) -> Wallet:
    return Wallet.import_wallet(
        _PRIVATE_KEY, name="alice", config_dir=test_config_dir
    )


@pytest.fixture
def saved_wallet(wallet: Wallet, wallets_folder: Path, password: str) -> Wallet:
    wallet.save(password=password, folder=str(wallets_folder))
    return wallet


# ----------------------------------------------------------------------
# send_native


def test_send_native_returns_transaction_result(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.estimate_gas_value = 21_000
    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.5,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert isinstance(result, TransactionResult)
    assert result.success is True
    assert result.tx_hash and result.tx_hash.startswith("0x")
    assert result.status == "pending"
    assert result.tx_type == "native"
    assert result.from_address == wallet.address
    assert result.to_address.lower() == _RECIPIENT.lower()
    assert result.value == 5 * 10**17
    assert result.gas_limit >= 21_000


def test_send_native_persists_when_wallet_folder_given(
    saved_wallet: Wallet,
    wallets_folder: Path,
    test_config_dir: Path,
    patch_web3,
    mock_eth,
):
    send_native(
        wallet=saved_wallet,
        to_address=_RECIPIENT,
        amount=0.1,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    history = json.loads(
        (wallets_folder / "alice" / "transactions.json").read_text()
    )
    assert len(history) == 1
    assert history[0]["tx_type"] == "native"
    assert history[0]["status"] == "pending"
    assert history[0]["from"] == saved_wallet.address


def test_send_native_invalid_address_raises(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        send_native(
            wallet=wallet,
            to_address="0xnot-a-real-address",
            amount=1,
            network="test_offline",
            config_dir=test_config_dir,
        )


def test_send_native_broadcast_failure_returns_error_result(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.raise_on_send = RuntimeError("rpc rejected")
    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert isinstance(result, TransactionResult)
    assert result.success is False
    assert "rpc rejected" in (result.error or "")
    assert result.status == "error"
    assert result.tx_hash is None


def test_send_native_manual_gas_override(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline_legacy",
        config_dir=test_config_dir,
        gas_price=15_000_000_000,  # 15 Gwei
        gas_limit=21_000,
    )
    assert result.success is True
    assert result.gas_limit == 21_000
    assert result.gas_price == 15_000_000_000


# ----------------------------------------------------------------------
# send_erc20


def test_send_erc20_with_symbol(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.estimate_gas_value = 50_000
    result = send_erc20(
        wallet=wallet,
        token_address="USDT",
        to_address=_RECIPIENT,
        amount=10,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is True
    assert result.tx_type == "erc20_transfer"
    assert result.metadata["token"] == "USDT"
    # USDT decimals=6 in the test config; 10 USDT == 10_000_000 raw units.
    assert result.metadata["amount"] == "10"


def test_send_erc20_with_explicit_address(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    explicit = "0x4444444444444444444444444444444444444444"
    result = send_erc20(
        wallet=wallet,
        token_address=explicit,
        to_address=_RECIPIENT,
        amount=1,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is True
    assert result.metadata["token_address"].lower() == explicit.lower()


def test_send_erc20_unknown_symbol_raises(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        send_erc20(
            wallet=wallet,
            token_address="UNKNOWN",
            to_address=_RECIPIENT,
            amount=1,
            network="test_offline",
            config_dir=test_config_dir,
        )


# ----------------------------------------------------------------------
# approve_token


def test_approve_token_basic(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    spender = "0x5555555555555555555555555555555555555555"
    result = approve_token(
        wallet=wallet,
        token_address="USDT",
        spender_address=spender,
        amount=100,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is True
    assert result.tx_type == "erc20_approve"
    assert result.metadata["spender"].lower() == spender.lower()
    assert result.metadata["amount"] == "100"


def test_approve_token_invalid_spender_raises(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        approve_token(
            wallet=wallet,
            token_address="USDT",
            spender_address="bad-address",
            amount=1,
            network="test_offline",
            config_dir=test_config_dir,
        )


# ----------------------------------------------------------------------
# Fee preview


def test_estimate_transaction_fee_native(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.estimate_gas_value = 21_000
    preview = estimate_transaction_fee(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        tx_type="native",
        config_dir=test_config_dir,
    )
    assert preview["gas_limit"] >= 21_000
    assert preview["estimated_fee_wei"] > 0
    assert preview["fee_type"] == "eip1559"


def test_estimate_transaction_fee_erc20(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    preview = estimate_transaction_fee(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=1,
        network="test_offline",
        tx_type="erc20_transfer",
        token="USDT",
        config_dir=test_config_dir,
    )
    assert preview["fee_type"] == "eip1559"


def test_estimate_transaction_fee_unknown_type_raises(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        estimate_transaction_fee(
            wallet=wallet,
            to_address=_RECIPIENT,
            amount=1,
            network="test_offline",
            tx_type="bogus",
            config_dir=test_config_dir,
        )


# ----------------------------------------------------------------------
# Nonce management


def test_nonce_is_unique_per_send(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.nonce = 5
    r1 = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    r2 = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert r1.nonce == 5
    assert r2.nonce == 6


def test_nonce_resets_on_broadcast_error(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.nonce = 10
    mock_eth.raise_on_send = RuntimeError("rpc rejected")
    failed = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert failed.success is False

    # On recovery the next send should start fresh from the on-chain count.
    mock_eth.raise_on_send = None
    mock_eth.nonce = 10
    ok = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert ok.success is True
    assert ok.nonce == 10


# ----------------------------------------------------------------------
# Transaction status / receipts


def test_get_transaction_status_pending(
    test_config_dir: Path, patch_web3, mock_eth
):
    # No receipt configured → "pending".
    mock_eth.receipt = None
    parsed = get_transaction_status(
        "0x" + "ab" * 32, "test_offline", config_dir=test_config_dir
    )
    assert parsed["status"] == "pending"


def test_get_transaction_status_success(
    test_config_dir: Path, patch_web3, mock_eth, sample_receipt
):
    mock_eth.receipt = sample_receipt
    # parse_receipt expects a receipt with a ``transactionHash`` attribute-like key.
    # The MockEth.get_transaction_receipt returns ``sample_receipt`` as-is, but
    # parse_receipt's normalisation expects the lowercase/hex shape; map keys.
    mock_eth.receipt = {
        "transactionHash": b"\x12" * 32,
        "blockNumber": sample_receipt["block_number"],
        "blockHash": b"\xab" * 32,
        "from": sample_receipt["from"],
        "to": sample_receipt["to"],
        "gasUsed": sample_receipt["gas_used"],
        "cumulativeGasUsed": sample_receipt["cumulative_gas_used"],
        "effectiveGasPrice": sample_receipt["effective_gas_price"],
        "status": 1,
        "contractAddress": None,
    }
    parsed = get_transaction_status(
        "0x" + "12" * 32, "test_offline", config_dir=test_config_dir
    )
    assert parsed["status"] == 1
    assert parsed["status_label"] == "success"


def test_update_result_from_receipt(
    saved_wallet: Wallet,
    wallets_folder: Path,
    test_config_dir: Path,
    patch_web3,
    mock_eth,
    sample_receipt,
):
    result = send_native(
        wallet=saved_wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    assert result.gas_used is None
    assert result.fee_paid is None

    mock_eth.receipt = {
        "transactionHash": b"\x12" * 32,
        "blockNumber": 42,
        "blockHash": b"\xab" * 32,
        "from": result.from_address,
        "to": result.to_address,
        "gasUsed": 21000,
        "cumulativeGasUsed": 21000,
        "effectiveGasPrice": 30_000_000_000,
        "status": 1,
        "contractAddress": None,
    }
    updated = update_result_from_receipt(
        result,
        "test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
        wallet_name="alice",
    )
    assert updated.success is True
    assert updated.status == "success"
    assert updated.gas_used == 21000
    assert updated.fee_paid == 21000 * 30_000_000_000
    assert updated.block_number == 42

    # History reflects the success update (single row, status=success).
    history = json.loads(
        (wallets_folder / "alice" / "transactions.json").read_text()
    )
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["gas_used"] == 21000


def test_update_result_failed_receipt_marks_failed(
    wallet: Wallet,
    test_config_dir: Path,
    patch_web3,
    mock_eth,
):
    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
    )
    mock_eth.receipt = {
        "transactionHash": b"\x12" * 32,
        "blockNumber": 99,
        "blockHash": b"\xab" * 32,
        "from": result.from_address,
        "to": result.to_address,
        "gasUsed": 21000,
        "cumulativeGasUsed": 21000,
        "effectiveGasPrice": 30_000_000_000,
        "status": 0,
        "contractAddress": None,
    }
    updated = update_result_from_receipt(
        result, "test_offline", config_dir=test_config_dir
    )
    assert updated.success is False
    assert updated.status == "failed"
    assert "reverted" in (updated.error or "").lower()
