#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor (Etherscan)

Features:
- Robust retry & backoff (urllib3 + tenacity)
- Rich table output (optional)
- JSON output mode
- Graceful shutdown
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
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

@dataclass(frozen=True)
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
    if logger.handlers:
        logger.handlers.clear()

    try:
        from rich.logging import RichHandler

        handler = RichHandler(
            show_time=True,
            show_path=False,
            rich_tracebacks=False,
        )
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

    retries = Retry(
        total=Config.RETRY_LIMIT,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods={"GET"},
        raise_on_status=False,
    )

    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

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
def fetch_gas_prices(api_key: str) -> Optional[GasPrices]:
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    response = SESSION.get(
        Config.API_URL,
        params=params,
        timeout=Config.TIMEOUT,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except json.JSONDecodeError:
        logger.error("Invalid JSON returned by API")
        return None

    if not isinstance(payload, dict):
        logger.error("Unexpected response structure: %r", payload)
        return None

    status = payload.get("status")
    message = str(payload.get("message", "")).upper()
    result = payload.get("result")

    if status == ApiStatus.OK and isinstance(result, dict):
        try:
            return GasPrices(
                safe=int(result["SafeGasPrice"]),
                propose=int(result["ProposeGasPrice"]),
                fast=int(result["FastGasPrice"]),
            )
        except (KeyError, ValueError, TypeError):
            logger.error("Malformed gas price data: %r", result)
            return None

    if message == ApiStatus.NOTOK:
        logger.warning("API rate-limited or temporarily unavailable")
        return None

    logger.error("Unexpected API response: %r", payload)
    return None


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
        table.add_column("Safe", justify="center")
        table.add_column("Propose", justify="center")
        table.add_column("Fast", justify="center")

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


def validate_interval(interval: int) -> int:
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
    interval = validate_interval(interval)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
        except Exception:
            pass  # Windows / restricted environments

    while not _stop_requested:
        start_ts = time.monotonic()

        try:
            prices = fetch_gas_prices(api_key)
            if prices:
                display_gas_prices(prices, as_json)
            else:
                logger.warning("No gas price data received")
        except Exception as exc:
            logger.error("Fetch failed: %s", exc)

        if run_once:
            break

        sleep_for = max(0.0, interval - (time.monotonic() - start_ts))
        time.sleep(sleep_for)


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
