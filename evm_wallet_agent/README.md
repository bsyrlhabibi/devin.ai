# EVM Wallet Agent

A Python toolkit for building agents that operate EVM wallets across multiple
chains (Ethereum, Polygon, BSC, and their testnets). It provides:

- A `Wallet` class for generating, importing, and loading wallets.
- Folder-based, AES-256-GCM encrypted on-disk storage of private keys.
- Multi-chain configuration via YAML for networks, tokens, and fee settings.
- Stateless, agent-friendly transaction functions (`send_native`, `send_erc20`,
  `approve_token`, `estimate_transaction_fee`, ...).
- A comprehensive `FeeManager` with EIP-1559 and legacy gas pricing, speed
  presets (`slow`, `medium`, `fast`, `urgent`), gas multipliers, max-gas-price
  caps, and gas estimation with manual override.
- A unified `TransactionResult` reporting type returned by every send / approve
  / claim function with `success`, `tx_hash`, `error`, `gas_used`, `fee_paid`,
  and a JSON-friendly `to_dict()`.
- Per-wallet logging (`wallets/<name>/transactions.log`) and per-wallet
  transaction history (`wallets/<name>/transactions.json`).
- A full test suite (unit + offline end-to-end + opt-in live-testnet) under
  `tests/` with `pytest.ini` and a separate `requirements-test.txt`.

> Heads up: this library handles private keys. Read the
> [Security Best Practices](#security-best-practices) section before using it
> with funded mainnet wallets.

## Project Structure

```
evm_wallet_agent/
├── config/
│   ├── networks.yaml        # RPC endpoints for Ethereum, Polygon, BSC, etc.
│   ├── tokens.yaml          # Common token addresses per network
│   └── fee_config.yaml      # Fee / gas configuration
├── src/
│   ├── wallet.py            # Wallet class: generate / import / load
│   ├── transactions.py      # Send native, send ERC-20, approve, fee preview
│   ├── claims.py            # Airdrop / staking / token claim helpers
│   ├── storage.py           # Folder-based wallet storage with encryption
│   ├── fee_manager.py       # Gas estimation and fee management
│   ├── logger.py            # Per-wallet logging
│   └── utils.py             # Error handling, conversions, retries, TransactionResult
├── tests/                   # Unit + offline e2e + opt-in live-e2e tests
│   ├── conftest.py
│   ├── test_wallet.py
│   ├── test_storage.py
│   ├── test_transactions.py
│   ├── test_claims.py
│   ├── test_fee_manager.py
│   └── test_e2e.py
├── test_config/             # Testnet configs (Sepolia / Mumbai / BSC testnet)
│   ├── networks.yaml          (aliased as test_networks.yaml)
│   ├── tokens.yaml            (aliased as test_tokens.yaml)
│   └── fee_config.yaml
├── wallets/                 # Wallet storage directory (gitignored)
├── .env.example             # Template for PRIVATE_KEY_PASSWORD, RPC URLs
├── pytest.ini
├── requirements.txt
├── requirements-test.txt
└── README.md
```

## Installation

```bash
cd evm_wallet_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # edit PRIVATE_KEY_PASSWORD and RPC URLs
```

To also install test dependencies:

```bash
pip install -r requirements-test.txt
```

Python 3.10+ is recommended.

## Quickstart

```python
import os
from dotenv import load_dotenv
from src.wallet import Wallet
from src.storage import save_wallet, load_wallet, list_wallets
from src.transactions import (
    send_native, send_erc20, approve_token, estimate_transaction_fee,
)

load_dotenv()
password = os.environ["PRIVATE_KEY_PASSWORD"]

# 1) Generate and persist a new wallet.
wallet = Wallet.generate_wallet(name="alice")
wallet.save(password=password)
print("New wallet:", wallet.address)

# 2) Load a saved wallet later (e.g. in another agent call).
alice = Wallet.load("alice", password=password)

# 3) Check balances.
eth_balance = alice.get_balance(network="sepolia")
usdc_balance = alice.get_balance(network="sepolia", token="USDC")

# 4) Preview the fee before sending.
preview = estimate_transaction_fee(
    wallet=alice,
    to_address="0x000000000000000000000000000000000000dEaD",
    amount=0.001,
    network="sepolia",
    tx_type="native",
    speed="fast",
)
print(preview)

# 5) Send a transaction (will broadcast to the configured RPC).
tx = send_native(
    wallet=alice,
    to_address="0x000000000000000000000000000000000000dEaD",
    amount=0.001,
    network="sepolia",
    speed="medium",
    wallet_folder="wallets",  # record into wallets/alice/transactions.json
)
print(tx["tx_hash"])
```

## Agent Integration

Every public function is **stateless** and designed to be called from an agent
loop. Typical patterns:

| Agent intent              | Function                                            |
|---------------------------|-----------------------------------------------------|
| Create a fresh wallet     | `Wallet.generate_wallet(...).save(...)`             |
| Use an existing wallet    | `Wallet.load(name, password)`                       |
| Check a balance           | `Wallet.get_balance(network, token=None)`           |
| Preview a fee             | `estimate_transaction_fee(wallet, to, amount, ...)` |
| Send a native transfer    | `send_native(wallet, to, amount, network, ...)`     |
| Send an ERC-20 transfer   | `send_erc20(wallet, token, to, amount, network)`    |
| Approve an ERC-20 spender | `approve_token(wallet, token, spender, amount, ..)` |
| Check claimable rewards   | `check_claimable(wallet, contract, network, ...)`   |
| Claim an airdrop          | `claim_airdrop(wallet, contract, network, ...)`     |
| Claim staking rewards     | `claim_staking_rewards(wallet, contract, network)`  |
| Claim a token reward      | `claim_token(wallet, token, contract, network)`     |
| Poll a tx status          | `get_transaction_status(tx_hash, network)`          |
| List all known wallets    | `list_wallets()`                                    |
| Delete a wallet folder    | `delete_wallet(name)`                               |

Pass `wallet_folder="wallets"` to any of the send/approve functions to have the
resulting transaction appended to `wallets/<name>/transactions.json`.

## Fee Management Guide

All fee logic lives in `src/fee_manager.py` and is configured by
`config/fee_config.yaml`. The key knobs are:

```yaml
fee_settings:
  default_speed: "medium"
  gas_multiplier: 1.1        # Buffer applied to estimated gas limits
  max_gas_price: 100         # Maximum Gwei (network-level cap)
  priority_fee:
    slow: 1
    medium: 2
    fast: 3
    urgent: 5
  network_specific:
    ethereum: { type: eip1559, max_gas_price: 200 }
    polygon:  { type: eip1559, max_gas_price: 500 }
    bsc:      { type: legacy,  max_gas_price: 20 }
```

- **Speed presets** select the priority fee (EIP-1559) or a multiplier on the
  current gas price (legacy): `slow`, `medium`, `fast`, `urgent`.
- **Gas multiplier** buffers the `eth_estimateGas` result so transactions don't
  run out of gas on slightly-different state.
- **Max gas price cap** is enforced both per-network and globally. Computed gas
  prices above the cap are clamped; manual overrides above the cap raise a
  `FeeError`.
- **Manual override**: any send function accepts `gas_price` (wei) and
  `gas_limit` to bypass auto-estimation:

  ```python
  send_native(
      wallet=alice,
      to_address="0x...",
      amount=0.1,
      network="ethereum",
      gas_price=30_000_000_000,  # 30 Gwei
      gas_limit=21000,
  )
  ```

- **EIP-1559 vs legacy**: each network advertises its preferred fee mode in
  `fee_config.yaml`. EIP-1559 builds `maxFeePerGas` + `maxPriorityFeePerGas`
  from the latest block's `baseFeePerGas`; legacy uses `eth_gasPrice`.

### Fee preview

`estimate_transaction_fee` returns a dict that's easy for an LLM to read:

```python
{
  "network": "sepolia",
  "speed": "medium",
  "gas_limit": 21000,
  "effective_gas_price_wei": 30000000000,
  "effective_gas_price_gwei": 30.0,
  "estimated_fee_wei": 630000000000000,
  "estimated_fee_ether": 0.00063,
  "fee_type": "eip1559",
  "params": { ... }
}
```

## Claim Functions Guide

Claim contracts vary wildly: airdrops, Synthetix-style staking pools, MasterChef
forks, vesting contracts, and so on. `src/claims.py` exposes four functions
designed to cover the common shapes while still letting you drop down to a raw
calldata payload when needed.

### Quick examples

```python
from src.wallet import Wallet
from src.claims import (
    claim_airdrop, claim_staking_rewards, claim_token, check_claimable,
)

alice = Wallet.load("alice", password=password)

# 1. Check claimable amount before spending gas.
info = check_claimable(
    wallet=alice,
    contract_address="0xAirdropContract...",
    network="ethereum",
    token_address="USDC",  # optional: enables human-readable `amount`
)
# {'contract': '0x...', 'account': '0x...', 'function': 'claimable',
#  'amount_wei': 12500000, 'amount': 12.5, 'claimable': True, ...}

# 2. Claim an airdrop (defaults try claim() / claim(address)).
if info["claimable"]:
    tx = claim_airdrop(
        wallet=alice,
        contract_address="0xAirdropContract...",
        network="ethereum",
        speed="fast",
    )

# 3. Claim staking rewards from a Synthetix-style pool.
claim_staking_rewards(
    wallet=alice,
    contract_address="0xStakingPool...",
    network="polygon",
)

# 4. Claim a specific token reward (token_address used for metadata + lookups).
claim_token(
    wallet=alice,
    token_address="USDC",
    contract_address="0xClaimContract...",
    network="ethereum",
)
```

### Custom contracts

If a contract's selector isn't in the default probe list, pass an `abi` and
`function_name` (with optional `args`). The helpers will encode the call for
you and route it through the same fee manager as everything else:

```python
my_abi = [
    {"inputs": [{"name": "merkleProof", "type": "bytes32[]"}],
     "name": "claim", "outputs": [],
     "stateMutability": "nonpayable", "type": "function"},
]

claim_airdrop(
    wallet=alice,
    contract_address="0xMerkleAirdrop...",
    network="ethereum",
    abi=my_abi,
    function_name="claim",
    args=[merkle_proof],
)
```

You can also bypass ABI encoding entirely by passing pre-computed calldata via
the `data` argument (`"0x..."` or bytes).

### How defaults are probed

| Function                | Default selectors tried                                  |
|-------------------------|----------------------------------------------------------|
| `claim_airdrop`         | `claim()`, `claim(address)`, `claimTokens()`             |
| `claim_staking_rewards` | `getReward()`, `getReward(address)`, `claimRewards()`, `harvest()` |
| `claim_token`           | `claim()`, `claim(address)`, `claimToken(address)`, `claimRewards()`, `getReward()` |
| `check_claimable`       | `claimable(address)`, `claimableAmount`, `earned`, `pendingRewards`, `balanceOf` |

When defaults don't fit, supply `abi` + `function_name` (and `args` if needed),
or `data` for a fully pre-encoded call. Successful sends are recorded into
`wallets/<name>/transactions.json` when `wallet_folder` is supplied.

## API Reference

### `src/wallet.py`

- `Wallet.generate_wallet(name=None)` — Create a fresh wallet (random key).
- `Wallet.import_wallet(private_key, name=None)` — Build a wallet from a key.
- `Wallet.from_mnemonic(mnemonic, ...)` — Derive from BIP-39 mnemonic.
- `Wallet.load(name, password, folder="wallets")` — Load a saved wallet.
- `Wallet.save(name, password, folder="wallets", overwrite=False)` — Encrypt
  and persist to disk.
- `Wallet.get_balance(network, token=None)` — Native balance, or ERC-20 if
  `token` (symbol or address) is provided.
- Module-level shortcuts: `generate_wallet`, `import_wallet`, `load_wallet`.

### `src/logger.py`

- `setup_logger(wallet_name, folder="wallets", level=None, console=True)` —
  Create (or fetch) a logger that writes to `wallets/<name>/transactions.log`
  plus stderr. Loggers are cached per-wallet so handlers don't stack.
- `get_wallet_logger(wallet_name, folder="wallets")` — Convenience wrapper.
- `log_transaction_result(wallet_name, result, folder="wallets")` — Log a
  :class:`TransactionResult` at INFO/ERROR depending on `result.success`.
- `reset_loggers()` — Clear the logger cache (used by tests).

Override the log level with `EVM_WALLET_LOG_LEVEL=DEBUG` in the environment.

### `src/storage.py`

- `create_wallet_folder(name, folder="wallets")` — Create a wallet directory.
- `save_wallet(wallet_data, name, password, folder="wallets")` — Encrypt and
  save a wallet. `wallet_data` must contain `address` and `private_key`.
- `load_wallet(name, password, folder="wallets")` — Decrypt and return wallet.
- `list_wallets(folder="wallets")` — Enumerate saved wallets.
- `delete_wallet(name, folder="wallets")` — Remove a wallet folder.
- `append_transaction(name, record, folder="wallets")` and
  `read_transactions(name, folder="wallets")` — Per-wallet tx history.

Folder layout (one folder per wallet):

```
wallets/<name>/
  private_key.enc      # AES-256-GCM, PBKDF2-HMAC-SHA256 (200k iterations)
  address.txt          # Public address
  config.yaml          # Metadata (created_at, label, network, ...)
  transactions.json    # Append-only transaction history
```

### `src/transactions.py`

- `send_native(wallet, to_address, amount, network, speed="medium", gas_price=None, gas_limit=None, wallet_folder=None)`
- `send_erc20(wallet, token_address, to_address, amount, network, speed="medium", gas_price=None, gas_limit=None, wallet_folder=None)`
- `approve_token(wallet, token_address, spender_address, amount, network, speed="medium", gas_price=None, gas_limit=None, wallet_folder=None)`
- `estimate_transaction_fee(wallet, to_address, amount, network, tx_type="native", token=None, speed="medium")`
- `get_transaction_status(tx_hash, network)` — Returns parsed receipt or
  `{"status": "pending"}`.
- `wait_for_receipt(tx_hash, network, timeout=180)` — Block until mined.

Nonces are managed internally per `(address, network)` using a thread-safe
counter, so sending several transactions in quick succession is safe.

### `src/claims.py`

- `claim_airdrop(wallet, contract_address, network, speed="medium", gas_price=None, gas_limit=None, abi=None, function_name=None, args=None, data=None, wallet_folder=None)`
- `claim_staking_rewards(wallet, contract_address, network, speed="medium", gas_price=None, gas_limit=None, abi=None, function_name=None, args=None, data=None, wallet_folder=None)`
- `claim_token(wallet, token_address, contract_address, network, speed="medium", gas_price=None, gas_limit=None, abi=None, function_name=None, args=None, data=None, wallet_folder=None)`
- `check_claimable(wallet, contract_address, network, abi=None, function_name=None, args=None, token_address=None, decimals=None)`

All four functions accept an `abi` + `function_name` (with optional `args`) for
custom contracts, or a pre-encoded `data` blob to bypass ABI encoding.

### `src/fee_manager.py`

- `FeeManager(network).estimate_gas(transaction)` — Estimate + buffer gas.
- `FeeManager(network).get_gas_price(speed="medium")` — Returns either
  `{"type": "eip1559", "maxFeePerGas", "maxPriorityFeePerGas"}` or
  `{"type": "legacy", "gasPrice"}`.
- `FeeManager.calculate_fee(gas_limit, gas_price)` — Total fee in wei.
- `FeeManager(network).validate_gas_price(gas_price, max_gas_price=None)` —
  Raises `FeeError` if above the cap.
- `FeeManager(network).preview_fee(transaction, tx_type, speed)` — Combined
  preview used by `estimate_transaction_fee`.

### `src/utils.py`

- `TransactionResult` — Dataclass returned by every send / approve / claim:
  `success`, `tx_hash`, `error`, `gas_used`, `fee_paid`, `timestamp`, plus
  diagnostic fields (`network`, `tx_type`, `from_address`, `to_address`,
  `nonce`, `gas_limit`, `gas_price`, `max_fee_per_gas`,
  `max_priority_fee_per_gas`, `chain_id`, `speed`, `status`, `block_number`,
  `receipt`, `metadata`). Supports `result["tx_hash"]` for dict-style access
  and `result.to_dict()` for JSON serialisation.
- `to_wei(amount, decimals)` / `from_wei(amount, decimals)`
- `gwei_to_wei(value)` / `wei_to_gwei(value)`
- `is_valid_address(addr)` / `to_checksum(addr)`
- `validate_network(name)` / `get_web3(name)`
- `retry(attempts, delay, backoff)` — Decorator for resilient RPC calls.
- `parse_receipt(receipt)` — Turn a `TxReceipt` into a JSON-friendly dict.
- Custom exceptions: `WalletError`, `NetworkError`, `TransactionError`,
  `StorageError`, `FeeError`.

## Reporting System

Every transaction-producing function returns a :class:`TransactionResult`.
Successful results have `success=True` and a populated `tx_hash`; failed
broadcasts return `success=False` with an `error` string instead of raising
(only pre-broadcast validation errors raise `TransactionError`).

```python
from src.transactions import send_native, update_result_from_receipt
from src.wallet import Wallet

alice = Wallet.load("alice", password=password)

result = send_native(
    wallet=alice,
    to_address="0x...",
    amount=0.01,
    network="sepolia",
    wallet_folder="wallets",  # records into wallets/alice/transactions.json
)

if result.success:
    confirmed = update_result_from_receipt(
        result, "sepolia", wallet_folder="wallets", wallet_name="alice",
    )
    print(confirmed.status)          # "success" or "failed"
    print(confirmed.gas_used)        # actual gas
    print(confirmed.fee_paid)        # gas_used * effective_gas_price
else:
    print("broadcast failed:", result.error)
```

`wallets/<name>/transactions.json` stores every result as JSON. Re-saving a
result with the same `tx_hash` (e.g. after `update_result_from_receipt`) is an
**in-place update** — the history file always has one row per transaction.

## Logging

Every wallet has a logger configured by :func:`setup_logger` (called lazily
when sending the first transaction). Logs go to:

1. `wallets/<name>/transactions.log` (file handler) — full audit trail.
2. `stderr` (stream handler) — for ad-hoc inspection during agent runs.

Each broadcast emits a single INFO line with `tx_hash`, `tx_type`, network,
sender, recipient, value, gas limit and speed; failures emit an ERROR line
with the underlying RPC error. The log level defaults to `INFO` and can be
raised with `EVM_WALLET_LOG_LEVEL=DEBUG` in the environment.

## Testing

The suite under `tests/` is organised into:

| File                       | What it covers                                              |
|----------------------------|-------------------------------------------------------------|
| `test_wallet.py`           | Generation, import, mnemonic, save/load, balance lookups     |
| `test_storage.py`          | Folder creation, AES-256-GCM encryption, history persistence |
| `test_transactions.py`     | Send / approve / fee preview / nonce / receipt updates       |
| `test_claims.py`           | Airdrop / staking / token claims + `check_claimable`         |
| `test_fee_manager.py`      | EIP-1559, legacy, gas multipliers, caps, validation          |
| `test_e2e.py`              | Offline end-to-end pipelines + opt-in live-testnet tests     |

All unit and offline-e2e tests run fully offline by patching `Web3` with a
mock in `tests/conftest.py`. Run them with:

```bash
pip install -r requirements-test.txt
pytest                            # full suite, ~100 tests in <2s
pytest --cov=src --cov-report=term-missing  # with coverage
```

### Live-testnet tests

`test_e2e.py` also defines tests marked with `@pytest.mark.live_e2e`. These
are skipped by default. To run them, point the agent at a funded testnet
wallet:

```bash
export EVM_WALLET_RUN_E2E=1
export E2E_PRIVATE_KEY=0x...           # a funded testnet wallet
export E2E_RECIPIENT=0x000000000000000000000000000000000000dEaD
export E2E_NETWORK=sepolia             # or mumbai / bsc_testnet
export SEPOLIA_RPC_URL=https://your-rpc...
pytest tests/test_e2e.py -m live_e2e -v
```

The `test_config/` directory ships testnet-flavoured `networks.yaml`,
`tokens.yaml`, and `fee_config.yaml` (the loader expects those exact names;
the spec aliases `test_networks.yaml` / `test_tokens.yaml` are kept for
backwards compatibility).

## Configuration

### Networks (`config/networks.yaml`)

RPC URLs use `${VAR:-default}` substitution; set the variables in your `.env`
to override the public defaults.

| Network        | Chain ID | Type   |
|----------------|----------|--------|
| `ethereum`     | 1        | EIP-1559 |
| `sepolia`      | 11155111 | EIP-1559 |
| `goerli`       | 5        | EIP-1559 |
| `polygon`      | 137      | EIP-1559 |
| `mumbai`       | 80001    | EIP-1559 |
| `bsc`          | 56       | Legacy   |
| `bsc_testnet`  | 97       | Legacy   |

### Tokens (`config/tokens.yaml`)

Ships with USDC, USDT, DAI, WETH, WBTC, WMATIC, BUSD, WBNB, ... addresses per
network. Add your own under the matching network key.

## Security Best Practices

1. **Never commit `.env` or anything under `wallets/`.** The repo's
   `.gitignore` keeps both out by default.
2. **Use a strong `PRIVATE_KEY_PASSWORD`.** Keys are encrypted with AES-256-GCM
   and PBKDF2-HMAC-SHA256 (200k iterations), but the strength of the encryption
   is bounded by your password.
3. **Use testnets first.** All examples in this README target Sepolia/Mumbai.
4. **Cap your gas prices.** Set sensible `max_gas_price` values per network in
   `fee_config.yaml` to avoid spending huge amounts on fees during volatile
   periods. The library will refuse to send transactions above the cap.
5. **Rotate keys if exposed.** If a `private_key.enc` file ever leaks, treat
   the wallet as compromised — generate a new one and move funds.
6. **Audit before mainnet use.** This code is provided as a building block;
   review it (and the dependencies) before signing real-money transactions.

## License

This component is part of the `bsyrlhabibi/devin.ai` repository; see the
top-level repository for license information.
