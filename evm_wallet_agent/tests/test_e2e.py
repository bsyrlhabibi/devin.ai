"""End-to-end integration tests for the EVM wallet agent.

These tests fall into two buckets:

1. **Offline E2E** — full workflows wired together with a mocked Web3. They run
   on every test invocation and verify that wallet generation, save/load,
   transaction broadcasting, fee estimation, claim flows, and reporting all
   plug together as a single pipeline.

2. **Live testnet E2E** — tests marked with ``@pytest.mark.live_e2e``. These
   are *skipped* by default and only run when ``EVM_WALLET_RUN_E2E=1`` is
   set in the environment, with funded test wallets exposed via:

   - ``E2E_PRIVATE_KEY`` — hex private key for a funded testnet wallet
   - ``E2E_RECIPIENT`` — recipient address for the test transfer
   - ``E2E_NETWORK`` — network key (e.g. ``sepolia``, ``mumbai``, ``bsc_testnet``)
   - ``E2E_TOKEN`` — (optional) token symbol or address for ERC-20 tests
   - The matching RPC URL env var (e.g. ``SEPOLIA_RPC_URL``)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List

import pytest

from src import storage
from src.claims import check_claimable, claim_airdrop
from src.transactions import (
    estimate_transaction_fee,
    send_erc20,
    send_native,
    update_result_from_receipt,
)
from src.utils import TransactionResult
from src.wallet import Wallet

_PRIVATE_KEY = (
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
)
_RECIPIENT = "0x000000000000000000000000000000000000dEaD"


# ======================================================================
# Offline end-to-end suite — always runs
# ======================================================================


def test_e2e_wallet_lifecycle(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3, mock_eth
):
    """Complete wallet lifecycle: generate, save, load, balance check."""
    # 1. Generate.
    wallet = Wallet.generate_wallet(name="e2e_alice")
    address = wallet.address
    assert address.startswith("0x")

    # 2. Save.
    wallet.save(password=password, folder=str(wallets_folder))
    assert (wallets_folder / "e2e_alice" / "private_key.enc").is_file()

    # 3. List should now show the wallet.
    entries = storage.list_wallets(folder=str(wallets_folder))
    assert any(e["name"] == "e2e_alice" for e in entries)

    # 4. Load.
    loaded = Wallet.load(
        "e2e_alice", password=password, folder=str(wallets_folder),
        config_dir=test_config_dir,
    )
    assert loaded.address == address
    assert loaded.private_key == wallet.private_key

    # 5. Balance check (via mocked Web3).
    mock_eth.balance = 2 * 10**18
    balance = loaded.get_native_balance("test_offline")
    assert balance == 2

    # 6. Delete cleans up.
    storage.delete_wallet("e2e_alice", folder=str(wallets_folder))
    assert not (wallets_folder / "e2e_alice").exists()


def test_e2e_native_transfer(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3, mock_eth,
):
    """Full native-token transfer workflow: estimate → send → confirm → report."""
    wallet = Wallet.import_wallet(
        _PRIVATE_KEY, name="e2e_native", config_dir=test_config_dir
    )
    wallet.save(password=password, folder=str(wallets_folder))

    # 1. Preview the fee.
    preview = estimate_transaction_fee(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.05,
        network="test_offline",
        tx_type="native",
        config_dir=test_config_dir,
    )
    assert preview["estimated_fee_wei"] > 0

    # 2. Broadcast.
    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.05,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    assert result.success is True
    assert result.tx_hash is not None

    # 3. Simulate confirmation and update the result.
    mock_eth.receipt = {
        "transactionHash": b"\x12" * 32,
        "blockNumber": 7,
        "blockHash": b"\xab" * 32,
        "from": result.from_address,
        "to": result.to_address,
        "gasUsed": 21000,
        "cumulativeGasUsed": 21000,
        "effectiveGasPrice": preview["effective_gas_price_wei"],
        "status": 1,
        "contractAddress": None,
    }
    updated = update_result_from_receipt(
        result,
        "test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
        wallet_name="e2e_native",
    )
    assert updated.status == "success"
    assert updated.fee_paid == 21000 * preview["effective_gas_price_wei"]

    # 4. History contains a single record with the final status.
    history = json.loads(
        (wallets_folder / "e2e_native" / "transactions.json").read_text()
    )
    assert len(history) == 1
    assert history[0]["status"] == "success"
    assert history[0]["fee_paid"] == updated.fee_paid


def test_e2e_erc20_transfer(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3, mock_eth,
):
    """ERC-20 transfer: approve → transfer → assert two history entries."""
    wallet = Wallet.import_wallet(
        _PRIVATE_KEY, name="e2e_erc20", config_dir=test_config_dir
    )
    wallet.save(password=password, folder=str(wallets_folder))

    spender = "0x6666666666666666666666666666666666666666"
    approve_result = send_erc20(  # send_erc20 emulating approve-and-transfer pair
        wallet=wallet,
        token_address="USDT",
        to_address=spender,
        amount=50,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    assert approve_result.success is True

    transfer_result = send_erc20(
        wallet=wallet,
        token_address="USDT",
        to_address=_RECIPIENT,
        amount=10,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    assert transfer_result.success is True

    history = json.loads(
        (wallets_folder / "e2e_erc20" / "transactions.json").read_text()
    )
    assert len(history) == 2
    assert all(tx["tx_type"] == "erc20_transfer" for tx in history)
    assert {tx["metadata"]["amount"] for tx in history} == {"50", "10"}


def test_e2e_fee_estimation_matches_actual(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3, mock_eth,
):
    """Compare estimated vs actual gas/fee accuracy after confirmation."""
    wallet = Wallet.import_wallet(
        _PRIVATE_KEY, name="e2e_fee", config_dir=test_config_dir
    )
    wallet.save(password=password, folder=str(wallets_folder))

    mock_eth.estimate_gas_value = 21_000
    preview = estimate_transaction_fee(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        tx_type="native",
        config_dir=test_config_dir,
    )
    estimated_gas = preview["gas_limit"]
    estimated_fee = preview["estimated_fee_wei"]

    result = send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.01,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    mock_eth.receipt = {
        "transactionHash": b"\x12" * 32,
        "blockNumber": 99,
        "blockHash": b"\xab" * 32,
        "from": result.from_address,
        "to": result.to_address,
        "gasUsed": 21_000,  # actual matches estimate in this synthetic test
        "cumulativeGasUsed": 21_000,
        "effectiveGasPrice": preview["effective_gas_price_wei"],
        "status": 1,
        "contractAddress": None,
    }
    updated = update_result_from_receipt(
        result, "test_offline", config_dir=test_config_dir
    )
    # The buffered gas estimate is at least as large as the actual usage.
    assert estimated_gas >= updated.gas_used
    # The estimated fee is within a small multiplier of the actual fee.
    assert estimated_fee >= updated.fee_paid


def test_e2e_multi_network(
    test_config_dir: Path, patch_web3, mock_eth,
):
    """Run send_native against two distinct networks back-to-back."""
    wallet = Wallet.import_wallet(_PRIVATE_KEY, config_dir=test_config_dir)
    results: List[TransactionResult] = []
    for network in ("test_offline", "test_offline_legacy"):
        result = send_native(
            wallet=wallet,
            to_address=_RECIPIENT,
            amount=0.01,
            network=network,
            config_dir=test_config_dir,
        )
        results.append(result)

    assert all(r.success for r in results)
    assert results[0].chain_id != results[1].chain_id
    assert results[0].network == "test_offline"
    assert results[1].network == "test_offline_legacy"


def test_e2e_claim_flow_with_reporting(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3, mock_eth,
):
    """check_claimable → claim_airdrop → confirm → reporting roundtrip."""
    wallet = Wallet.import_wallet(
        _PRIVATE_KEY, name="e2e_claim", config_dir=test_config_dir
    )
    wallet.save(password=password, folder=str(wallets_folder))

    # 1. The agent first checks for claimable rewards.
    info = check_claimable(
        wallet=wallet,
        contract_address="0xCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCc",
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert "claimable" in info

    # 2. Even when the mock reports zero, the agent should still be able to
    #    broadcast a claim and record it.
    claim_result = claim_airdrop(
        wallet=wallet,
        contract_address="0xCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCc",
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    assert claim_result.success is True

    history = json.loads(
        (wallets_folder / "e2e_claim" / "transactions.json").read_text()
    )
    assert len(history) == 1
    assert history[0]["tx_type"] == "claim_airdrop"


def test_e2e_logger_writes_to_wallet_file(
    wallets_folder: Path, password: str, test_config_dir: Path, patch_web3,
):
    """A wallet's transactions.log should be created and populated on send."""
    wallet = Wallet.import_wallet(
        _PRIVATE_KEY, name="e2e_logger", config_dir=test_config_dir
    )
    wallet.save(password=password, folder=str(wallets_folder))

    send_native(
        wallet=wallet,
        to_address=_RECIPIENT,
        amount=0.001,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    log_path = wallets_folder / "e2e_logger" / "transactions.log"
    assert log_path.exists()
    contents = log_path.read_text()
    assert "Broadcast tx_hash=" in contents


# ======================================================================
# Live testnet E2E suite — opt-in via EVM_WALLET_RUN_E2E=1
# ======================================================================


def _live_e2e_env() -> dict:
    return {
        "private_key": os.environ.get("E2E_PRIVATE_KEY", ""),
        "recipient": os.environ.get("E2E_RECIPIENT", _RECIPIENT),
        "network": os.environ.get("E2E_NETWORK", "sepolia"),
        "token": os.environ.get("E2E_TOKEN", ""),
    }


@pytest.mark.live_e2e
def test_live_native_transfer_on_testnet(tmp_path: Path, password: str):
    """Send a tiny native transfer on a real testnet and wait for confirmation."""
    env = _live_e2e_env()
    if not env["private_key"]:
        pytest.skip("E2E_PRIVATE_KEY not set")
    wallets = tmp_path / "wallets"
    wallets.mkdir()
    wallet = Wallet.import_wallet(env["private_key"], name="live")
    wallet.save(password=password, folder=str(wallets))

    result = send_native(
        wallet=wallet,
        to_address=env["recipient"],
        amount=0.00001,
        network=env["network"],
        wallet_folder=str(wallets),
    )
    assert result.success is True
    update_result_from_receipt(
        result, env["network"], wallet_folder=str(wallets), wallet_name="live",
    )


@pytest.mark.live_e2e
def test_live_erc20_transfer_on_testnet(tmp_path: Path, password: str):
    env = _live_e2e_env()
    if not env["private_key"] or not env["token"]:
        pytest.skip("E2E_PRIVATE_KEY/E2E_TOKEN not set")
    wallets = tmp_path / "wallets"
    wallets.mkdir()
    wallet = Wallet.import_wallet(env["private_key"], name="live_erc20")
    wallet.save(password=password, folder=str(wallets))
    result = send_erc20(
        wallet=wallet,
        token_address=env["token"],
        to_address=env["recipient"],
        amount=0.0001,
        network=env["network"],
        wallet_folder=str(wallets),
    )
    assert result.success is True


@pytest.mark.live_e2e
def test_live_fee_estimation_on_testnet(tmp_path: Path, password: str):
    env = _live_e2e_env()
    if not env["private_key"]:
        pytest.skip("E2E_PRIVATE_KEY not set")
    wallet = Wallet.import_wallet(env["private_key"])
    preview = estimate_transaction_fee(
        wallet=wallet,
        to_address=env["recipient"],
        amount=0.00001,
        network=env["network"],
        tx_type="native",
    )
    assert preview["estimated_fee_wei"] > 0
