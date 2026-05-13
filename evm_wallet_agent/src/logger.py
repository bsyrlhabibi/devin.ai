"""Per-wallet logging.

Each wallet gets its own :class:`logging.Logger` configured with a file
handler that writes to ``wallets/<name>/transactions.log`` and a console
handler that prints to stderr. Loggers are cached so repeated calls return
the same instance and don't stack duplicate handlers.

Used by :mod:`transactions` and :mod:`claims` to log every broadcast attempt
with timestamp, success status, tx hash, gas, and fees.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

from .utils import TransactionResult

DEFAULT_WALLETS_FOLDER = "wallets"
DEFAULT_LOG_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
DEFAULT_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"

_LOGGERS: Dict[str, logging.Logger] = {}


def _log_level_from_env() -> int:
    level_name = os.environ.get("EVM_WALLET_LOG_LEVEL", "INFO").upper()
    return getattr(logging, level_name, logging.INFO)


def setup_logger(
    wallet_name: Optional[str],
    folder: Optional[str] = DEFAULT_WALLETS_FOLDER,
    level: Optional[int] = None,
    console: bool = True,
) -> logging.Logger:
    """Return a logger for ``wallet_name`` writing to ``folder/<name>/transactions.log``.

    If ``wallet_name`` is None we return a module logger that only writes to
    stderr (no per-wallet file). Loggers are cached by name so repeat calls
    don't accumulate handlers.
    """
    key = f"evm_wallet_agent.wallet.{wallet_name}" if wallet_name else "evm_wallet_agent.wallet.<anon>"
    cached = _LOGGERS.get(key)
    if cached is not None:
        return cached

    logger = logging.getLogger(key)
    logger.setLevel(level if level is not None else _log_level_from_env())
    logger.propagate = False

    formatter = logging.Formatter(DEFAULT_LOG_FORMAT, datefmt=DEFAULT_DATE_FORMAT)

    if wallet_name and folder:
        try:
            log_dir = Path(folder) / wallet_name
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_dir / "transactions.log", encoding="utf-8")
            file_handler.setLevel(logger.level)
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - filesystem failure
            logger.warning("Could not attach file handler for %s: %s", wallet_name, exc)

    if console:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(logger.level)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    _LOGGERS[key] = logger
    return logger


def get_wallet_logger(
    wallet_name: Optional[str],
    folder: Optional[str] = DEFAULT_WALLETS_FOLDER,
) -> logging.Logger:
    """Convenience wrapper around :func:`setup_logger` with default level."""
    return setup_logger(wallet_name, folder=folder)


def log_transaction_result(
    wallet_name: Optional[str],
    result: TransactionResult,
    folder: Optional[str] = DEFAULT_WALLETS_FOLDER,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    """Log a :class:`TransactionResult` to the wallet's logger.

    Errors are logged at ``ERROR``; successes at ``INFO``.
    """
    log = get_wallet_logger(wallet_name, folder=folder)
    payload = {
        "tx_hash": result.tx_hash,
        "tx_type": result.tx_type,
        "network": result.network,
        "status": result.status,
        "gas_used": result.gas_used,
        "fee_paid": result.fee_paid,
    }
    if extra:
        payload.update(extra)
    if result.success:
        log.info("Transaction success: %s", payload)
    else:
        log.error("Transaction failed: error=%s payload=%s", result.error, payload)


def reset_loggers() -> None:
    """Clear the logger cache and detach handlers. Used by tests."""
    for logger in _LOGGERS.values():
        for handler in list(logger.handlers):
            try:
                handler.close()
            except Exception:  # pragma: no cover
                pass
            logger.removeHandler(handler)
    _LOGGERS.clear()


__all__ = [
    "DEFAULT_WALLETS_FOLDER",
    "get_wallet_logger",
    "log_transaction_result",
    "reset_loggers",
    "setup_logger",
]
