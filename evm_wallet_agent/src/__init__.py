"""EVM Wallet Agent package."""

from .wallet import Wallet
from .storage import (
    create_wallet_folder,
    save_wallet,
    load_wallet,
    list_wallets,
    delete_wallet,
)
from .transactions import (
    send_native,
    send_erc20,
    approve_token,
    estimate_transaction_fee,
    get_transaction_status,
)
from .claims import (
    claim_airdrop,
    claim_staking_rewards,
    claim_token,
    check_claimable,
)
from .fee_manager import FeeManager
from . import utils

__all__ = [
    "Wallet",
    "FeeManager",
    "create_wallet_folder",
    "save_wallet",
    "load_wallet",
    "list_wallets",
    "delete_wallet",
    "send_native",
    "send_erc20",
    "approve_token",
    "estimate_transaction_fee",
    "get_transaction_status",
    "claim_airdrop",
    "claim_staking_rewards",
    "claim_token",
    "check_claimable",
    "utils",
]
