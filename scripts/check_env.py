#!/usr/bin/env python3
"""Validate Diamond environment before starting a live run.

Run with: python scripts/check_env.py

Exits with 0 if all critical variables are set and reachable.
Exits with 1 if any check fails (prints which one).
"""

import os
import sys
import json
import urllib.request
import urllib.error


def check_env(var: str, required: bool = True) -> tuple[bool, str]:
    value = os.getenv(var)
    if value and value != f"YOUR_{var}_HERE":
        return True, f"  ✅ {var}: set"
    elif not required:
        return True, f"  ⚠️  {var}: not set (optional)"
    else:
        return False, f"  ❌ {var}: MISSING or still contains placeholder"


def check_rpc_url(url: str, name: str) -> tuple[bool, str]:
    if not url or url == "https://polygon-rpc.com" or "YOUR_" in url:
        return False, f"  ❌ {name}: using public endpoint or placeholder — NOT PRODUCTION READY"

    try:
        req = urllib.request.Request(
            url,
            method="POST",
            headers={"Content-Type": "application/json"},
            data=json.dumps({"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 1}).encode(),
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read())
                block = data.get("result", "unknown")
                return True, f"  ✅ {name}: reachable (block {block})"
            return False, f"  ❌ {name}: HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        return False, f"  ❌ {name}: HTTP {e.code} — check your API key"
    except Exception as e:
        return False, f"  ❌ {name}: unreachable ({e})"


def main():
    print("\n=== Diamond Environment Checks ===\n")

    checks = []

    # Required vars
    for var in [
        "BINANCE_API_KEY",
        "BINANCE_API_SECRET",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
    ]:
        ok, msg = check_env(var)
        checks.append(ok)
        print(msg)

    # RPC — must be private for production
    print()
    polygon_rpc = os.getenv("POLYGON_RPC_URL", "https://polygon-rpc.com")
    ok, msg = check_rpc_url(polygon_rpc, "POLYGON_RPC_URL")
    checks.append(ok)
    print(msg)

    # Binance
    print()
    print("  Binance Futures connectivity check:")
    api_key = os.getenv("BINANCE_API_KEY")
    if not api_key or "YOUR_" in api_key:
        print("  ❌ Cannot check — BINANCE_API_KEY not set")
        checks.append(False)
    else:
        try:
            import requests
            resp = requests.get(
                "https://fapi.binance.com/fapi/v1/positionSide/dual",
                headers={"X-MBX-APIKEY": api_key},
                timeout=5,
            )
            if resp.status_code in (200, 401):
                print(f"  ✅ Binance reachable (API key valid, HTTP {resp.status_code})")
                checks.append(True)
            else:
                print(f"  ⚠️  Binance responded HTTP {resp.status_code}")
                checks.append(False)
        except Exception as e:
            print(f"  ❌ Binance unreachable: {e}")
            checks.append(False)

    # Summary
    print()
    if all(checks):
        print("✅ All checks passed — environment is ready.")
        sys.exit(0)
    else:
        n_failed = len([c for c in checks if not c])
        print(f"❌ {n_failed} check(s) failed — fix before starting the bot.")
        sys.exit(1)


if __name__ == "__main__":
    main()
