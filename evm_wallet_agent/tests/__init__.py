"""Test package for the EVM wallet agent.

These tests are designed to run fully offline by default. Anywhere a real RPC
call would be required, we patch ``Web3`` with a :class:`MockWeb3` from
``conftest`` so that no network requests are made. End-to-end tests that
exercise live testnets (Sepolia, Mumbai, BSC testnet) live in ``test_e2e.py``
and are skipped unless ``EVM_WALLET_RUN_E2E=1`` is set in the environment.
"""
