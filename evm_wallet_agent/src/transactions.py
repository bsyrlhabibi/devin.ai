"""Transaction helpers: send native, send ERC-20, approve, fee preview, status."""

from __future__ import annotations

import logging
import threading
from collections import defaultdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional, Union

from web3 import Web3

from . import storage
from .fee_manager import FeeManager
from .utils import (
    ERC20_ABI,
    TransactionError,
    get_web3,
    is_valid_address,
    load_tokens_config,
    parse_receipt,
    retry,
    to_checksum,
    to_wei,
    validate_network,
)
from .wallet import Wallet

logger = logging.getLogger(__name__)


# Nonce management for concurrent transactions. Tracks the highest nonce
# we've used per (address, network) so callers can fire off multiple
# transactions without colliding. Falls back to the on-chain
# ``getTransactionCount(pending)`` value when first seen.
_nonce_lock = threading.Lock()
_nonce_state: Dict[tuple, int] = defaultdict(int)


def _get_next_nonce(w3: Web3, address: str, network: str) -> int:
    key = (to_checksum(address), network)
    with _nonce_lock:
        on_chain = w3.eth.get_transaction_count(address, "pending")
        cached = _nonce_state.get(key, -1)
        nonce = max(on_chain, cached + 1) if cached >= 0 else on_chain
        _nonce_state[key] = nonce
        return nonce


def _reset_nonce(address: str, network: str) -> None:
    key = (to_checksum(address), network)
    with _nonce_lock:
        _nonce_state.pop(key, None)


def _resolve_wallet(
    wallet: Union[Wallet, Dict[str, Any]],
    config_dir: Optional[Path] = None,
) -> Wallet:
    if isinstance(wallet, Wallet):
        return wallet
    if isinstance(wallet, dict):
        return Wallet(
            private_key=wallet["private_key"],
            address=wallet.get("address"),
            name=wallet.get("name"),
            config_dir=config_dir,
        )
    raise TransactionError("wallet must be a Wallet instance or dict with private_key")


def _resolve_token_info(
    network: str,
    token: str,
    w3: Web3,
    config_dir: Optional[Path] = None,
) -> tuple[str, int]:
    if is_valid_address(token):
        address = to_checksum(token)
        contract = w3.eth.contract(address=address, abi=ERC20_ABI)
        try:
            decimals = contract.functions.decimals().call()
        except Exception as exc:
            raise TransactionError(
                f"Failed to fetch decimals for token {address}: {exc}"
            ) from exc
        return address, int(decimals)
    tokens_cfg = load_tokens_config(config_dir)
    if token not in tokens_cfg.get(network, {}):
        raise TransactionError(
            f"Token '{token}' is not configured for network '{network}'"
        )
    info = tokens_cfg[network][token]
    return to_checksum(info["address"]), int(info["decimals"])


def _build_base_tx(
    w3: Web3,
    network_cfg: Dict[str, Any],
    from_address: str,
    to_address: str,
    value: int,
    data: bytes = b"",
) -> Dict[str, Any]:
    return {
        "from": to_checksum(from_address),
        "to": to_checksum(to_address),
        "value": int(value),
        "data": data,
        "chainId": int(network_cfg["chain_id"]),
    }


def _finalize_and_send(
    wallet: Wallet,
    w3: Web3,
    network: str,
    tx: Dict[str, Any],
    tx_type: str,
    fee_manager: FeeManager,
    speed: str,
    gas_price: Optional[int],
    gas_limit: Optional[int],
    wallet_folder: Optional[str],
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fee_params = fee_manager.build_fee_params(
        tx, tx_type=tx_type, speed=speed, gas_price=gas_price, gas_limit=gas_limit
    )
    tx.update(fee_params)
    tx["nonce"] = _get_next_nonce(w3, wallet.address, network)
    signed = wallet.sign_transaction(tx)
    raw_tx = getattr(signed, "raw_transaction", None) or getattr(
        signed, "rawTransaction", None
    )
    if raw_tx is None:
        raise TransactionError("Signed transaction has no raw payload")
    try:
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
    except Exception as exc:
        _reset_nonce(wallet.address, network)
        raise TransactionError(f"Failed to broadcast transaction: {exc}") from exc

    tx_hash_hex = tx_hash.hex() if hasattr(tx_hash, "hex") else str(tx_hash)
    record: Dict[str, Any] = {
        "tx_hash": tx_hash_hex,
        "from": wallet.address,
        "to": tx["to"],
        "value": tx["value"],
        "chain_id": tx["chainId"],
        "network": network,
        "tx_type": tx_type,
        "nonce": tx["nonce"],
        "gas": tx["gas"],
        "speed": speed,
        "status": "pending",
    }
    if "gasPrice" in tx:
        record["gas_price"] = tx["gasPrice"]
    if "maxFeePerGas" in tx:
        record["max_fee_per_gas"] = tx["maxFeePerGas"]
        record["max_priority_fee_per_gas"] = tx["maxPriorityFeePerGas"]
    if metadata:
        record["metadata"] = metadata

    if wallet_folder and wallet.name:
        try:
            storage.append_transaction(wallet.name, record, folder=wallet_folder)
        except Exception as exc:
            logger.warning("Failed to record transaction for %s: %s", wallet.name, exc)
    return record


# ----------------------------------------------------------------------
# Public functions


def send_native(
    wallet: Union[Wallet, Dict[str, Any]],
    to_address: str,
    amount: Union[float, int, str, Decimal],
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Send the network's native currency (ETH, MATIC, BNB, ...)."""
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    if not is_valid_address(to_address):
        raise TransactionError(f"Invalid recipient address: {to_address}")
    network_cfg = validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)
    decimals = int(network_cfg.get("native_currency", {}).get("decimals", 18))
    value = to_wei(amount, decimals=decimals)
    tx = _build_base_tx(w3, network_cfg, wallet_obj.address, to_address, value)
    fee_manager = FeeManager(network, config_dir=config_dir, web3=w3)
    return _finalize_and_send(
        wallet_obj,
        w3,
        network,
        tx,
        "native",
        fee_manager,
        speed,
        gas_price,
        gas_limit,
        wallet_folder,
        metadata={"amount": str(amount)},
    )


def send_erc20(
    wallet: Union[Wallet, Dict[str, Any]],
    token_address: str,
    to_address: str,
    amount: Union[float, int, str, Decimal],
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Send an ERC-20 token. ``token_address`` may be an address or a configured symbol."""
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    if not is_valid_address(to_address):
        raise TransactionError(f"Invalid recipient address: {to_address}")
    network_cfg = validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)
    contract_address, decimals = _resolve_token_info(network, token_address, w3, config_dir)
    raw_amount = to_wei(amount, decimals=decimals)
    contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
    data = contract.encode_abi("transfer", args=[to_checksum(to_address), raw_amount])
    tx = _build_base_tx(
        w3,
        network_cfg,
        wallet_obj.address,
        contract_address,
        value=0,
        data=data,
    )
    fee_manager = FeeManager(network, config_dir=config_dir, web3=w3)
    return _finalize_and_send(
        wallet_obj,
        w3,
        network,
        tx,
        "erc20_transfer",
        fee_manager,
        speed,
        gas_price,
        gas_limit,
        wallet_folder,
        metadata={
            "token": token_address,
            "token_address": contract_address,
            "amount": str(amount),
            "to": to_checksum(to_address),
        },
    )


def approve_token(
    wallet: Union[Wallet, Dict[str, Any]],
    token_address: str,
    spender_address: str,
    amount: Union[float, int, str, Decimal],
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve a spender to transfer up to ``amount`` of an ERC-20 token."""
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    if not is_valid_address(spender_address):
        raise TransactionError(f"Invalid spender address: {spender_address}")
    network_cfg = validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)
    contract_address, decimals = _resolve_token_info(network, token_address, w3, config_dir)
    raw_amount = to_wei(amount, decimals=decimals)
    contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
    data = contract.encode_abi("approve", args=[to_checksum(spender_address), raw_amount])
    tx = _build_base_tx(
        w3,
        network_cfg,
        wallet_obj.address,
        contract_address,
        value=0,
        data=data,
    )
    fee_manager = FeeManager(network, config_dir=config_dir, web3=w3)
    return _finalize_and_send(
        wallet_obj,
        w3,
        network,
        tx,
        "erc20_approve",
        fee_manager,
        speed,
        gas_price,
        gas_limit,
        wallet_folder,
        metadata={
            "token": token_address,
            "token_address": contract_address,
            "spender": to_checksum(spender_address),
            "amount": str(amount),
        },
    )


def estimate_transaction_fee(
    wallet: Union[Wallet, Dict[str, Any]],
    to_address: str,
    amount: Union[float, int, str, Decimal],
    network: str,
    tx_type: str = "native",
    token: Optional[str] = None,
    speed: str = "medium",
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Preview the estimated fee for a transaction without sending it."""
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    network_cfg = validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)

    if tx_type == "native":
        decimals = int(network_cfg.get("native_currency", {}).get("decimals", 18))
        value = to_wei(amount, decimals=decimals)
        tx = _build_base_tx(w3, network_cfg, wallet_obj.address, to_address, value)
        effective_type = "native"
    elif tx_type == "erc20_transfer":
        if not token:
            raise TransactionError("token is required for erc20_transfer fee estimation")
        contract_address, decimals = _resolve_token_info(network, token, w3, config_dir)
        raw_amount = to_wei(amount, decimals=decimals)
        contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
        data = contract.encode_abi("transfer", args=[to_checksum(to_address), raw_amount])
        tx = _build_base_tx(
            w3, network_cfg, wallet_obj.address, contract_address, value=0, data=data
        )
        effective_type = "erc20_transfer"
    elif tx_type == "erc20_approve":
        if not token:
            raise TransactionError("token is required for erc20_approve fee estimation")
        contract_address, decimals = _resolve_token_info(network, token, w3, config_dir)
        raw_amount = to_wei(amount, decimals=decimals)
        contract = w3.eth.contract(address=contract_address, abi=ERC20_ABI)
        data = contract.encode_abi("approve", args=[to_checksum(to_address), raw_amount])
        tx = _build_base_tx(
            w3, network_cfg, wallet_obj.address, contract_address, value=0, data=data
        )
        effective_type = "erc20_approve"
    else:
        raise TransactionError(
            f"Unsupported tx_type '{tx_type}'. Use 'native', 'erc20_transfer', or 'erc20_approve'."
        )

    fee_manager = FeeManager(network, config_dir=config_dir, web3=w3)
    return fee_manager.preview_fee(tx, tx_type=effective_type, speed=speed)


@retry(attempts=3, delay=1.0, backoff=2.0)
def get_transaction_status(
    tx_hash: str,
    network: str,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return the status and parsed receipt for a transaction hash."""
    w3 = get_web3(network, config_dir)
    receipt = w3.eth.get_transaction_receipt(tx_hash)
    parsed = parse_receipt(receipt)
    if not parsed:
        return {"tx_hash": tx_hash, "status": "pending"}
    parsed["status_label"] = "success" if parsed.get("status") == 1 else "failed"
    return parsed


def wait_for_receipt(
    tx_hash: str,
    network: str,
    timeout: int = 180,
    poll_latency: float = 2.0,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Block until a transaction is mined and return its parsed receipt."""
    w3 = get_web3(network, config_dir)
    receipt = w3.eth.wait_for_transaction_receipt(
        tx_hash, timeout=timeout, poll_latency=poll_latency
    )
    parsed = parse_receipt(receipt)
    parsed["status_label"] = "success" if parsed.get("status") == 1 else "failed"
    return parsed
