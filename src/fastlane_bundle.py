"""FastLane Atlas MEV protection service.

Wraps the FastLane Atlas Priority Fee Lane (PFL) for censorship-resistant
order submission on Polygon. When an order is submitted through Atlas, it
enters a solver auction — solvers compete to include it in the next block,
preventing front-running and MEV extraction by validators.

Architecture:
  Order → FastLaneBundleService → pfl_addSearcherBundle RPC
                              → Atlas Solver Auction
                              → Block inclusion
                              → Polymarket CLOB fill

Contracts (Polygon mainnet):
  Atlas:      0x4A394bD4Bc2f4309ac0b75c052b242ba3e0f32e0
  PFL-dApp:   0x3e23e4282FcE0cF42DCd0E9bdf39056434E65C1F
  dAppSigner: 0x96D501A4C52669283980dc5648EEC6437e2E6346

Docs: https://fastlane-labs.gitbook.io/polygon-fastlane
SDK:  https://www.npmjs.com/package/@fastlane-labs/atlas-sdk
POC:  https://github.com/FastLane-Labs/polymarket-mev-bundle-poc

Requires: eth-abi, eth-keys (add to pyproject.toml)
"""

import json
import logging
import time
from dataclasses import dataclass

import requests
import sha3
from eth_abi import encode as abi_encode
from eth_keys import keys as eth_keys

log = logging.getLogger(__name__)

# Atlas contract addresses on Polygon
ATLAS_ADDRESS = "0x4A394bD4Bc2f4309ac0b75c052b242ba3e0f32e0"
PFL_DAPP_ADDRESS = "0x3e23e4282FcE0cF42DCd0E9bdf39056434E65C1F"
DAPP_SIGNER_ADDRESS = "0x96D501A4C52669283980dc5648EEC6437e2E6346"

# MATIC/POL on Polygon
NATIVE_TOKEN = "0x0000000000000000000000000000000000000000"


@dataclass
class SolverOp:
    """Represents a SolverOperation for the Atlas PFL auction.

    See: https://fastlane-labs.gitbook.io/polygon-fastlane/searcher-guides/atlas-sdks

    struct SolverOp {
        bytes32  domain;       // call_chain_hash — keccak(opp_tx_hash || solver_op_encoded)
        address  from;         // solver address
        uint256  value;        // msg.value (0 for Atlas)
        uint256  maxFeePerGas;
        uint256  gas;
        uint256  deadline;     // 0 = no expiry
        address  solver;       // must equal from
        address  control;      // dapp control contract (PFL-dApp)
        bytes32  userOpHash;   // hash of the user operation
        address  bidToken;     // NATIVE_TOKEN (MATIC/POL)
        uint256  bidAmount;    // solver bid in wei
        bytes    data;         // solver strategy call data
    }
    """

    domain: bytes
    from_: str
    to: str
    value: int
    max_fee_per_gas: int
    gas: int
    deadline: int
    solver: str
    control: str
    user_op_hash: bytes
    bid_token: str
    bid_amount: int
    data: bytes


@dataclass
class BundleResult:
    """Result of a bundle submission attempt."""

    success: bool
    bundle_id: int | None = None
    error: str | None = None
    retries: int = 0


class FastLaneBundleService:
    """Submit orders through FastLane Atlas for MEV protection.

    Instead of posting an order directly to the Polygon RPC (exposed to
    front-runners), the order is wrapped in a SolverOp and submitted to
    the Atlas PFL solver auction. Solvers compete to include the order,
    eliminating MEV extraction and providing guaranteed block inclusion.

    Usage:
        service = FastLaneBundleService(
            rpc_url="https://polygon-mainnet.g.alchemy.com/v2/...",
            solver_private_key="0x...",
        )
        result = service.submit_bundle(
            opportunity_tx=signed_tx_data,   # raw signed Polymarket order tx
            user_op_hash=user_op_hash,       # from Polymarket CLOB SDK
            bid_amount_wei=0,                 # 0 = no monetary bid
            max_fee_per_gas=500_000_000,     # 500 gwei
            gas=500_000,
        )
    """

    def __init__(
        self,
        rpc_url: str,
        solver_private_key: str,
        max_retries: int = 3,
        retry_interval_s: float = 5.0,
    ):
        self.rpc_url = rpc_url
        self.solver_key = solver_private_key
        self.max_retries = max_retries
        self.retry_interval_s = retry_interval_s
        self._session = requests.Session()
        self._bundle_seq = int(time.time()) % 1_000_000

        # Derive solver address from private key
        pk_bytes = bytes.fromhex(solver_private_key.replace("0x", ""))
        pk = eth_keys.PrivateKey(pk_bytes)
        self._solver_addr = pk.public_key.to_checksum_address()

    # ── RPC helper ───────────────────────────────────────────────────────────

    def _rpc(self, method: str, params: list) -> str:
        """Make a JSON-RPC call. Returns the 'result' field or raises."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params, "id": 1}
        resp = self._session.post(
            self.rpc_url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"RPC error: {data['error']}")
        return data["result"]

    # ── Encoding ─────────────────────────────────────────────────────────────

    @staticmethod
    def _encode_solver_op(op: SolverOp) -> bytes:
        """ABI-encode a SolverOp struct."""
        return abi_encode(
            [
                "bytes32",
                "address",
                "uint256",
                "uint256",
                "uint256",
                "uint256",
                "address",
                "address",
                "bytes32",
                "address",
                "uint256",
                "bytes",
            ],
            [
                op.domain,
                op.from_,
                op.value,
                op.max_fee_per_gas,
                op.gas,
                op.deadline,
                op.solver,
                op.control,
                op.user_op_hash,
                op.bid_token,
                op.bid_amount,
                op.data,
            ],
        )

    def _compute_call_chain_hash(
        self, opportunity_tx_hash: bytes, solver_op_encoded: bytes
    ) -> bytes:
        """call_chain_hash = keccak256(opportunity_tx_hash || solver_op_encoded)."""
        return sha3.keccak_256(opportunity_tx_hash + solver_op_encoded).digest()

    # ── Public API ───────────────────────────────────────────────────────────

    def submit_bundle(
        self,
        opportunity_tx: str,
        user_op_hash: str,
        bid_amount_wei: int = 0,
        max_fee_per_gas: int = 500_000_000,
        gas: int = 500_000,
    ) -> BundleResult:
        """Submit an order through the Atlas PFL solver auction.

        Args:
            opportunity_tx: Raw signed transaction (rlp, 0x-prefixed) for the
                            Polymarket order placement.
            user_op_hash:   bytes32 hex string of the user operation, from the
                            Polymarket CLOB SDK or gamma API.
            bid_amount_wei: Solver bid in wei (0 = altruistic).
            max_fee_per_gas: Priority fee cap in wei.
            gas:            Gas limit for the bundle.

        Returns:
            BundleResult with success status and bundle_id.
        """
        # Normalize inputs
        opp_hash_bytes = bytes.fromhex(opportunity_tx[:66])  # first 32 bytes of tx hash
        user_op_bytes = bytes.fromhex(user_op_hash.replace("0x", ""))

        # Step 1: Build a dummy SolverOp just to get the encoded bytes for hashing
        dummy_op = SolverOp(
            domain=bytes(32),
            from_=self._solver_addr,
            to=ATLAS_ADDRESS,
            value=0,
            max_fee_per_gas=max_fee_per_gas,
            gas=gas,
            deadline=0,
            solver=self._solver_addr,
            control=PFL_DAPP_ADDRESS,
            user_op_hash=user_op_bytes,
            bid_token=NATIVE_TOKEN,
            bid_amount=bid_amount_wei,
            data=b"",
        )
        dummy_encoded = self._encode_solver_op(dummy_op)

        # Step 2: Compute call_chain_hash = keccak(opp_hash || dummy_encoded)
        call_chain_hash = self._compute_call_chain_hash(opp_hash_bytes, dummy_encoded)

        # Step 3: Build the real SolverOp with the computed domain
        real_op = SolverOp(
            domain=call_chain_hash,
            from_=self._solver_addr,
            to=ATLAS_ADDRESS,
            value=0,
            max_fee_per_gas=max_fee_per_gas,
            gas=gas,
            deadline=0,
            solver=self._solver_addr,
            control=PFL_DAPP_ADDRESS,
            user_op_hash=user_op_bytes,
            bid_token=NATIVE_TOKEN,
            bid_amount=bid_amount_wei,
            data=b"",
        )
        solver_op_encoded = self._encode_solver_op(real_op)

        # Step 4: Submit via pfl_addSearcherBundle RPC
        self._bundle_seq += 1
        params = [
            opportunity_tx,  # raw signed opportunity transaction
            json.dumps(
                {
                    "domain": call_chain_hash.hex(),
                    "from": self._solver_addr,
                    "to": ATLAS_ADDRESS,
                    "value": "0x0",
                    "maxFeePerGas": hex(max_fee_per_gas),
                    "gas": hex(gas),
                    "deadline": "0x0",
                    "solver": self._solver_addr,
                    "control": PFL_DAPP_ADDRESS,
                    "userOpHash": user_op_hash,
                    "bidToken": NATIVE_TOKEN,
                    "bidAmount": hex(bid_amount_wei),
                    "data": solver_op_encoded.hex(),
                }
            ),
        ]

        for attempt in range(self.max_retries):
            try:
                result = self._rpc("pfl_addSearcherBundle", params)
                log.info(
                    f"Atlas bundle submitted: bundle_id={self._bundle_seq}, "
                    f"attempt={attempt + 1}, result={result}"
                )
                return BundleResult(
                    success=True,
                    bundle_id=self._bundle_seq,
                    retries=attempt,
                )
            except Exception as e:
                log.warning(
                    f"Atlas bundle attempt {attempt + 1}/{self.max_retries} failed: {e}"
                )
                if attempt < self.max_retries - 1:
                    time.sleep(self.retry_interval_s)
                else:
                    return BundleResult(
                        success=False,
                        error=str(e),
                        retries=self.max_retries,
                    )

        return BundleResult(success=False, error="Max retries exceeded")
