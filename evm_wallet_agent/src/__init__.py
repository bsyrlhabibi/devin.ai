"""EVM Wallet Agent package."""

from .wallet import Wallet
from .storage import (
    append_transaction,
    create_wallet_folder,
    delete_wallet,
    list_wallets,
    load_wallet,
    read_transactions,
    save_transaction_result,
    save_wallet,
)
from .transactions import (
    approve_token,
    estimate_transaction_fee,
    get_transaction_status,
    send_erc20,
    send_native,
    update_result_from_receipt,
    wait_for_receipt,
)
from .claims import (
    check_claimable,
    claim_airdrop,
    claim_staking_rewards,
    claim_token,
)
from .fee_manager import FeeManager
from .logger import get_wallet_logger, log_transaction_result, setup_logger
from .utils import (
    FeeError,
    NetworkError,
    StorageError,
    TransactionError,
    TransactionResult,
    WalletError,
)
from . import utils

__all__ = [
    "FeeError",
    "FeeManager",
    "NetworkError",
    "StorageError",
    "TransactionError",
    "TransactionResult",
    "Wallet",
    "WalletError",
    "append_transaction",
    "approve_token",
    "check_claimable",
    "claim_airdrop",
    "claim_staking_rewards",
    "claim_token",
    "create_wallet_folder",
    "delete_wallet",
    "estimate_transaction_fee",
    "get_transaction_status",
    "get_wallet_logger",
    "list_wallets",
    "load_wallet",
    "log_transaction_result",
    "read_transactions",
    "save_transaction_result",
    "save_wallet",
    "send_erc20",
    "send_native",
    "setup_logger",
    "update_result_from_receipt",
    "utils",
    "wait_for_receipt",
]
