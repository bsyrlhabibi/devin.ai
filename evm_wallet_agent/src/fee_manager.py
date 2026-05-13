"""Fee management: gas estimation, EIP-1559 / legacy gas pricing, validation."""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

from web3 import Web3

from .utils import (
    FeeError,
    NetworkError,
    get_web3,
    gwei_to_wei,
    load_fee_config,
    load_networks_config,
    wei_to_gwei,
)

logger = logging.getLogger(__name__)

VALID_SPEEDS = {"slow", "medium", "fast", "urgent"}


class FeeManager:
    """Encapsulates gas estimation and gas price selection for a network."""

    def __init__(
        self,
        network: str,
        config_dir: Optional[Path] = None,
        web3: Optional[Web3] = None,
    ) -> None:
        self.network = network
        self._config_dir = config_dir
        self.networks = load_networks_config(config_dir)
        if network not in self.networks:
            raise NetworkError(
                f"Unknown network '{network}'. Available: {sorted(self.networks.keys())}"
            )
        self.fee_settings = load_fee_config(config_dir)
        self.web3 = web3 or get_web3(network, config_dir)

    # ------------------------------------------------------------------
    # Public helpers

    def network_fee_type(self) -> str:
        """Return ``'eip1559'`` or ``'legacy'`` for the active network."""
        ns = self.fee_settings.get("network_specific", {}).get(self.network, {})
        return ns.get("type", "eip1559")

    def gas_multiplier(self) -> Decimal:
        return Decimal(str(self.fee_settings.get("gas_multiplier", 1.1)))

    def max_gas_price_gwei(self) -> Decimal:
        ns = self.fee_settings.get("network_specific", {}).get(self.network, {})
        return Decimal(str(ns.get("max_gas_price", self.fee_settings.get("max_gas_price", 100))))

    def max_gas_price_wei(self) -> int:
        return gwei_to_wei(self.max_gas_price_gwei())

    def priority_fee_gwei(self, speed: str) -> Decimal:
        speed = self._normalize_speed(speed)
        priority = self.fee_settings.get("priority_fee", {})
        return Decimal(str(priority.get(speed, priority.get("medium", 2))))

    def speed_multiplier(self, speed: str) -> Decimal:
        speed = self._normalize_speed(speed)
        multipliers = self.fee_settings.get("speed_multipliers", {})
        return Decimal(str(multipliers.get(speed, 1.0)))

    def default_speed(self) -> str:
        return self._normalize_speed(self.fee_settings.get("default_speed", "medium"))

    def default_gas_limit(self, tx_type: str) -> int:
        defaults = self.fee_settings.get("default_gas_limits", {})
        mapping = {
            "native": defaults.get("native_transfer", 21000),
            "erc20_transfer": defaults.get("erc20_transfer", 65000),
            "erc20_approve": defaults.get("erc20_approve", 60000),
        }
        return int(mapping.get(tx_type, 21000))

    # ------------------------------------------------------------------
    # Gas estimation

    def estimate_gas(self, transaction: Dict[str, Any]) -> int:
        """Estimate gas for a transaction and apply the configured multiplier."""
        try:
            estimate = self.web3.eth.estimate_gas(transaction)
        except Exception as exc:  # web3 exceptions are too varied to enumerate
            raise FeeError(f"Failed to estimate gas: {exc}") from exc
        buffered = int(Decimal(estimate) * self.gas_multiplier())
        return max(buffered, estimate)

    # ------------------------------------------------------------------
    # Gas pricing

    def get_gas_price(self, speed: Optional[str] = None) -> Dict[str, Any]:
        """Return a dict describing the gas price to use.

        EIP-1559 result keys: ``type='eip1559'``, ``maxFeePerGas``,
        ``maxPriorityFeePerGas``.

        Legacy result keys: ``type='legacy'``, ``gasPrice``.
        """
        speed = self._normalize_speed(speed or self.default_speed())
        fee_type = self.network_fee_type()
        if fee_type == "eip1559":
            return self._eip1559_gas_price(speed)
        return self._legacy_gas_price(speed)

    def _eip1559_gas_price(self, speed: str) -> Dict[str, Any]:
        latest_block = self.web3.eth.get_block("latest")
        base_fee = latest_block.get("baseFeePerGas")
        if base_fee is None:
            logger.warning(
                "Network %s has no baseFeePerGas; falling back to legacy gasPrice",
                self.network,
            )
            return self._legacy_gas_price(speed)

        priority_fee_wei = gwei_to_wei(self.priority_fee_gwei(speed))
        multiplier = self.speed_multiplier(speed)
        # Standard formula: maxFeePerGas = base_fee * 2 + priority_fee
        max_fee_wei = int((Decimal(base_fee) * Decimal(2) + Decimal(priority_fee_wei)) * multiplier)
        max_fee_wei = self._cap_gas_price(max_fee_wei)
        priority_fee_wei = min(priority_fee_wei, max_fee_wei)
        return {
            "type": "eip1559",
            "maxFeePerGas": max_fee_wei,
            "maxPriorityFeePerGas": priority_fee_wei,
            "baseFeePerGas": int(base_fee),
        }

    def _legacy_gas_price(self, speed: str) -> Dict[str, Any]:
        try:
            current = self.web3.eth.gas_price
        except Exception as exc:
            raise FeeError(f"Failed to fetch gas price: {exc}") from exc
        multiplier = self.speed_multiplier(speed)
        gas_price = int(Decimal(current) * multiplier)
        gas_price = self._cap_gas_price(gas_price)
        return {
            "type": "legacy",
            "gasPrice": gas_price,
        }

    # ------------------------------------------------------------------
    # Validation and combination

    def validate_gas_price(
        self,
        gas_price: int,
        max_gas_price: Optional[int] = None,
    ) -> bool:
        """Return True if ``gas_price`` (wei) is within the configured cap."""
        cap = max_gas_price if max_gas_price is not None else self.max_gas_price_wei()
        if gas_price <= 0:
            raise FeeError("gas_price must be positive")
        if gas_price > cap:
            raise FeeError(
                f"Gas price {wei_to_gwei(gas_price)} Gwei exceeds max "
                f"{wei_to_gwei(cap)} Gwei"
            )
        return True

    def _cap_gas_price(self, gas_price: int) -> int:
        cap = self.max_gas_price_wei()
        if gas_price > cap:
            logger.warning(
                "Computed gas price %s wei exceeds cap %s wei; clamping",
                gas_price,
                cap,
            )
            return cap
        return gas_price

    @staticmethod
    def calculate_fee(gas_limit: int, gas_price: int) -> int:
        """Calculate the total fee in wei (gas_limit * gas_price)."""
        if gas_limit <= 0 or gas_price <= 0:
            raise FeeError("gas_limit and gas_price must be positive")
        return gas_limit * gas_price

    # ------------------------------------------------------------------
    # Higher-level builder used by the transactions module

    def build_fee_params(
        self,
        transaction: Dict[str, Any],
        tx_type: str = "native",
        speed: Optional[str] = None,
        gas_price: Optional[int] = None,
        gas_limit: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Return the gas/fee fields for a transaction dict.

        ``gas_price`` and ``gas_limit`` allow callers to override automatic
        estimation. ``gas_price`` is interpreted as wei.
        """
        speed = self._normalize_speed(speed or self.default_speed())

        # Gas limit
        if gas_limit is not None:
            final_gas_limit = int(gas_limit)
        else:
            try:
                final_gas_limit = self.estimate_gas(transaction)
            except FeeError:
                final_gas_limit = self.default_gas_limit(tx_type)

        # Gas price
        fee_type = self.network_fee_type()
        result: Dict[str, Any] = {"gas": final_gas_limit}
        if gas_price is not None:
            self.validate_gas_price(int(gas_price))
            if fee_type == "eip1559":
                priority_fee_wei = min(
                    gwei_to_wei(self.priority_fee_gwei(speed)), int(gas_price)
                )
                result.update(
                    {
                        "maxFeePerGas": int(gas_price),
                        "maxPriorityFeePerGas": priority_fee_wei,
                    }
                )
            else:
                result["gasPrice"] = int(gas_price)
        else:
            pricing = self.get_gas_price(speed)
            if pricing["type"] == "eip1559":
                self.validate_gas_price(pricing["maxFeePerGas"])
                result.update(
                    {
                        "maxFeePerGas": pricing["maxFeePerGas"],
                        "maxPriorityFeePerGas": pricing["maxPriorityFeePerGas"],
                    }
                )
            else:
                self.validate_gas_price(pricing["gasPrice"])
                result["gasPrice"] = pricing["gasPrice"]
        return result

    def preview_fee(
        self,
        transaction: Dict[str, Any],
        tx_type: str = "native",
        speed: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return a human-friendly fee preview without sending the tx."""
        params = self.build_fee_params(transaction, tx_type=tx_type, speed=speed)
        gas_limit = params["gas"]
        if "gasPrice" in params:
            effective_price = params["gasPrice"]
        else:
            effective_price = params["maxFeePerGas"]
        total_wei = self.calculate_fee(gas_limit, effective_price)
        return {
            "network": self.network,
            "speed": speed or self.default_speed(),
            "gas_limit": gas_limit,
            "effective_gas_price_wei": effective_price,
            "effective_gas_price_gwei": float(wei_to_gwei(effective_price)),
            "estimated_fee_wei": total_wei,
            "estimated_fee_ether": float(Decimal(total_wei) / Decimal(10**18)),
            "fee_type": self.network_fee_type(),
            "params": params,
        }

    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_speed(speed: str) -> str:
        if speed not in VALID_SPEEDS:
            raise FeeError(
                f"Invalid speed '{speed}'. Must be one of {sorted(VALID_SPEEDS)}"
            )
        return speed
