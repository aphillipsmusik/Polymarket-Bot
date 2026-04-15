"""
auth.py — Two-tier Polymarket authentication (Section 1).

Section 1 specifies:
  • L1 authentication: private key signing (EIP-712 on-chain)
  • L2 authentication: API credentials with HMAC-SHA256
  • Signature types: 0 (MetaMask/EOA), 1 (Magic Link), 2 (Gnosis Safe)

How it works
------------
L1 (on-chain / order signing):
  Every order submitted to the CLOB must be signed with the wallet's
  private key using EIP-712.  py_clob_client handles this internally
  via create_order() — the PolymarketAuth class surfaces the setup.

L2 (REST API / HMAC):
  Account endpoints (balance, positions, open orders) require HMAC-SHA256
  signed headers:
    POLY-ADDRESS:    wallet address
    POLY-SIGNATURE:  HMAC-SHA256 of (timestamp + method + path + body)
    POLY-TIMESTAMP:  Unix timestamp (seconds)
    POLY-API-KEY:    API key ID

API key derivation:
  The CLOB API key is deterministically derived from the wallet's private
  key via a signed challenge — no external registration needed.
  py_clob_client.create_or_derive_api_key() does this automatically.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class L2Credentials:
    """HMAC-SHA256 API credentials (L2 auth)."""
    api_key: str
    api_secret: str
    api_passphrase: str
    wallet_address: str


class PolymarketAuth:
    """
    Manages both tiers of Polymarket authentication.

    Usage
    -----
    auth = PolymarketAuth(private_key, chain_id=137, sig_type=0)
    clob_client = auth.get_clob_client(host)
    headers = auth.l2_headers("GET", "/orders")
    """

    def __init__(
        self,
        private_key: str,
        chain_id: int = 137,
        sig_type: int = 0,            # 0=MetaMask, 1=Magic Link, 2=Gnosis Safe
    ) -> None:
        self.private_key = private_key
        self.chain_id = chain_id
        self.sig_type = sig_type

        self._clob = None             # py_clob_client.ClobClient
        self._creds: Optional[L2Credentials] = None
        self._wallet_address: Optional[str] = None

    # ── Initialization ────────────────────────────────────────────────────────

    def initialize(self, clob_host: str) -> None:
        """
        Set up L1 (py_clob_client) and derive L2 API credentials.
        Idempotent — safe to call multiple times.
        """
        if self._clob is not None:
            return

        try:
            from py_clob_client.client import ClobClient
        except ImportError:
            raise ImportError(
                "py_clob_client is required for live trading.\n"
                "Install: pip install py-clob-client"
            )

        logger.info("Initialising Polymarket two-tier auth …")

        # L1: EIP-712 signing client
        self._clob = ClobClient(
            host=clob_host,
            key=self.private_key,
            chain_id=self.chain_id,
            signature_type=self.sig_type,
        )

        # Derive wallet address from private key
        try:
            from eth_account import Account
            acct = Account.from_key(self.private_key)
            self._wallet_address = acct.address
            logger.info(f"L1 wallet: {self._wallet_address}")
        except Exception:
            logger.warning("Could not derive wallet address from key")

        # L2: Derive / create HMAC API credentials
        try:
            raw_creds = self._clob.create_or_derive_api_key()
            self._clob.set_api_creds(raw_creds)
            self._creds = L2Credentials(
                api_key=raw_creds.api_key,
                api_secret=raw_creds.api_secret,
                api_passphrase=raw_creds.api_passphrase,
                wallet_address=self._wallet_address or "",
            )
            logger.info(f"L2 API key: {self._creds.api_key[:12]}…")
        except Exception as exc:
            logger.error(f"L2 credential derivation failed: {exc}")
            raise

    # ── L2 HMAC-SHA256 headers ────────────────────────────────────────────────

    def l2_headers(
        self,
        method: str,
        path: str,
        body: str = "",
    ) -> Dict[str, str]:
        """
        Build HMAC-SHA256 L2 authentication headers for REST API calls.

        The signature covers: timestamp + method + path + body
        Algorithm: HMAC-SHA256 with api_secret as key.

        Parameters
        ----------
        method : HTTP method (GET, POST, DELETE) — uppercase
        path   : URL path including query string (e.g. "/orders?market=...")
        body   : Request body as a string (empty for GET)
        """
        if self._creds is None:
            raise RuntimeError("Auth not initialized — call initialize() first")

        ts = str(int(time.time()))
        message = ts + method.upper() + path + body
        sig = hmac.new(
            self._creds.api_secret.encode("utf-8"),
            message.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "POLY-ADDRESS":    self._creds.wallet_address,
            "POLY-SIGNATURE":  sig,
            "POLY-TIMESTAMP":  ts,
            "POLY-API-KEY":    self._creds.api_key,
            "POLY-PASSPHRASE": self._creds.api_passphrase,
        }

    # ── Accessors ─────────────────────────────────────────────────────────────

    @property
    def clob_client(self):
        """Return the py_clob_client.ClobClient instance (requires initialize())."""
        if self._clob is None:
            raise RuntimeError("Auth not initialized")
        return self._clob

    @property
    def wallet_address(self) -> Optional[str]:
        return self._wallet_address

    @property
    def is_ready(self) -> bool:
        return self._clob is not None and self._creds is not None
