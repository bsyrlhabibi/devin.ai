"""Tests for :mod:`src.storage`: folder layout, AES-256-GCM encryption, history."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import storage
from src.utils import StorageError, TransactionResult


# ----------------------------------------------------------------------
# Folder + name validation


def test_create_wallet_folder_creates_directory(wallets_folder: Path):
    path = storage.create_wallet_folder("alice", folder=str(wallets_folder))
    assert path.is_dir()
    assert path == wallets_folder / "alice"


def test_create_wallet_folder_rejects_existing(wallets_folder: Path):
    storage.create_wallet_folder("alice", folder=str(wallets_folder))
    with pytest.raises(StorageError):
        storage.create_wallet_folder("alice", folder=str(wallets_folder))


def test_create_wallet_folder_allows_exist_ok(wallets_folder: Path):
    storage.create_wallet_folder("alice", folder=str(wallets_folder))
    # Should not raise.
    path = storage.create_wallet_folder(
        "alice", folder=str(wallets_folder), exist_ok=True
    )
    assert path.is_dir()


@pytest.mark.parametrize(
    "bad_name",
    ["", "../escape", "name with spaces", "very-long" * 20, "name/with/slash"],
)
def test_invalid_wallet_names_rejected(bad_name: str, wallets_folder: Path):
    with pytest.raises(StorageError):
        storage.create_wallet_folder(bad_name, folder=str(wallets_folder))


# ----------------------------------------------------------------------
# Encryption roundtrip


@pytest.fixture
def sample_wallet_data() -> dict:
    return {
        "address": "0x1111111111111111111111111111111111111111",
        "private_key": "0x" + "ab" * 32,
        "label": "Sample wallet",
        "network": "sepolia",
    }


def test_save_wallet_writes_expected_files(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    path = storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    assert path.is_dir()
    assert (path / "private_key.enc").is_file()
    assert (path / "address.txt").read_text().strip() == sample_wallet_data["address"]
    assert (path / "config.yaml").is_file()
    assert (path / "transactions.json").read_text() == "[]"


def test_save_then_load_recovers_private_key(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    loaded = storage.load_wallet("bob", password, folder=str(wallets_folder))
    assert loaded["address"] == sample_wallet_data["address"]
    assert loaded["private_key"] == sample_wallet_data["private_key"]
    assert loaded["config"]["label"] == "Sample wallet"
    assert loaded["name"] == "bob"


def test_load_with_wrong_password_raises(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    with pytest.raises(StorageError):
        storage.load_wallet("bob", "wrong-password", folder=str(wallets_folder))


def test_save_wallet_refuses_overwrite_by_default(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    with pytest.raises(StorageError):
        storage.save_wallet(
            sample_wallet_data, "bob", password, folder=str(wallets_folder)
        )


def test_save_wallet_overwrite_true(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    new_data = dict(sample_wallet_data)
    new_data["private_key"] = "0x" + "cd" * 32
    storage.save_wallet(
        new_data, "bob", password, folder=str(wallets_folder), overwrite=True
    )
    loaded = storage.load_wallet("bob", password, folder=str(wallets_folder))
    assert loaded["private_key"] == "0x" + "cd" * 32


def test_save_wallet_requires_required_fields(wallets_folder: Path, password: str):
    with pytest.raises(StorageError):
        storage.save_wallet(
            {"private_key": "0x" + "00" * 32}, "bob", password,
            folder=str(wallets_folder),
        )


def test_encrypted_blob_is_not_plaintext_key(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    raw = (wallets_folder / "bob" / "private_key.enc").read_bytes()
    assert sample_wallet_data["private_key"].encode() not in raw


# ----------------------------------------------------------------------
# Listing + deletion


def test_list_wallets_returns_metadata(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "alice", password, folder=str(wallets_folder)
    )
    second = dict(sample_wallet_data, address="0x2222222222222222222222222222222222222222")
    storage.save_wallet(second, "bob", password, folder=str(wallets_folder))
    entries = storage.list_wallets(folder=str(wallets_folder))
    names = sorted(e["name"] for e in entries)
    assert names == ["alice", "bob"]


def test_delete_wallet_removes_folder(
    wallets_folder: Path, sample_wallet_data: dict, password: str
):
    storage.save_wallet(
        sample_wallet_data, "bob", password, folder=str(wallets_folder)
    )
    storage.delete_wallet("bob", folder=str(wallets_folder))
    assert not (wallets_folder / "bob").exists()


def test_delete_missing_wallet_raises(wallets_folder: Path):
    with pytest.raises(StorageError):
        storage.delete_wallet("ghost", folder=str(wallets_folder))


# ----------------------------------------------------------------------
# Transaction history


def test_append_transaction_creates_history(wallets_folder: Path):
    storage.append_transaction(
        "alice",
        {"tx_hash": "0xabc", "status": "pending"},
        folder=str(wallets_folder),
    )
    data = json.loads((wallets_folder / "alice" / "transactions.json").read_text())
    assert len(data) == 1
    assert data[0]["tx_hash"] == "0xabc"
    assert "timestamp" in data[0]


def test_save_transaction_result_appends_then_updates(wallets_folder: Path):
    result = TransactionResult(
        success=True,
        tx_hash="0xfeedbeef",
        status="pending",
        network="test_offline",
        tx_type="native",
        from_address="0x1111111111111111111111111111111111111111",
        to_address="0x2222222222222222222222222222222222222222",
        value=10,
    )
    storage.save_transaction_result("alice", result, folder=str(wallets_folder))
    first = json.loads(
        (wallets_folder / "alice" / "transactions.json").read_text()
    )
    assert len(first) == 1
    assert first[0]["status"] == "pending"

    # Re-saving the same tx_hash updates the row in place.
    result.status = "success"
    result.gas_used = 21000
    result.fee_paid = 21000 * 30_000_000_000
    storage.save_transaction_result("alice", result, folder=str(wallets_folder))
    updated = json.loads(
        (wallets_folder / "alice" / "transactions.json").read_text()
    )
    assert len(updated) == 1
    assert updated[0]["status"] == "success"
    assert updated[0]["gas_used"] == 21000
    assert updated[0]["fee_paid"] == 21000 * 30_000_000_000


def test_read_transactions_returns_history(wallets_folder: Path):
    for i in range(3):
        storage.append_transaction(
            "alice", {"tx_hash": f"0x{i:04x}"}, folder=str(wallets_folder)
        )
    history = storage.read_transactions("alice", folder=str(wallets_folder))
    assert [tx["tx_hash"] for tx in history] == ["0x0000", "0x0001", "0x0002"]
