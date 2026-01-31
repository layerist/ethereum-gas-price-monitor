#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor (Etherscan)

Highlights:
- Clear separation of concerns
- Strong typing & validation
- Explicit error classes
- Clean retry strategy (network only)
- Graceful shutdown handling
- Drift-corrected scheduling
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
from threading import Event
from typing import Final, TypedDict

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
# Exceptions
# ============================================================

class EtherscanError(RuntimeError):
    """Logical API error returned by Etherscan."""


class InvalidPayloadError(ValueError):
    """Malformed or unexpected API payload."""


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
            Config.LOG_FORMAT,
            datefmt=Config.LOG_DATE_FORMAT,
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
            "User-Agent": "eth-gas-monitor/1.1",
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

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise InvalidPayloadError("Invalid JSON from Etherscan") from exc

    if not isinstance(payload, dict):
        raise InvalidPayloadError(f"Unexpected payload type: {type(payload)}")

    if payload.get("status") != ApiStatus.OK:
        raise EtherscanError(
            f"{payload.get('message')} | {payload.get('result')}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise InvalidPayloadError("Missing or malformed result field")

    try:
        return GasPrices(
            safe=int(result["SafeGasPrice"]),
            propose=int(result["ProposeGasPrice"]),
            fast=int(result["FastGasPrice"]),
        )
    except (KeyError, ValueError, TypeError) as exc:
        raise InvalidPayloadError(
            f"Invalid gas price values: {result!r}"
        ) from exc


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

stop_event = Event()


def handle_exit_signal(signum, _frame) -> None:
    logger.info("Received signal %s, shutting down…", signum)
    stop_event.set()


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

    while not stop_event.is_set():
        start_ts = time.monotonic()

        try:
            prices = fetch_gas_prices(api_key)
            display_gas_prices(prices, as_json)
        except EtherscanError as exc:
            logger.error("API error: %s", exc)
        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)
        except Exception as exc:
            logger.exception("Unexpected failure: %s", exc)

        if run_once:
            break

        elapsed = time.monotonic() - start_ts
        stop_event.wait(max(0.0, interval - elapsed))


# ============================================================
# Entry Point
# ============================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ethereum Gas Price Monitor (Etherscan)"
    )
    parser.add_argument("--api-key", default=os.getenv("ETHERSCAN_API_KEY"))
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("ETH_GAS_INTERVAL", "60")),
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("ETH_GAS_LOG", "INFO"),
    )
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
