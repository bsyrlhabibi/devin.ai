"""Shared pytest fixtures for the EVM wallet agent test suite.

These fixtures provide an *offline* test environment by mocking the parts of
``web3`` that hit the network. Each test gets its own temporary wallets folder
plus a ``MockWeb3`` that records every transaction sent to it so we can assert
on the resulting transaction shape without touching a real RPC.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest

# Add the package root and ``src`` directory to ``sys.path`` so the tests can
# import the modules using either ``from src.x import y`` (which the project
# uses internally) or ``from src import x``.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


# ----------------------------------------------------------------------
# Path fixtures


@pytest.fixture
def test_config_dir() -> Path:
    """Path to the test_config directory used by all unit tests."""
    return _PROJECT_ROOT / "test_config"


@pytest.fixture
def wallets_folder(tmp_path: Path) -> Path:
    """Per-test wallets folder (isolated under tmp_path)."""
    folder = tmp_path / "wallets"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset module-global state that can leak between tests."""
    # Reset the nonce cache and logger cache between tests.
    from src import logger as logger_module
    from src import transactions as transactions_module

    transactions_module._nonce_state.clear()
    logger_module.reset_loggers()
    monkeypatch.setenv("EVM_WALLET_LOG_LEVEL", "DEBUG")


# ----------------------------------------------------------------------
# MockWeb3 helpers


@dataclass
class MockEth:
    """Replacement for ``web3.eth`` covering everything the agent needs."""

    nonce: int = 0
    gas_price_wei: int = 30_000_000_000  # 30 Gwei
    estimate_gas_value: int = 21_000
    base_fee_wei: int = 10_000_000_000  # 10 Gwei
    raise_on_send: Optional[Exception] = None
    sent_transactions: List[bytes] = field(default_factory=list)
    receipt: Optional[Dict[str, Any]] = None
    balance: int = 1_000_000_000_000_000_000  # 1 ETH
    receipt_calls: int = 0
    _send_counter: int = 0

    def get_transaction_count(self, address: str, block: str = "pending") -> int:
        return self.nonce

    def estimate_gas(self, transaction: Dict[str, Any]) -> int:
        return self.estimate_gas_value

    def get_block(self, block_identifier: str) -> Dict[str, Any]:
        return {"baseFeePerGas": self.base_fee_wei}

    @property
    def gas_price(self) -> int:
        return self.gas_price_wei

    def send_raw_transaction(self, raw_tx: bytes) -> bytes:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent_transactions.append(raw_tx)
        self._send_counter += 1
        # Use a per-call deterministic hash so the storage layer treats each
        # broadcast as a distinct transaction.
        return (self._send_counter).to_bytes(32, "big")

    def get_balance(self, address: str) -> int:
        return self.balance

    def get_transaction_receipt(self, tx_hash: Any) -> Optional[Dict[str, Any]]:
        self.receipt_calls += 1
        if self.receipt is None:
            return None
        return self.receipt

    def wait_for_transaction_receipt(self, tx_hash: Any, **_: Any) -> Dict[str, Any]:
        if self.receipt is None:
            raise TimeoutError("No receipt configured on MockEth")
        return self.receipt

    def contract(self, address: str, abi: Any) -> Any:
        return MockContract(address=address, abi=abi, mock_eth=self)


@dataclass
class MockContract:
    """Minimal replacement for ``web3.eth.contract`` outputs."""

    address: str
    abi: Any
    mock_eth: "MockEth"
    balance_of_value: int = 1_000_000  # raw int (decimals-applied)
    decimals_value: int = 18
    claimable_value: Optional[int] = None

    def encode_abi(self, function_name: str, args: List[Any]) -> str:
        # Return a valid-shaped hex blob. eth-account validates the ``data``
        # field is hex, so we cannot leak Python text into it. The contents
        # don't matter for transaction-signing tests, only the *shape* does.
        payload = function_name.encode("utf-8").ljust(32, b"\x00")
        return "0x" + payload.hex()

    @property
    def functions(self) -> Any:
        return _MockFunctions(self)

    def get_function_by_name(self, name: str) -> Any:
        return _MockBoundFunction(self, name)


class _MockBoundFunction:
    def __init__(self, contract: MockContract, name: str) -> None:
        self.contract = contract
        self.name = name

    def __call__(self, *args: Any) -> "_MockBoundCall":
        return _MockBoundCall(self.contract, self.name, args)


class _MockBoundCall:
    def __init__(self, contract: MockContract, name: str, args: Any) -> None:
        self.contract = contract
        self.name = name
        self.args = args

    def call(self) -> Any:
        if self.name in {"claimable", "claimableAmount", "earned", "pendingRewards"}:
            return (
                self.contract.claimable_value
                if self.contract.claimable_value is not None
                else 0
            )
        if self.name == "balanceOf":
            return self.contract.balance_of_value
        if self.name == "decimals":
            return self.contract.decimals_value
        raise AttributeError(self.name)


class _MockFunctions:
    def __init__(self, contract: MockContract) -> None:
        self._contract = contract

    def balanceOf(self, address: str) -> _MockBoundCall:  # noqa: N802 (web3 naming)
        return _MockBoundCall(self._contract, "balanceOf", (address,))

    def decimals(self) -> _MockBoundCall:
        return _MockBoundCall(self._contract, "decimals", ())


@dataclass
class MockWeb3:
    """Stand-in for ``web3.Web3`` returned by ``get_web3``.

    Only the attributes the wallet-agent actually uses are wired up.
    """

    eth: MockEth = field(default_factory=MockEth)

    @staticmethod
    def to_checksum_address(address: str) -> str:
        from web3 import Web3

        return Web3.to_checksum_address(address)

    @property
    def is_connected(self) -> bool:  # pragma: no cover - convenience for callers
        return True


@pytest.fixture
def mock_eth() -> MockEth:
    """Return a fresh :class:`MockEth` for a test."""
    return MockEth()


@pytest.fixture
def mock_web3(mock_eth: MockEth) -> MockWeb3:
    """Return a :class:`MockWeb3` wired to a fresh :class:`MockEth`."""
    return MockWeb3(eth=mock_eth)


@pytest.fixture
def patch_web3(monkeypatch: pytest.MonkeyPatch, mock_web3: MockWeb3) -> MockWeb3:
    """Patch ``get_web3`` everywhere so tests stay offline.

    All modules that import ``get_web3`` from ``src.utils`` see the patched
    version returning ``mock_web3``.
    """
    from src import claims as claims_module
    from src import fee_manager as fee_module
    from src import transactions as transactions_module
    from src import utils as utils_module
    from src import wallet as wallet_module

    for module in (utils_module, fee_module, wallet_module, transactions_module, claims_module):
        monkeypatch.setattr(module, "get_web3", lambda *_, **__: mock_web3, raising=True)
    return mock_web3


@pytest.fixture
def sample_receipt() -> Dict[str, Any]:
    """A receipt-shaped dict used to simulate a confirmed transaction."""
    return {
        "transaction_hash": "0x" + "12" * 32,
        "block_number": 12345,
        "block_hash": "0x" + "ab" * 32,
        "from": "0x0000000000000000000000000000000000000001",
        "to": "0x0000000000000000000000000000000000000002",
        "gas_used": 21000,
        "cumulative_gas_used": 21000,
        "effective_gas_price": 30_000_000_000,
        "status": 1,
        "contract_address": None,
    }


# ----------------------------------------------------------------------
# Misc helpers


_TEST_PASSWORD = "p@ssw0rd-for-tests-only"


@pytest.fixture
def password() -> str:
    """Default password used by storage / wallet tests."""
    return _TEST_PASSWORD


def pytest_collection_modifyitems(config: pytest.Config, items: List[pytest.Item]) -> None:
    """Skip E2E tests unless ``EVM_WALLET_RUN_E2E=1`` is set."""
    run_e2e = os.environ.get("EVM_WALLET_RUN_E2E", "0") in {"1", "true", "yes"}
    if run_e2e:
        return
    skip_marker = pytest.mark.skip(
        reason="Set EVM_WALLET_RUN_E2E=1 to run live-testnet end-to-end tests."
    )
    for item in items:
        if "live_e2e" in item.keywords:
            item.add_marker(skip_marker)
