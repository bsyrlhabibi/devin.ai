"""Folder-based wallet storage with AES-256-GCM encryption of private keys.

Each wallet is stored as a directory under the configured wallets folder with
the following structure:

    wallets/<wallet_name>/
        private_key.enc      # AES-256-GCM ciphertext (salt + nonce + ct)
        address.txt          # Public address (plaintext)
        config.yaml          # Wallet metadata (network preferences, etc.)
        transactions.json    # Append-only transaction history
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .utils import StorageError, TransactionResult

DEFAULT_WALLETS_FOLDER = "wallets"
_PBKDF2_ITERATIONS = 200_000
_KEY_LENGTH = 32  # AES-256
_SALT_LENGTH = 16
_NONCE_LENGTH = 12

_WALLET_NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def _validate_wallet_name(name: str) -> None:
    if not isinstance(name, str) or not _WALLET_NAME_RE.match(name):
        raise StorageError(
            f"Invalid wallet name '{name}'. Use 1-64 chars: letters, digits, '_' or '-'."
        )


def _derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_KEY_LENGTH,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def _encrypt_private_key(private_key: str, password: str) -> bytes:
    if not password:
        raise StorageError("Password must not be empty")
    salt = os.urandom(_SALT_LENGTH)
    nonce = os.urandom(_NONCE_LENGTH)
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    ciphertext = aesgcm.encrypt(nonce, private_key.encode("utf-8"), None)
    blob = {
        "version": 1,
        "kdf": "pbkdf2-hmac-sha256",
        "iterations": _PBKDF2_ITERATIONS,
        "salt": base64.b64encode(salt).decode("ascii"),
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(blob).encode("utf-8")


def _decrypt_private_key(blob: bytes, password: str) -> str:
    try:
        data = json.loads(blob.decode("utf-8"))
        salt = base64.b64decode(data["salt"])
        nonce = base64.b64decode(data["nonce"])
        ciphertext = base64.b64decode(data["ciphertext"])
    except (KeyError, ValueError, json.JSONDecodeError) as exc:
        raise StorageError(f"Corrupted wallet ciphertext: {exc}") from exc
    key = _derive_key(password, salt)
    aesgcm = AESGCM(key)
    try:
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    except Exception as exc:  # InvalidTag and friends
        raise StorageError("Failed to decrypt private key (wrong password?)") from exc
    return plaintext.decode("utf-8")


def _wallet_dir(wallet_name: str, folder: str = DEFAULT_WALLETS_FOLDER) -> Path:
    _validate_wallet_name(wallet_name)
    return Path(folder) / wallet_name


def create_wallet_folder(
    wallet_name: str,
    folder: str = DEFAULT_WALLETS_FOLDER,
    exist_ok: bool = False,
) -> Path:
    """Create the directory for a new wallet. Returns the path."""
    path = _wallet_dir(wallet_name, folder)
    if path.exists():
        if not exist_ok:
            raise StorageError(f"Wallet folder already exists: {path}")
        return path
    path.mkdir(parents=True, exist_ok=False)
    return path


def save_wallet(
    wallet_data: Dict[str, Any],
    wallet_name: str,
    password: str,
    folder: str = DEFAULT_WALLETS_FOLDER,
    overwrite: bool = False,
) -> Path:
    """Persist a wallet to disk.

    ``wallet_data`` must contain ``address`` and ``private_key``. Any other
    fields (``network``, ``label``, etc.) are stored in ``config.yaml``.
    """
    if "address" not in wallet_data or "private_key" not in wallet_data:
        raise StorageError("wallet_data must contain 'address' and 'private_key'")

    path = _wallet_dir(wallet_name, folder)
    if path.exists() and not overwrite:
        raise StorageError(f"Wallet '{wallet_name}' already exists at {path}")
    path.mkdir(parents=True, exist_ok=True)

    enc_blob = _encrypt_private_key(wallet_data["private_key"], password)
    (path / "private_key.enc").write_bytes(enc_blob)
    os.chmod(path / "private_key.enc", 0o600)

    (path / "address.txt").write_text(wallet_data["address"] + "\n", encoding="utf-8")

    metadata = {k: v for k, v in wallet_data.items() if k not in {"private_key"}}
    metadata.setdefault("created_at", datetime.now(timezone.utc).isoformat())
    metadata["name"] = wallet_name
    with (path / "config.yaml").open("w", encoding="utf-8") as f:
        yaml.safe_dump(metadata, f, sort_keys=False)

    tx_file = path / "transactions.json"
    if not tx_file.exists():
        tx_file.write_text("[]", encoding="utf-8")

    return path


def load_wallet(
    wallet_name: str,
    password: str,
    folder: str = DEFAULT_WALLETS_FOLDER,
) -> Dict[str, Any]:
    """Load and decrypt a wallet from disk."""
    path = _wallet_dir(wallet_name, folder)
    if not path.is_dir():
        raise StorageError(f"Wallet '{wallet_name}' not found at {path}")

    enc_file = path / "private_key.enc"
    addr_file = path / "address.txt"
    cfg_file = path / "config.yaml"
    if not enc_file.exists() or not addr_file.exists():
        raise StorageError(f"Wallet '{wallet_name}' is missing required files")

    private_key = _decrypt_private_key(enc_file.read_bytes(), password)
    address = addr_file.read_text(encoding="utf-8").strip()

    config: Dict[str, Any] = {}
    if cfg_file.exists():
        with cfg_file.open("r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}

    return {
        "name": wallet_name,
        "address": address,
        "private_key": private_key,
        "config": config,
        "path": str(path),
    }


def list_wallets(folder: str = DEFAULT_WALLETS_FOLDER) -> List[Dict[str, Any]]:
    """List all wallets in the folder with basic metadata."""
    base = Path(folder)
    if not base.is_dir():
        return []
    wallets: List[Dict[str, Any]] = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        addr_file = entry / "address.txt"
        cfg_file = entry / "config.yaml"
        if not addr_file.exists():
            continue
        config: Dict[str, Any] = {}
        if cfg_file.exists():
            with cfg_file.open("r", encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        wallets.append(
            {
                "name": entry.name,
                "address": addr_file.read_text(encoding="utf-8").strip(),
                "path": str(entry),
                "config": config,
            }
        )
    return wallets


def delete_wallet(wallet_name: str, folder: str = DEFAULT_WALLETS_FOLDER) -> bool:
    """Delete a wallet folder and all its contents. Returns True on success."""
    path = _wallet_dir(wallet_name, folder)
    if not path.exists():
        raise StorageError(f"Wallet '{wallet_name}' not found at {path}")
    shutil.rmtree(path)
    return True


def append_transaction(
    wallet_name: str,
    tx_record: Dict[str, Any],
    folder: str = DEFAULT_WALLETS_FOLDER,
) -> None:
    """Append a transaction record to the wallet's transactions.json file."""
    path = _wallet_dir(wallet_name, folder)
    path.mkdir(parents=True, exist_ok=True)
    tx_file = path / "transactions.json"
    history: List[Dict[str, Any]] = []
    if tx_file.exists():
        try:
            history = json.loads(tx_file.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            history = []
    tx_record.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    history.append(tx_record)
    tx_file.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


def save_transaction_result(
    wallet_name: str,
    result: "TransactionResult",
    folder: str = DEFAULT_WALLETS_FOLDER,
) -> None:
    """Persist a :class:`TransactionResult` into the wallet's history.

    Updates the existing record in place if a row with the same ``tx_hash``
    already exists (so receipts overwrite the original "pending" entry).
    """
    path = _wallet_dir(wallet_name, folder)
    path.mkdir(parents=True, exist_ok=True)
    tx_file = path / "transactions.json"
    history: List[Dict[str, Any]] = []
    if tx_file.exists():
        try:
            history = json.loads(tx_file.read_text(encoding="utf-8") or "[]")
        except json.JSONDecodeError:
            history = []
    record = result.to_dict()
    tx_hash = record.get("tx_hash")
    replaced = False
    if tx_hash:
        for idx, existing in enumerate(history):
            if existing.get("tx_hash") == tx_hash:
                history[idx] = record
                replaced = True
                break
    if not replaced:
        history.append(record)
    tx_file.write_text(json.dumps(history, indent=2, default=str), encoding="utf-8")


def read_transactions(
    wallet_name: str,
    folder: str = DEFAULT_WALLETS_FOLDER,
) -> List[Dict[str, Any]]:
    """Read the transaction history for a wallet."""
    path = _wallet_dir(wallet_name, folder)
    tx_file = path / "transactions.json"
    if not tx_file.exists():
        return []
    try:
        return json.loads(tx_file.read_text(encoding="utf-8") or "[]")
    except json.JSONDecodeError:
        return []


def wallet_exists(wallet_name: str, folder: str = DEFAULT_WALLETS_FOLDER) -> bool:
    """Return True if a wallet with this name exists in the folder."""
    try:
        return _wallet_dir(wallet_name, folder).is_dir()
    except StorageError:
        return False


def get_wallet_address(
    wallet_name: str,
    folder: str = DEFAULT_WALLETS_FOLDER,
) -> Optional[str]:
    """Return the address of a saved wallet without requiring the password."""
    path = _wallet_dir(wallet_name, folder)
    addr_file = path / "address.txt"
    if not addr_file.exists():
        return None
    return addr_file.read_text(encoding="utf-8").strip()
