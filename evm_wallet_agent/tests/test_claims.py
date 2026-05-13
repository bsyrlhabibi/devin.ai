"""Tests for :mod:`src.claims`: airdrop, staking, token, check_claimable."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src import storage
from src.claims import (
    check_claimable,
    claim_airdrop,
    claim_staking_rewards,
    claim_token,
)
from src.utils import TransactionError, TransactionResult
from src.wallet import Wallet

_PRIVATE_KEY = (
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
)
_CONTRACT = "0xCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCcCc"


@pytest.fixture
def wallet(test_config_dir: Path) -> Wallet:
    return Wallet.import_wallet(_PRIVATE_KEY, name="claimer", config_dir=test_config_dir)


# ----------------------------------------------------------------------
# claim_airdrop


def test_claim_airdrop_success(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    result = claim_airdrop(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert isinstance(result, TransactionResult)
    assert result.success is True
    assert result.tx_type == "claim_airdrop"
    assert result.metadata["function"] == "claim"
    assert result.to_address.lower() == _CONTRACT.lower()


def test_claim_airdrop_invalid_contract(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        claim_airdrop(
            wallet=wallet,
            contract_address="0x-invalid",
            network="test_offline",
            config_dir=test_config_dir,
        )


def test_claim_airdrop_broadcast_failure(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    mock_eth.raise_on_send = RuntimeError("execution reverted")
    result = claim_airdrop(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is False
    assert "execution reverted" in (result.error or "")


def test_claim_airdrop_custom_abi(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    custom_abi = [
        {
            "inputs": [{"name": "proof", "type": "bytes32[]"}],
            "name": "claim",
            "outputs": [],
            "stateMutability": "nonpayable",
            "type": "function",
        }
    ]
    result = claim_airdrop(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
        abi=custom_abi,
        function_name="claim",
        args=[[]],
    )
    assert result.success is True
    assert result.metadata["function"] == "claim"


def test_claim_airdrop_with_raw_data(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    result = claim_airdrop(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
        data="0xdeadbeef",
    )
    assert result.success is True


# ----------------------------------------------------------------------
# claim_staking_rewards


def test_claim_staking_rewards_default(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    result = claim_staking_rewards(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is True
    assert result.tx_type == "claim_staking_rewards"
    assert result.metadata["function"] in {"getReward", "claimRewards", "harvest"}


def test_claim_staking_with_speed_override(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    result = claim_staking_rewards(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
        speed="urgent",
    )
    assert result.speed == "urgent"


# ----------------------------------------------------------------------
# claim_token


def test_claim_token_with_symbol(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    result = claim_token(
        wallet=wallet,
        token_address="USDT",
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result.success is True
    assert result.tx_type == "claim_token"
    assert result.metadata["token"] == "USDT"
    assert "token_address" in result.metadata


def test_claim_token_persists_history(
    wallet: Wallet,
    wallets_folder: Path,
    test_config_dir: Path,
    patch_web3,
    password: str,
):
    wallet.save(password=password, folder=str(wallets_folder))
    claim_token(
        wallet=wallet,
        token_address="USDT",
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
        wallet_folder=str(wallets_folder),
    )
    history = json.loads(
        (wallets_folder / "claimer" / "transactions.json").read_text()
    )
    assert len(history) == 1
    assert history[0]["tx_type"] == "claim_token"


# ----------------------------------------------------------------------
# check_claimable


def test_check_claimable_returns_zero_for_no_balance(
    wallet: Wallet, test_config_dir: Path, patch_web3, mock_eth
):
    # Default claimable_value is None → MockContract returns 0.
    result = check_claimable(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
    )
    assert result["amount_wei"] == 0
    assert result["claimable"] is False
    assert result["account"] == wallet.address
    assert result["function"] in {
        "claimable",
        "claimableAmount",
        "earned",
        "pendingRewards",
        "balanceOf",
    }


def test_check_claimable_returns_positive(
    wallet: Wallet, test_config_dir: Path, patch_web3, monkeypatch
):
    # Patch MockWeb3's contract factory to return a MockContract with a positive
    # claimable amount.
    from tests.conftest import MockContract

    real_contract_method = None
    captured_eth = None

    def make_contract(self, address, abi):  # type: ignore[no-redef]
        contract = MockContract(address=address, abi=abi, mock_eth=self)
        contract.claimable_value = 12_500_000
        return contract

    from tests.conftest import MockEth

    monkeypatch.setattr(MockEth, "contract", make_contract)

    result = check_claimable(
        wallet=wallet,
        contract_address=_CONTRACT,
        network="test_offline",
        config_dir=test_config_dir,
        token_address="USDT",
    )
    assert result["amount_wei"] == 12_500_000
    assert result["claimable"] is True
    assert result.get("amount") == pytest.approx(12.5)


def test_check_claimable_invalid_contract(
    wallet: Wallet, test_config_dir: Path, patch_web3
):
    with pytest.raises(TransactionError):
        check_claimable(
            wallet=wallet,
            contract_address="not-an-address",
            network="test_offline",
            config_dir=test_config_dir,
        )
