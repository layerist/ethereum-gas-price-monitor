#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor (Etherscan)

Improvements:
- Clearer separation of concerns
- Stronger typing & validation
- Better error classification
- Cleaner retry strategy (no double-retry on HTTP layer)
- Safer shutdown handling
- Minor performance & readability tweaks
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from enum import Enum
from typing import Final, Optional, TypedDict

import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception_type,
    before_sleep_log,
)

# ============================================================
# Configuration
# ============================================================

@dataclass(frozen=True, slots=True)
class Config:
    API_URL: Final[str] = "https://api.etherscan.io/api"
    MIN_INTERVAL: Final[int] = 10
    RETRY_LIMIT: Final[int] = 5
    TIMEOUT: Final[int] = 10
    LOG_FORMAT: Final[str] = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"


class ApiStatus(str, Enum):
    OK = "1"
    ERROR = "0"
    NOTOK = "NOTOK"


# ============================================================
# Types
# ============================================================

class GasPrices(TypedDict):
    safe: int
    propose: int
    fast: int


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger("eth-gas-monitor")


def setup_logging(level: str) -> None:
    logger.handlers.clear()

    try:
        from rich.logging import RichHandler

        handler = RichHandler(show_time=True, show_path=False)
        formatter = logging.Formatter("%(message)s")
    except ImportError:
        handler = logging.StreamHandler()
        formatter = logging.Formatter(
            Config.LOG_FORMAT, datefmt=Config.LOG_DATE_FORMAT
        )

    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# ============================================================
# HTTP Session
# ============================================================

def create_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "eth-gas-monitor/1.0",
        }
    )
    atexit.register(session.close)
    return session


SESSION: Final[requests.Session] = create_session()


# ============================================================
# Fetch Logic
# ============================================================

@retry(
    stop=stop_after_attempt(Config.RETRY_LIMIT),
    wait=wait_exponential_jitter(initial=1, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_gas_prices(api_key: str) -> GasPrices:
    response = SESSION.get(
        Config.API_URL,
        timeout=Config.TIMEOUT,
        params={
            "module": "gastracker",
            "action": "gasoracle",
            "apikey": api_key,
        },
    )
    response.raise_for_status()

    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid response structure: {payload!r}")

    if payload.get("status") != ApiStatus.OK:
        raise RuntimeError(
            f"Etherscan error: {payload.get('message')} | {payload.get('result')}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Malformed result field: {result!r}")

    try:
        return GasPrices(
            safe=int(result["SafeGasPrice"]),
            propose=int(result["ProposeGasPrice"]),
            fast=int(result["FastGasPrice"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid gas price payload: {result!r}") from exc


# ============================================================
# Output
# ============================================================

def display_gas_prices(prices: GasPrices, as_json: bool) -> None:
    if as_json:
        print(json.dumps(prices, indent=2))
        return

    try:
        from rich.console import Console
        from rich.table import Table

        table = Table(title="⛽ Ethereum Gas Prices (Gwei)", expand=True)
        for col in ("Safe", "Propose", "Fast"):
            table.add_column(col, justify="center")

        table.add_row(
            str(prices["safe"]),
            str(prices["propose"]),
            str(prices["fast"]),
        )

        Console().print(table)
    except ImportError:
        logger.info(
            "⛽ Gas | Safe=%d | Propose=%d | Fast=%d",
            prices["safe"],
            prices["propose"],
            prices["fast"],
        )


# ============================================================
# Runtime Control
# ============================================================

_stop_requested = False


def handle_exit_signal(signum, _frame) -> None:
    global _stop_requested
    logger.info("Received signal %s, shutting down…", signum)
    _stop_requested = True


def normalize_interval(interval: int) -> int:
    if interval < Config.MIN_INTERVAL:
        logger.warning(
            "Interval too small (%ds). Using minimum %ds.",
            interval,
            Config.MIN_INTERVAL,
        )
    return max(interval, Config.MIN_INTERVAL)


# ============================================================
# Main Loop
# ============================================================

def run_monitor(
    api_key: str,
    interval: int,
    run_once: bool,
    as_json: bool,
) -> None:
    logger.info("Ethereum Gas Monitor started")
    interval = normalize_interval(interval)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
        except Exception:
            pass

    while not _stop_requested:
        start_ts = time.monotonic()

        try:
            prices = fetch_gas_prices(api_key)
            display_gas_prices(prices, as_json)
        except Exception as exc:
            logger.error("Fetch failed: %s", exc)

        if run_once:
            break

        elapsed = time.monotonic() - start_ts
        time.sleep(max(0.0, interval - elapsed))


# ============================================================
# Entry Point
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument("--api-key", default=os.getenv("ETHERSCAN_API_KEY"))
    parser.add_argument("--interval", type=int, default=int(os.getenv("ETH_GAS_INTERVAL", "60")))
    parser.add_argument("--log-level", default=os.getenv("ETH_GAS_LOG", "INFO"))
    parser.add_argument("--once", action="store_true", help="Run once and exit")
    parser.add_argument("--json", action="store_true", help="Output JSON")

    args = parser.parse_args()
    setup_logging(args.log_level)

    if not args.api_key:
        logger.error("Missing Etherscan API key")
        sys.exit(1)

    run_monitor(
        api_key=args.api_key,
        interval=args.interval,
        run_once=args.once,
        as_json=args.json,
    )


if __name__ == "__main__":
    main()
