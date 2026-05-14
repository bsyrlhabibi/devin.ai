---
name: testing-evm-wallet-agent
description: Test the evm_wallet_agent package end-to-end. Use when verifying changes to wallet/transactions/claims/storage/fee_manager/logger/reporting under evm_wallet_agent/src or evm_wallet_agent/tests, OR when verifying the interactive CLI under evm_wallet_agent/cli.py.
---

# Testing the EVM wallet agent

The project lives under `evm_wallet_agent/` and has two testable
surfaces:

1. The Python library (`src/*.py`, exercised by `tests/`) — pure shell,
   no UI.
2. The interactive CLI (`cli.py`, added in PR #7) — exercised by
   recording an xterm session.

## When to use this skill

Use this skill any time you're verifying changes that touch:

- `evm_wallet_agent/src/wallet.py`
- `evm_wallet_agent/src/transactions.py`
- `evm_wallet_agent/src/claims.py`
- `evm_wallet_agent/src/storage.py`
- `evm_wallet_agent/src/fee_manager.py`
- `evm_wallet_agent/src/logger.py`
- `evm_wallet_agent/src/utils.py` (esp. `TransactionResult`, `get_rpc_url`)
- `evm_wallet_agent/cli.py`
- anything under `evm_wallet_agent/tests/` or `evm_wallet_agent/test_config/`

## Setup

1. Install dependencies from the package root:

   ```bash
   pip install -r evm_wallet_agent/requirements.txt
   pip install -r evm_wallet_agent/requirements-test.txt
   ```

   (Both are also installed automatically by the repo blueprint's
   `maintenance` step.)

2. The unit tests expect `evm_wallet_agent/test_config/` to contain both:
   - `networks.yaml` / `tokens.yaml` (canonical names the loader looks for),
     and
   - `test_networks.yaml` / `test_tokens.yaml` (spec aliases).

   Both files are kept in sync intentionally — don't delete one without
   updating the loader.

## How to run the pytest suite

### Default: offline suite (~2 seconds)

```bash
(cd evm_wallet_agent && python -m pytest tests/ -v)
```

Expected outcome: `106 passed, 3 skipped` (the three skipped tests are
`@pytest.mark.live_e2e` and require explicit opt-in). The baseline count
may grow if newer PRs add tests — the important thing is that *all*
offlined tests pass and the live-e2e ones stay skipped.

### Optional: live testnet

Only run this when you have a funded testnet wallet. The wallet only
needs tiny amounts (the test sends `0.00001` of the native token).

```bash
EVM_WALLET_RUN_E2E=1 \
  E2E_PRIVATE_KEY=0x... \
  E2E_NETWORK=sepolia \
  E2E_RECIPIENT=0x000000000000000000000000000000000000dEaD \
  SEPOLIA_RPC_URL=https://... \
  pytest evm_wallet_agent/tests/test_e2e.py -m live_e2e -v
```

For ERC-20 testing, also set `E2E_TOKEN` (symbol or address). To exercise
the Alchemy path, also set `ALCHEMY_API_KEY=...` — `get_rpc_url` will
then prefer the chain's `rpc.alchemy` URL unless the test passes
`use_alchemy=False`.

## Testing the interactive CLI

`evm_wallet_agent/cli.py` is a click-based CLI where every sensitive
input (password, recipient, amount) is collected via `click.prompt`.
To exercise it end-to-end you need a real terminal so you can type into
the prompts — record an `xterm` session with the computer-use tool.

### Recording recipe (Ubuntu, KDE Plasma)

```bash
sudo apt-get install -y xterm wmctrl   # not in the default snapshot
cd /home/ubuntu/repos/devin-ai/evm_wallet_agent
DISPLAY=:0 nohup xterm -fa 'DejaVu Sans Mono' -fs 14 \
  -geometry 120x32 -bg '#101418' -fg '#e6e6e6' \
  -title 'EVM Wallet CLI Test' bash -l >/tmp/xterm.log 2>&1 & disown
sleep 1
WID=$(DISPLAY=:0 wmctrl -l | awk '/evm_wallet_agent/ && /devin-box/ {print $1; exit}')
DISPLAY=:0 wmctrl -i -r "$WID" -b add,maximized_vert,maximized_horz
DISPLAY=:0 wmctrl -i -a "$WID"
```

The shell sets the xterm title to its working dir after launch, so match
on the directory name (`evm_wallet_agent`) rather than the original
`-title` string. Use `DejaVu Sans Mono @ 14pt` for legibility in the
recording — the default `Monospace @ 10pt` is unreadable at 1024x768.

With the xterm visible and maximized, call `recording_start` and then
use `computer` tool clicks/types to drive the CLI.

### Minimal e2e flow that proves the CLI works

1. `python cli.py --help` — confirm 8 commands and that `send-erc20` is
   named that way (not `send-erc20-cmd`).
2. `python cli.py list-networks | head -12` — third column should be
   the native-currency *symbol* (e.g. `ETH`, `MNT`), NOT a Python dict.
3. `python cli.py list-wallets` with an empty `wallets/` — prints
   `No wallets saved yet.` (regression check for the `for x in dicts`
   bug from the original spec).
4. `python cli.py generate` — type a name, password, password again.
   Confirm neither password echoes. Afterwards `ls wallets/<name>/`
   should show `address.txt`, `config.yaml`, `private_key.enc` (perm
   `0600`), `transactions.json`, and `address.txt` content should match
   the `Address:` line printed by the CLI.
5. `python cli.py load` — first with the **wrong** password (expect
   `src.utils.StorageError: Failed to decrypt private key (wrong
   password?)` traceback), then with the correct one (expect `Name:` +
   `Address:` lines).
6. `python cli.py balance` with `network=sepolia` — works against the
   public RPC without `ALCHEMY_API_KEY` thanks to `get_rpc_url`'s
   fallback. A fresh wallet prints `Balance: 0`.
7. `python cli.py send` from an empty wallet against Sepolia — the
   broadcast hits the public RPC and is rejected with `-32000
   insufficient funds for gas * price + value`. The CLI must surface it
   as `Transaction failed: ...`, NOT a Python traceback. This proves
   the whole prompt → sign → broadcast pipeline without needing a
   funded testnet wallet.
8. `python cli.py select-network`, type `2` — should print
   `Selected network: sepolia`.
9. Cleanup: `rm -rf evm_wallet_agent/wallets/cli_test/`.

### Gotchas learned the hard way

- `cli.py:balance` calls `Wallet.import_wallet(wallet_data["private_key"])`,
  which re-derives the address from the private key. **Editing
  `wallets/<name>/address.txt` does NOT change the address used by the
  CLI** — the cached file is only used by `list_wallets()`. If you
  need to prove the balance code path against a known funded address,
  call `Wallet.get_balance("sepolia", use_alchemy=True)` directly with
  a Wallet whose `.address` you've overridden in Python — that's the
  exact method `cli.py:balance` invokes.
- The xterm shell's `PROMPT_COMMAND` overrides the `-title` string
  set on launch. Match windows by working-directory substring with
  `wmctrl -l`, not by the literal title you passed.
- `xterm` and `wmctrl` are not in the default snapshot — install them
  with `apt-get` at the start of the testing session.
- `cli.py` must be invoked from inside `evm_wallet_agent/` (relative
  `wallets/` default path).

## Contracts the test suite is defending

When reviewing a PR that touches the wallet agent, verify these still
hold (the tests already do, but it's useful to know what to grep for in
the diff):

1. Every `send_*` / `approve_*` / `claim_*` function returns a
   `TransactionResult`. Broadcast errors are *captured* as
   `TransactionResult(success=False, error=...)` — they must **not**
   raise. Pre-broadcast validation errors still raise
   `TransactionError`.
2. `update_result_from_receipt` populates `gas_used`, `fee_paid`,
   `block_number`, `effective_gas_price`, and flips `status` based on
   `receipt['status']` (1 = success, 0 = failed).
3. `storage.save_transaction_result` deduplicates by `tx_hash` — the
   pending row is replaced in place when the confirmed row arrives, so
   `transactions.json` stays one entry per transaction.
4. The per-wallet logger (`src/logger.py`) caches loggers by wallet
   name. Calling `get_wallet_logger("alice", folder=X)` twice must
   return the same `Logger` and must NOT stack a second `FileHandler`.
   Tests must call `logger_module.reset_loggers()` between cases (the
   `_reset_module_state` autouse fixture in `conftest.py` does this).
5. Nonce cache (`transactions._nonce_state`) is rolled back to nothing
   on a broadcast failure so the next attempt reuses the same nonce.
6. `get_rpc_url(network, use_alchemy=True)` picks `rpc.alchemy` only
   when `ALCHEMY_API_KEY` is set in the env. `use_alchemy=False` forces
   the public RPC even when the key is set. Every send/claim/balance
   function exposes `use_alchemy: bool = True` and forwards it through
   to `get_web3`.

## Writing targeted probes for new behavior

When a PR adds new behavior that doesn't map cleanly to existing tests,
use `tests/conftest.py`'s `MockWeb3` / `MockEth` directly in a script
rather than spinning up real chain interactions. Pattern:

```python
import sys
from pathlib import Path

ROOT = Path("/home/ubuntu/repos/devin-ai/evm_wallet_agent")
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import conftest
from src import transactions as transactions_module
from src import claims as claims_module
from src import fee_manager as fee_module
from src import utils as utils_module
from src import wallet as wallet_module
from src.transactions import send_native
from src.wallet import Wallet

mw = conftest.MockWeb3()
for module in (utils_module, fee_module, wallet_module,
                transactions_module, claims_module):
    module.get_web3 = lambda *_, **__: mw

w = Wallet.import_wallet(
    "0x4c0883a69102937d6231471b5dbb6204fe5129617082792ae468d01a3f362318",
    config_dir=ROOT / "test_config",
)
r = send_native(
    wallet=w, to_address="0x000000000000000000000000000000000000dEaD",
    amount=0.01, network="test_offline", config_dir=ROOT / "test_config",
)
assert r.success
```

Use `mw.eth.raise_on_send = SomeError(...)` to simulate broadcast
failures, `mw.eth.receipt = {...}` to simulate confirmed transactions,
and `mw.eth.nonce = N` to control the starting nonce.

### Probing the Alchemy / Public RPC selection

To exercise the new selection logic without hitting real RPCs, point
`get_rpc_url` at a temporary YAML config and toggle `ALCHEMY_API_KEY`
in the env:

```python
import os
from src.utils import get_rpc_url

os.environ["ALCHEMY_API_KEY"] = "TESTKEY"
assert get_rpc_url("ethereum").endswith("/v2/TESTKEY")

del os.environ["ALCHEMY_API_KEY"]
assert get_rpc_url("ethereum") == "https://eth.llamarpc.com"

# Force-public even when a key is set:
os.environ["ALCHEMY_API_KEY"] = "TESTKEY"
assert get_rpc_url("ethereum", use_alchemy=False).startswith("https://eth.")
```

When wiring a spy version of `get_web3` to verify `use_alchemy` is
forwarded, remember to patch the symbol on **every** module that
imports it: `utils`, `fee_manager`, `wallet`, `transactions`, `claims`.

### Gotchas when scripting wallet flows

- `Wallet.save(...)` takes `wallet_name` as its **first positional**
  argument, then `password`. When the wallet already has a `name` you
  can omit `wallet_name`, but you must still pass `password` as a
  keyword: `w.save(password="pw", folder=str(folder))`. Passing the
  password positionally is a common foot-gun and will surface as
  `StorageError: Invalid wallet name 'pw...'`.
- The storage layer accepts wallet names matching `[A-Za-z0-9_-]{1,64}`
  only — characters like `@`, `.`, `/` are rejected.
- The transactions module keeps a process-global nonce cache
  (`transactions._nonce_state`). When running multiple probe scripts
  back-to-back, call `transactions._nonce_state.clear()` and
  `logger.reset_loggers()` between them.

## What does NOT need testing in this repo

- The library surface has no dev server to start, no web UI, no
  frontend or backend deployment — for `src/*.py` changes a pytest
  run is sufficient. **CLI changes do warrant a terminal recording**
  (see "Testing the interactive CLI" above).
- CI: `.github/workflows/` is empty in this repo. `git_pr_checks` will
  report "No CI checks ran" — that's expected, not a problem to fix.

## Devin Secrets Needed

- None for the offline suite or the CLI recording — public Sepolia RPC
  is reachable without an Alchemy key.
- Live e2e (`@pytest.mark.live_e2e`) requires `E2E_PRIVATE_KEY` (and
  optionally `E2E_NETWORK`, `E2E_TOKEN`, `E2E_RECIPIENT`, plus the
  matching `*_RPC_URL` env var, e.g. `SEPOLIA_RPC_URL`). To exercise
  the Alchemy URL path, also set `ALCHEMY_API_KEY`. Treat all of these
  as user-scoped secrets and never log their values.
