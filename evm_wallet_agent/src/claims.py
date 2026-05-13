"""Claim helpers for airdrops, staking rewards, and generic token claims.

The functions in this module follow the same stateless, agent-friendly pattern
as :mod:`transactions`. They build, sign, and broadcast a contract call against
the configured network using :class:`~src.fee_manager.FeeManager` for gas
pricing and the wallet's signing key.

Because claim contracts come in many shapes, every function accepts either:

* a pre-encoded ``calldata`` blob (``data`` parameter), for cases where the
  caller already knows the exact bytes to send, or
* an ``abi`` plus optional ``function_name`` and ``args`` to encode the call
  on the fly.

When neither is provided, the helpers fall back to the most common selector for
that kind of contract (``claim()`` for airdrops, ``getReward()`` for staking,
and the supplied function for generic token claims).

The ``check_claimable`` function performs an ``eth_call`` against a read-only
view function (``claimable``/``earned``/``balanceOf`` by default) and returns
the raw integer plus, when possible, the human-readable amount.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union

from web3 import Web3
from web3.contract import Contract

from . import storage
from .fee_manager import FeeManager
from .transactions import _finalize_and_send, _resolve_token_info, _resolve_wallet
from .utils import (
    ERC20_ABI,
    TransactionError,
    from_wei,
    get_web3,
    is_valid_address,
    to_checksum,
    validate_network,
)
from .wallet import Wallet

logger = logging.getLogger(__name__)


# Minimal ABI fragments covering the most common airdrop / staking / claim
# entry points. We try these in order when the caller doesn't supply an ABI.
_DEFAULT_CLAIM_ABI: List[Dict[str, Any]] = [
    {
        "inputs": [],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "claim",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getReward",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "getReward",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "harvest",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "claimable",
        "outputs": [{"name": "amount", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "earned",
        "outputs": [{"name": "amount", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "pendingRewards",
        "outputs": [{"name": "amount", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]


# Order in which we probe for claimable balances when the caller doesn't pick
# a specific view function. These names cover the dominant patterns used by
# airdrop and staking contracts.
_DEFAULT_CLAIMABLE_VIEW_NAMES: Sequence[str] = (
    "claimable",
    "claimableAmount",
    "claimableTokens",
    "earned",
    "pendingRewards",
    "pendingReward",
    "rewardsOf",
    "balanceOf",
)


def _build_contract(
    w3: Web3,
    contract_address: str,
    abi: Optional[Sequence[Dict[str, Any]]] = None,
) -> Contract:
    if not is_valid_address(contract_address):
        raise TransactionError(f"Invalid contract address: {contract_address}")
    return w3.eth.contract(
        address=to_checksum(contract_address),
        abi=list(abi or _DEFAULT_CLAIM_ABI),
    )


def _try_encode_claim_call(
    contract: Contract,
    candidates: Iterable[tuple[str, Sequence[Any]]],
) -> Optional[tuple[str, str, Sequence[Any]]]:
    """Try each (function_name, args) candidate and return the first that encodes.

    Returns a (function_name, encoded_calldata, args) tuple or ``None``.
    """
    for fn_name, args in candidates:
        try:
            data = contract.encode_abi(fn_name, args=list(args))
        except Exception:  # function not in ABI / args mismatch / etc.
            continue
        return fn_name, data, args
    return None


def _encode_claim_call(
    w3: Web3,
    contract_address: str,
    wallet_address: str,
    abi: Optional[Sequence[Dict[str, Any]]],
    function_name: Optional[str],
    args: Optional[Sequence[Any]],
    data: Optional[Union[str, bytes]],
    default_candidates: Sequence[tuple[str, Sequence[Any]]],
) -> tuple[str, str]:
    """Return ``(function_name, calldata_hex)`` for a claim transaction.

    Caller-provided ``data`` short-circuits everything. Otherwise we use the
    supplied ABI/function_name/args, falling back to a list of sensible
    defaults so most airdrop and staking contracts "just work".
    """
    if data is not None:
        if isinstance(data, bytes):
            hex_data = "0x" + data.hex()
        else:
            hex_data = data if data.startswith("0x") else "0x" + data
        return function_name or "raw", hex_data

    contract = _build_contract(w3, contract_address, abi)
    if function_name is not None:
        fn_args: Sequence[Any] = list(args) if args is not None else []
        result = _try_encode_claim_call(contract, [(function_name, fn_args)])
        if result is None:
            raise TransactionError(
                f"Failed to encode '{function_name}' on {contract_address}. "
                "Check the ABI and arguments."
            )
        fn_name, encoded, _ = result
        return fn_name, encoded

    result = _try_encode_claim_call(contract, default_candidates)
    if result is None:
        raise TransactionError(
            "Could not encode a default claim call against "
            f"{contract_address}. Provide an `abi` and `function_name`."
        )
    fn_name, encoded, _ = result
    return fn_name, encoded


def _claim_call(
    wallet: Union[Wallet, Dict[str, Any]],
    contract_address: str,
    network: str,
    tx_type: str,
    default_candidates: Sequence[tuple[str, Sequence[Any]]],
    speed: str,
    gas_price: Optional[int],
    gas_limit: Optional[int],
    abi: Optional[Sequence[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    args: Optional[Sequence[Any]] = None,
    data: Optional[Union[str, bytes]] = None,
    value: int = 0,
    extra_metadata: Optional[Dict[str, Any]] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    if not is_valid_address(contract_address):
        raise TransactionError(f"Invalid contract address: {contract_address}")
    network_cfg = validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)

    fn_name, calldata = _encode_claim_call(
        w3=w3,
        contract_address=contract_address,
        wallet_address=wallet_obj.address,
        abi=abi,
        function_name=function_name,
        args=args,
        data=data,
        default_candidates=default_candidates,
    )

    tx: Dict[str, Any] = {
        "from": to_checksum(wallet_obj.address),
        "to": to_checksum(contract_address),
        "value": int(value),
        "data": calldata,
        "chainId": int(network_cfg["chain_id"]),
    }

    metadata: Dict[str, Any] = {
        "contract": to_checksum(contract_address),
        "function": fn_name,
    }
    if args is not None:
        metadata["args"] = [str(a) for a in args]
    if extra_metadata:
        metadata.update(extra_metadata)

    fee_manager = FeeManager(network, config_dir=config_dir, web3=w3)
    return _finalize_and_send(
        wallet_obj,
        w3,
        network,
        tx,
        tx_type,
        fee_manager,
        speed,
        gas_price,
        gas_limit,
        wallet_folder,
        metadata=metadata,
    )


# ----------------------------------------------------------------------
# Public API


def claim_airdrop(
    wallet: Union[Wallet, Dict[str, Any]],
    contract_address: str,
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    abi: Optional[Sequence[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    args: Optional[Sequence[Any]] = None,
    data: Optional[Union[str, bytes]] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Claim an airdrop from ``contract_address``.

    By default we try ``claim()`` and ``claim(address)``. Pass an ``abi``
    and ``function_name`` (with optional ``args``) for custom contracts,
    or supply pre-encoded ``data`` to bypass ABI encoding entirely.
    """
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    defaults: Sequence[tuple[str, Sequence[Any]]] = (
        ("claim", []),
        ("claim", [to_checksum(wallet_obj.address)]),
        ("claimTokens", []),
        ("claimTokens", [to_checksum(wallet_obj.address)]),
    )
    return _claim_call(
        wallet=wallet_obj,
        contract_address=contract_address,
        network=network,
        tx_type="claim_airdrop",
        default_candidates=defaults,
        speed=speed,
        gas_price=gas_price,
        gas_limit=gas_limit,
        abi=abi,
        function_name=function_name,
        args=args,
        data=data,
        config_dir=config_dir,
        wallet_folder=wallet_folder,
    )


def claim_staking_rewards(
    wallet: Union[Wallet, Dict[str, Any]],
    contract_address: str,
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    abi: Optional[Sequence[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    args: Optional[Sequence[Any]] = None,
    data: Optional[Union[str, bytes]] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Claim staking rewards from a staking contract.

    Defaults probe ``getReward()``, ``getReward(address)``, ``harvest()`` and
    ``claimRewards()``, which together cover the majority of staking contracts
    in the wild (Synthetix-style, MasterChef-style, Curve gauges, etc.).
    """
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    defaults: Sequence[tuple[str, Sequence[Any]]] = (
        ("getReward", []),
        ("getReward", [to_checksum(wallet_obj.address)]),
        ("claimRewards", []),
        ("claimRewards", [to_checksum(wallet_obj.address)]),
        ("harvest", []),
        ("harvest", [to_checksum(wallet_obj.address)]),
    )
    return _claim_call(
        wallet=wallet_obj,
        contract_address=contract_address,
        network=network,
        tx_type="claim_staking_rewards",
        default_candidates=defaults,
        speed=speed,
        gas_price=gas_price,
        gas_limit=gas_limit,
        abi=abi,
        function_name=function_name,
        args=args,
        data=data,
        config_dir=config_dir,
        wallet_folder=wallet_folder,
    )


def claim_token(
    wallet: Union[Wallet, Dict[str, Any]],
    token_address: str,
    contract_address: str,
    network: str,
    speed: str = "medium",
    gas_price: Optional[int] = None,
    gas_limit: Optional[int] = None,
    abi: Optional[Sequence[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    args: Optional[Sequence[Any]] = None,
    data: Optional[Union[str, bytes]] = None,
    config_dir: Optional[Path] = None,
    wallet_folder: Optional[str] = None,
) -> Dict[str, Any]:
    """Generic claim function for token rewards.

    ``token_address`` identifies the ERC-20 the claim pays out in (used for
    metadata / human-readable amounts) and ``contract_address`` is the
    contract whose claim function is invoked. Defaults try ``claim()``,
    ``claim(address)``, ``claimToken(address)``, ``claimRewards()``.
    """
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    w3 = get_web3(network, config_dir)
    resolved_token, _ = _resolve_token_info(network, token_address, w3, config_dir)

    defaults: Sequence[tuple[str, Sequence[Any]]] = (
        ("claim", []),
        ("claim", [to_checksum(wallet_obj.address)]),
        ("claimToken", [resolved_token]),
        ("claimToken", [resolved_token, to_checksum(wallet_obj.address)]),
        ("claimRewards", []),
        ("claimRewards", [to_checksum(wallet_obj.address)]),
        ("getReward", []),
        ("getReward", [to_checksum(wallet_obj.address)]),
    )
    return _claim_call(
        wallet=wallet_obj,
        contract_address=contract_address,
        network=network,
        tx_type="claim_token",
        default_candidates=defaults,
        speed=speed,
        gas_price=gas_price,
        gas_limit=gas_limit,
        abi=abi,
        function_name=function_name,
        args=args,
        data=data,
        extra_metadata={
            "token": token_address,
            "token_address": resolved_token,
        },
        config_dir=config_dir,
        wallet_folder=wallet_folder,
    )


def check_claimable(
    wallet: Union[Wallet, Dict[str, Any]],
    contract_address: str,
    network: str,
    abi: Optional[Sequence[Dict[str, Any]]] = None,
    function_name: Optional[str] = None,
    args: Optional[Sequence[Any]] = None,
    token_address: Optional[str] = None,
    decimals: Optional[int] = None,
    config_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Return how much ``wallet`` can claim from ``contract_address``.

    The function performs a read-only ``eth_call``. The result dict has:

    * ``contract``: checksum address of the claim contract
    * ``account``: wallet address
    * ``function``: the view function that was successfully called
    * ``amount_wei``: raw integer balance returned
    * ``amount``: human-readable Decimal (only if ``decimals`` is known)
    * ``claimable``: ``True`` when ``amount_wei > 0``

    If ``token_address`` is given (as a symbol or address) we look up the
    decimals automatically. You can also pass ``decimals`` directly.
    """
    wallet_obj = _resolve_wallet(wallet, config_dir=config_dir)
    if not is_valid_address(contract_address):
        raise TransactionError(f"Invalid contract address: {contract_address}")
    validate_network(network, config_dir)
    w3 = get_web3(network, config_dir)
    contract = _build_contract(w3, contract_address, abi)

    fn_candidates: List[tuple[str, Sequence[Any]]]
    if function_name is not None:
        fn_args: Sequence[Any] = list(args) if args is not None else [
            to_checksum(wallet_obj.address)
        ]
        fn_candidates = [(function_name, fn_args)]
    else:
        fn_candidates = [(name, [to_checksum(wallet_obj.address)]) for name in
                         _DEFAULT_CLAIMABLE_VIEW_NAMES]

    last_error: Optional[Exception] = None
    chosen_fn: Optional[str] = None
    raw_amount: Optional[int] = None
    for fn_name, fn_args in fn_candidates:
        try:
            fn = contract.get_function_by_name(fn_name)
        except Exception:
            continue
        try:
            raw_amount = int(fn(*fn_args).call())
            chosen_fn = fn_name
            break
        except Exception as exc:
            last_error = exc
            continue

    if chosen_fn is None or raw_amount is None:
        msg = (
            f"Could not determine a claimable balance on {contract_address}. "
            "Provide an `abi` and `function_name`."
        )
        if last_error is not None:
            msg += f" Last error: {last_error}"
        raise TransactionError(msg)

    result: Dict[str, Any] = {
        "contract": to_checksum(contract_address),
        "account": wallet_obj.address,
        "network": network,
        "function": chosen_fn,
        "amount_wei": raw_amount,
        "claimable": raw_amount > 0,
    }

    resolved_decimals: Optional[int] = decimals
    if resolved_decimals is None and token_address is not None:
        try:
            _, resolved_decimals = _resolve_token_info(
                network, token_address, w3, config_dir
            )
        except Exception as exc:  # token lookup failed; that's OK
            logger.debug("Failed to resolve token decimals: %s", exc)

    if resolved_decimals is not None:
        result["decimals"] = int(resolved_decimals)
        result["amount"] = float(from_wei(raw_amount, decimals=int(resolved_decimals)))

    return result


__all__ = [
    "claim_airdrop",
    "claim_staking_rewards",
    "claim_token",
    "check_claimable",
]
