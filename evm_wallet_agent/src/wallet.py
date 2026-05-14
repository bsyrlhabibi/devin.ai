"""Wallet class: generate / import / load EVM wallets and check balances."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Union

from eth_account import Account
from web3 import Web3

from . import storage
from .utils import (
    ERC20_ABI,
    NetworkError,
    WalletError,
    from_wei,
    get_web3,
    is_valid_address,
    load_tokens_config,
    to_checksum,
    validate_network,
)

logger = logging.getLogger(__name__)


class Wallet:
    """An EVM wallet with helpers for balance queries and storage."""

    def __init__(
        self,
        private_key: str,
        address: Optional[str] = None,
        name: Optional[str] = None,
        config_dir: Optional[Path] = None,
    ) -> None:
        if not private_key:
            raise WalletError("private_key must not be empty")
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        account = Account.from_key(private_key)
        if address and to_checksum(address) != account.address:
            raise WalletError(
                "Provided address does not match the private key's derived address"
            )
        self.private_key = private_key
        self.address = account.address
        self.name = name
        self._account = account
        self._config_dir = config_dir

    # ------------------------------------------------------------------
    # Factory constructors

    @classmethod
    def generate_wallet(
        cls, name: Optional[str] = None, config_dir: Optional[Path] = None
    ) -> "Wallet":
        """Generate a brand new wallet with a random private key."""
        account = Account.create()
        return cls(
            private_key=account.key.hex(),
            address=account.address,
            name=name,
            config_dir=config_dir,
        )

    @classmethod
    def import_wallet(
        cls,
        private_key: str,
        name: Optional[str] = None,
        config_dir: Optional[Path] = None,
    ) -> "Wallet":
        """Import a wallet from an existing private key."""
        return cls(private_key=private_key, name=name, config_dir=config_dir)

    @classmethod
    def from_mnemonic(
        cls,
        mnemonic: str,
        passphrase: str = "",
        account_path: str = "m/44'/60'/0'/0/0",
        name: Optional[str] = None,
        config_dir: Optional[Path] = None,
    ) -> "Wallet":
        """Derive a wallet from a BIP-39 mnemonic."""
        Account.enable_unaudited_hdwallet_features()
        account = Account.from_mnemonic(
            mnemonic, passphrase=passphrase, account_path=account_path
        )
        return cls(
            private_key=account.key.hex(),
            address=account.address,
            name=name,
            config_dir=config_dir,
        )

    @classmethod
    def load(
        cls,
        wallet_name: str,
        password: str,
        folder: str = storage.DEFAULT_WALLETS_FOLDER,
        config_dir: Optional[Path] = None,
    ) -> "Wallet":
        """Load a previously saved wallet from disk."""
        data = storage.load_wallet(wallet_name, password, folder=folder)
        wallet = cls(
            private_key=data["private_key"],
            address=data["address"],
            name=data["name"],
            config_dir=config_dir,
        )
        return wallet

    # ------------------------------------------------------------------
    # Persistence

    def save(
        self,
        wallet_name: Optional[str] = None,
        password: str = "",
        folder: str = storage.DEFAULT_WALLETS_FOLDER,
        overwrite: bool = False,
        extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> Path:
        """Encrypt and save this wallet to disk."""
        name = wallet_name or self.name
        if not name:
            raise WalletError("A wallet_name is required to save the wallet")
        data: Dict[str, Any] = {
            "address": self.address,
            "private_key": self.private_key,
        }
        if extra_metadata:
            data.update(extra_metadata)
        path = storage.save_wallet(
            data, name, password, folder=folder, overwrite=overwrite
        )
        self.name = name
        return path

    # ------------------------------------------------------------------
    # Balances

    def get_native_balance(
        self,
        network: str,
        web3: Optional[Web3] = None,
        use_alchemy: bool = True,
    ) -> Decimal:
        """Return the native balance for this wallet on ``network`` in ether units.

        Pass ``use_alchemy=False`` to force the public RPC for this lookup.
        """
        cfg = validate_network(network, self._config_dir)
        decimals = int(cfg.get("native_currency", {}).get("decimals", 18))
        w3 = web3 or get_web3(network, self._config_dir, use_alchemy=use_alchemy)
        balance_wei = w3.eth.get_balance(self.address)
        return from_wei(balance_wei, decimals=decimals)

    def get_token_balance(
        self,
        network: str,
        token: str,
        web3: Optional[Web3] = None,
        use_alchemy: bool = True,
    ) -> Decimal:
        """Return the ERC-20 balance. ``token`` may be a symbol or an address."""
        validate_network(network, self._config_dir)
        w3 = web3 or get_web3(network, self._config_dir, use_alchemy=use_alchemy)
        token_address, decimals = self._resolve_token(network, token, w3)
        contract = w3.eth.contract(address=token_address, abi=ERC20_ABI)
        balance = contract.functions.balanceOf(self.address).call()
        return from_wei(balance, decimals=decimals)

    def get_balance(
        self,
        network: str,
        token: Optional[str] = None,
        web3: Optional[Web3] = None,
        use_alchemy: bool = True,
    ) -> Decimal:
        """Return native balance if ``token`` is None, otherwise the token balance."""
        if token is None:
            return self.get_native_balance(network, web3=web3, use_alchemy=use_alchemy)
        return self.get_token_balance(
            network, token, web3=web3, use_alchemy=use_alchemy
        )

    # ------------------------------------------------------------------
    # Helpers

    def _resolve_token(self, network: str, token: str, w3: Web3) -> tuple[str, int]:
        """Resolve a token identifier (symbol or address) into (address, decimals)."""
        if is_valid_address(token):
            address = to_checksum(token)
            contract = w3.eth.contract(address=address, abi=ERC20_ABI)
            try:
                decimals = contract.functions.decimals().call()
            except Exception as exc:
                raise WalletError(
                    f"Failed to fetch decimals for token {address}: {exc}"
                ) from exc
            return address, int(decimals)
        tokens_cfg = load_tokens_config(self._config_dir)
        network_tokens = tokens_cfg.get(network, {})
        if token not in network_tokens:
            raise NetworkError(
                f"Token '{token}' is not configured for network '{network}'"
            )
        info = network_tokens[token]
        return to_checksum(info["address"]), int(info["decimals"])

    def sign_transaction(self, transaction: Dict[str, Any]) -> Any:
        """Sign a prepared transaction dict with this wallet's private key."""
        return self._account.sign_transaction(transaction)

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return f"Wallet(address={self.address!r}, name={self.name!r})"


def generate_wallet(
    name: Optional[str] = None, config_dir: Optional[Path] = None
) -> Wallet:
    """Module-level convenience wrapper for ``Wallet.generate_wallet``."""
    return Wallet.generate_wallet(name=name, config_dir=config_dir)


def import_wallet(
    private_key: str,
    name: Optional[str] = None,
    config_dir: Optional[Path] = None,
) -> Wallet:
    """Module-level convenience wrapper for ``Wallet.import_wallet``."""
    return Wallet.import_wallet(private_key, name=name, config_dir=config_dir)


def load_wallet(
    wallet_name: str,
    password: str,
    folder: str = storage.DEFAULT_WALLETS_FOLDER,
    config_dir: Optional[Path] = None,
) -> Wallet:
    """Module-level convenience wrapper for ``Wallet.load``."""
    return Wallet.load(wallet_name, password, folder=folder, config_dir=config_dir)


WalletLike = Union[Wallet, str]
