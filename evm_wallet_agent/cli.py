#!/usr/bin/env python3
"""Interactive EVM wallet CLI.

All sensitive inputs (wallet password, recipient address, amount) are
collected via interactive prompts -- never as command-line arguments --
so they never end up in shell history or process listings.

Run from the ``evm_wallet_agent`` directory:

    python cli.py --help
"""

from __future__ import annotations

from typing import Any, Dict

import click
from dotenv import load_dotenv

from src.storage import list_wallets as storage_list_wallets
from src.storage import load_wallet as storage_load_wallet
from src.transactions import send_erc20, send_native
from src.utils import load_networks_config
from src.wallet import Wallet

load_dotenv()


def _format_result(result: Any) -> None:
    """Pretty-print a :class:`TransactionResult`."""
    if getattr(result, "success", False):
        click.echo(f"Transaction successful! TX: {result.tx_hash}")
    else:
        click.echo(f"Transaction failed: {getattr(result, 'error', 'unknown error')}")


def _print_networks(networks: Dict[str, Dict[str, Any]]) -> None:
    """Print a numbered table of networks (shared by list/select commands)."""
    for idx, (key, info) in enumerate(networks.items(), 1):
        display_name = info.get("name", "N/A")
        symbol = info.get("native_currency", {}).get("symbol", "N/A")
        click.echo(f"{idx:>3}. {key:<20} | {display_name:<35} | {symbol}")


@click.group()
def cli() -> None:
    """EVM Wallet Agent CLI."""


@cli.command()
def balance() -> None:
    """Check wallet balance."""
    wallet_name = click.prompt("Enter wallet name")
    network = click.prompt("Enter network name", default="ethereum")
    password = click.prompt("Enter wallet password", hide_input=True)

    wallet_data = storage_load_wallet(wallet_name, password)
    wallet = Wallet.import_wallet(wallet_data["private_key"], name=wallet_name)
    bal = wallet.get_balance(network, use_alchemy=True)
    click.echo(f"Balance: {bal}")


@cli.command()
def generate() -> None:
    """Generate a new wallet and save it (encrypted)."""
    wallet_name = click.prompt("Enter wallet name")
    password = click.prompt(
        "Enter password for encryption",
        hide_input=True,
        confirmation_prompt=True,
    )

    wallet = Wallet.generate_wallet(name=wallet_name)
    click.echo(f"Address: {wallet.address}")
    click.echo(f"Private Key: {wallet.private_key}")

    wallet.save(wallet_name=wallet_name, password=password)
    click.echo(f"Wallet saved as: {wallet_name}")


@cli.command()
def send() -> None:
    """Send native tokens (recipient and amount prompted)."""
    wallet_name = click.prompt("Enter wallet name")
    network = click.prompt("Enter network name", default="ethereum")
    password = click.prompt("Enter wallet password", hide_input=True)
    to_address = click.prompt("Enter recipient address")
    amount = click.prompt("Enter amount to send", type=float)

    wallet_data = storage_load_wallet(wallet_name, password)
    wallet = Wallet.import_wallet(wallet_data["private_key"], name=wallet_name)

    result = send_native(wallet, to_address, amount, network, use_alchemy=True)
    _format_result(result)


@cli.command("send-erc20")
def send_erc20_cmd() -> None:
    """Send ERC-20 tokens (token, recipient, amount prompted)."""
    wallet_name = click.prompt("Enter wallet name")
    network = click.prompt("Enter network name", default="ethereum")
    password = click.prompt("Enter wallet password", hide_input=True)
    token_address = click.prompt("Enter token address or symbol")
    to_address = click.prompt("Enter recipient address")
    amount = click.prompt("Enter amount to send", type=float)

    wallet_data = storage_load_wallet(wallet_name, password)
    wallet = Wallet.import_wallet(wallet_data["private_key"], name=wallet_name)

    result = send_erc20(
        wallet, token_address, to_address, amount, network, use_alchemy=True
    )
    _format_result(result)


@cli.command("list-wallets")
def list_wallets() -> None:
    """List all saved wallets."""
    wallets = storage_list_wallets()
    if not wallets:
        click.echo("No wallets saved yet.")
        return
    for entry in wallets:
        click.echo(f"- {entry['name']:<20} {entry['address']}")


@cli.command()
def load() -> None:
    """Load and display wallet info."""
    wallet_name = click.prompt("Enter wallet name")
    password = click.prompt("Enter wallet password", hide_input=True)

    wallet_data = storage_load_wallet(wallet_name, password)
    click.echo(f"Name: {wallet_data['name']}")
    click.echo(f"Address: {wallet_data['address']}")


@cli.command("list-networks")
def list_networks() -> None:
    """List all available networks."""
    _print_networks(load_networks_config())


@cli.command("select-network")
def select_network() -> None:
    """Interactively select a network from the available list."""
    networks = load_networks_config()
    if not networks:
        click.echo("No networks configured.")
        return
    _print_networks(networks)
    choice = click.prompt("Select network number", type=int)
    network_names = list(networks.keys())
    if 1 <= choice <= len(network_names):
        selected = network_names[choice - 1]
        click.echo(f"Selected network: {selected}")
    else:
        click.echo("Invalid selection")


if __name__ == "__main__":
    cli()
