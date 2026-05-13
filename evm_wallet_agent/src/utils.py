"""Utility helpers: config loading, conversions, retries, network validation."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar

import yaml
from web3 import Web3

logger = logging.getLogger(__name__)

T = TypeVar("T")

_ENV_VAR_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


class WalletError(Exception):
    """Base exception for wallet agent."""


class NetworkError(WalletError):
    """Raised when a network is not configured or unreachable."""


class TransactionError(WalletError):
    """Raised when a transaction fails to build, sign, or send."""


class StorageError(WalletError):
    """Raised on storage / encryption errors."""


class FeeError(WalletError):
    """Raised when fee estimation or validation fails."""


def _utcnow_isoformat() -> str:
    """Return a UTC ISO-8601 timestamp suitable for logs and JSON storage."""
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TransactionResult:
    """Unified result for all wallet-agent transaction-like operations.

    Every send / approve / claim function returns one of these so callers
    (especially LLM-driven agents) can treat the result format uniformly.

    Required fields:
        success: ``True`` when the transaction was broadcast (or, for
            confirmed transactions, when the receipt status is ``1``).
        tx_hash: Hex-encoded transaction hash (``None`` if broadcast failed).
        error: Human-readable error message when ``success`` is ``False``.
        gas_used: Actual gas consumed (only filled after a receipt is
            available; ``None`` while pending).
        fee_paid: Actual fee paid in wei (``gas_used * effective_gas_price``
            when available).
        timestamp: UTC ISO-8601 timestamp the result was created.

    Additional diagnostic fields are stored under ``metadata`` and ``params``
    so downstream code (logging, reporting, replays) has everything it needs.
    """

    success: bool
    tx_hash: Optional[str] = None
    error: Optional[str] = None
    gas_used: Optional[int] = None
    fee_paid: Optional[int] = None
    timestamp: str = field(default_factory=_utcnow_isoformat)

    # Diagnostics & reporting fields
    network: Optional[str] = None
    tx_type: Optional[str] = None
    status: str = "pending"  # one of: pending, success, failed, error
    from_address: Optional[str] = None
    to_address: Optional[str] = None
    value: Optional[int] = None
    nonce: Optional[int] = None
    gas_limit: Optional[int] = None
    gas_price: Optional[int] = None
    max_fee_per_gas: Optional[int] = None
    max_priority_fee_per_gas: Optional[int] = None
    effective_gas_price: Optional[int] = None
    chain_id: Optional[int] = None
    speed: Optional[str] = None
    block_number: Optional[int] = None
    receipt: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Mapping-style access for backwards compatibility with code/tests
    # that index TransactionResult like a dict.

    def __getitem__(self, key: str) -> Any:
        if key == "from":
            return self.from_address
        if key == "to":
            return self.to_address
        try:
            return getattr(self, key)
        except AttributeError as exc:
            raise KeyError(key) from exc

    def __contains__(self, key: object) -> bool:
        if not isinstance(key, str):
            return False
        if key in {"from", "to"}:
            return True
        return hasattr(self, key)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain-dict representation suitable for JSON storage."""
        data = asdict(self)
        # Aliases that match the older record format used by storage.
        data["from"] = self.from_address
        data["to"] = self.to_address
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TransactionResult":
        """Rebuild a TransactionResult from a JSON-friendly dict."""
        if not isinstance(data, dict):
            raise TypeError("TransactionResult.from_dict expects a dict")
        known: Dict[str, Any] = {}
        for f in cls.__dataclass_fields__:
            if f in data:
                known[f] = data[f]
        if "from_address" not in known and "from" in data:
            known["from_address"] = data["from"]
        if "to_address" not in known and "to" in data:
            known["to_address"] = data["to"]
        return cls(**known)


def _expand_env_vars(value: Any) -> Any:
    """Recursively expand ${VAR} and ${VAR:-default} placeholders in YAML values."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            var_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(var_name, default)

        return _ENV_VAR_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env_vars(v) for v in value]
    return value


def load_yaml_config(filename: str, config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load a YAML config file from the config directory with env-var expansion."""
    directory = Path(config_dir) if config_dir else _CONFIG_DIR
    path = directory / filename
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return _expand_env_vars(data)


def load_networks_config(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load the networks.yaml config."""
    return load_yaml_config("networks.yaml", config_dir).get("networks", {})


def load_tokens_config(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load the tokens.yaml config."""
    return load_yaml_config("tokens.yaml", config_dir).get("tokens", {})


def load_fee_config(config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Load the fee_config.yaml config."""
    return load_yaml_config("fee_config.yaml", config_dir).get("fee_settings", {})


def validate_network(network: str, config_dir: Optional[Path] = None) -> Dict[str, Any]:
    """Validate that a network is configured and return its config."""
    networks = load_networks_config(config_dir)
    if network not in networks:
        raise NetworkError(
            f"Unknown network '{network}'. Available: {sorted(networks.keys())}"
        )
    return networks[network]


def get_web3(network: str, config_dir: Optional[Path] = None) -> Web3:
    """Return a Web3 instance connected to the given network."""
    cfg = validate_network(network, config_dir)
    rpc_url = cfg.get("rpc_url")
    if not rpc_url:
        raise NetworkError(f"No RPC URL configured for network '{network}'")
    provider = Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30})
    w3 = Web3(provider)
    return w3


def to_checksum(address: str) -> str:
    """Return the EIP-55 checksum form of an address."""
    return Web3.to_checksum_address(address)


def is_valid_address(address: str) -> bool:
    """Check if a string is a valid Ethereum address."""
    try:
        Web3.to_checksum_address(address)
        return True
    except (ValueError, TypeError):
        return False


def to_wei(amount: float | int | str | Decimal, decimals: int = 18) -> int:
    """Convert a human-readable amount to wei-equivalent integer units."""
    if isinstance(amount, str):
        amount = Decimal(amount)
    elif isinstance(amount, float):
        amount = Decimal(str(amount))
    elif isinstance(amount, int):
        amount = Decimal(amount)
    return int(amount * (Decimal(10) ** decimals))


def from_wei(amount: int, decimals: int = 18) -> Decimal:
    """Convert a wei-equivalent integer to a human-readable Decimal."""
    return Decimal(amount) / (Decimal(10) ** decimals)


def gwei_to_wei(gwei: float | int | Decimal) -> int:
    """Convert Gwei to wei."""
    return int(Decimal(str(gwei)) * Decimal(10**9))


def wei_to_gwei(wei: int) -> Decimal:
    """Convert wei to Gwei."""
    return Decimal(wei) / Decimal(10**9)


def parse_receipt(receipt: Any) -> Dict[str, Any]:
    """Parse a web3 transaction receipt into a plain dict."""
    if receipt is None:
        return {}
    return {
        "transaction_hash": receipt["transactionHash"].hex()
        if hasattr(receipt["transactionHash"], "hex")
        else str(receipt["transactionHash"]),
        "block_number": receipt.get("blockNumber"),
        "block_hash": receipt["blockHash"].hex()
        if receipt.get("blockHash") and hasattr(receipt["blockHash"], "hex")
        else None,
        "from": receipt.get("from"),
        "to": receipt.get("to"),
        "gas_used": receipt.get("gasUsed"),
        "cumulative_gas_used": receipt.get("cumulativeGasUsed"),
        "effective_gas_price": receipt.get("effectiveGasPrice"),
        "status": receipt.get("status"),
        "contract_address": receipt.get("contractAddress"),
    }


def retry(
    attempts: int = 3,
    delay: float = 1.0,
    backoff: float = 2.0,
    exceptions: tuple = (Exception,),
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Retry decorator with exponential backoff."""

    def decorator(func: Callable[..., T]) -> Callable[..., T]:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> T:
            current_delay = delay
            last_exc: Optional[BaseException] = None
            for attempt in range(1, attempts + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == attempts:
                        break
                    logger.warning(
                        "Attempt %d/%d for %s failed: %s. Retrying in %.2fs",
                        attempt,
                        attempts,
                        func.__name__,
                        exc,
                        current_delay,
                    )
                    time.sleep(current_delay)
                    current_delay *= backoff
            assert last_exc is not None
            raise last_exc

        return wrapper

    return decorator


# Minimal ERC-20 ABI sufficient for transfer / approve / balanceOf / metadata.
ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "decimals",
        "outputs": [{"name": "", "type": "uint8"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "symbol",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [],
        "name": "name",
        "outputs": [{"name": "", "type": "string"}],
        "type": "function",
    },
]
