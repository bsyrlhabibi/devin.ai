"""Tests for :mod:`src.wallet`: generation, import, mnemonic, balances."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from eth_account import Account

from src import storage
from src.utils import WalletError
from src.wallet import (
    Wallet,
    generate_wallet,
    import_wallet,
    load_wallet,
)

# Fixed test private key (well-known, never used on mainnet).
_TEST_PRIVATE_KEY = (
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318"
)
_TEST_ADDRESS = Account.from_key(_TEST_PRIVATE_KEY).address


# ----------------------------------------------------------------------
# Generation & import


def test_generate_wallet_produces_valid_address():
    wallet = Wallet.generate_wallet(name="alice")
    assert wallet.address.startswith("0x")
    assert len(wallet.address) == 42
    # Address must derive from the private key.
    assert Account.from_key(wallet.private_key).address == wallet.address
    assert wallet.name == "alice"


def test_module_level_generate_wallet():
    w1 = generate_wallet()
    w2 = generate_wallet()
    assert w1.private_key != w2.private_key
    assert w1.address != w2.address


def test_import_wallet_accepts_known_key():
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, name="bob")
    assert wallet.address == _TEST_ADDRESS
    assert wallet.private_key.startswith("0x")


def test_import_wallet_module_shortcut():
    assert import_wallet(_TEST_PRIVATE_KEY).address == _TEST_ADDRESS


def test_import_wallet_handles_unprefixed_key():
    raw = _TEST_PRIVATE_KEY[2:]
    wallet = Wallet.import_wallet(raw)
    assert wallet.address == _TEST_ADDRESS
    assert wallet.private_key.startswith("0x")


def test_wallet_requires_non_empty_key():
    with pytest.raises(WalletError):
        Wallet(private_key="")


def test_wallet_rejects_mismatched_address():
    other = Wallet.generate_wallet()
    with pytest.raises(WalletError):
        Wallet(private_key=_TEST_PRIVATE_KEY, address=other.address)


def test_from_mnemonic_is_deterministic():
    mnemonic = "test test test test test test test test test test test junk"
    w1 = Wallet.from_mnemonic(mnemonic)
    w2 = Wallet.from_mnemonic(mnemonic)
    assert w1.address == w2.address


# ----------------------------------------------------------------------
# Persistence: generate → save → load


def test_save_and_load_wallet_roundtrip(wallets_folder: Path, password: str):
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, name="charlie")
    wallet.save(password=password, folder=str(wallets_folder))

    saved_dir = wallets_folder / "charlie"
    assert (saved_dir / "private_key.enc").is_file()
    assert (saved_dir / "address.txt").read_text().strip() == _TEST_ADDRESS
    assert (saved_dir / "config.yaml").is_file()
    assert (saved_dir / "transactions.json").read_text().strip() == "[]"

    reloaded = load_wallet("charlie", password, folder=str(wallets_folder))
    assert reloaded.address == _TEST_ADDRESS
    assert reloaded.private_key == _TEST_PRIVATE_KEY
    assert reloaded.name == "charlie"


def test_save_wallet_requires_name(password: str, wallets_folder: Path):
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY)
    with pytest.raises(WalletError):
        wallet.save(password=password, folder=str(wallets_folder))


def test_load_with_wrong_password_raises(wallets_folder: Path, password: str):
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, name="dave")
    wallet.save(password=password, folder=str(wallets_folder))
    with pytest.raises(Exception):
        Wallet.load("dave", "wrong-password", folder=str(wallets_folder))


# ----------------------------------------------------------------------
# Balances (via mocked web3)


def test_get_native_balance_uses_web3(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.balance = 5 * 10**18
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, config_dir=test_config_dir)
    balance = wallet.get_native_balance("test_offline")
    assert balance == Decimal("5")


def test_get_balance_with_token_symbol(test_config_dir: Path, patch_web3, mock_eth):
    mock_eth.balance = 0
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, config_dir=test_config_dir)
    # The mock contract returns balance_of_value (1_000_000 by default) regardless
    # of which token is queried — exercise the lookup path via the config.
    balance = wallet.get_balance(network="test_offline", token="USDT")
    # USDT in the test config has 6 decimals, so 1_000_000 raw == 1.0 USDT.
    assert balance == Decimal("1")


def test_get_balance_with_token_address(test_config_dir: Path, patch_web3, mock_eth):
    wallet = Wallet.import_wallet(_TEST_PRIVATE_KEY, config_dir=test_config_dir)
    explicit_address = "0x4444444444444444444444444444444444444444"
    balance = wallet.get_balance(network="test_offline", token=explicit_address)
    # By default the mock contract uses 18 decimals when queried by address.
    assert isinstance(balance, Decimal)
