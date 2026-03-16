#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor (Etherscan)

Features
- Drift-free aligned scheduler
- Strict payload validation
- Network-only retries
- Deterministic shutdown
- Stable logging
- Low allocation runtime loop
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
from typing import Final, TypedDict, Any

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

    CONNECT_TIMEOUT: Final[int] = 5
    READ_TIMEOUT: Final[int] = 10

    USER_AGENT: Final[str] = "eth-gas-monitor/2.1"

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
    pass


class InvalidPayloadError(ValueError):
    pass


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger("eth-gas-monitor")


def setup_logging(level: str) -> None:
    logger.handlers.clear()

    normalized = level.upper()
    if normalized not in logging._nameToLevel:
        normalized = "INFO"

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
    logger.setLevel(logging._nameToLevel[normalized])


# ============================================================
# HTTP Session
# ============================================================


def create_session() -> requests.Session:
    session = requests.Session()

    adapter = requests.adapters.HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
    )

    session.mount("https://", adapter)

    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": Config.USER_AGENT,
        }
    )

    atexit.register(session.close)
    return session


SESSION: Final[requests.Session] = create_session()

TIMEOUT: Final[tuple[int, int]] = (
    Config.CONNECT_TIMEOUT,
    Config.READ_TIMEOUT,
)


# ============================================================
# Fetch Logic
# ============================================================


@retry(
    stop=stop_after_attempt(Config.RETRY_LIMIT),
    wait=wait_exponential_jitter(initial=1, max=15),
    retry=retry_if_exception_type(requests.RequestException),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_gas_prices(api_key: str) -> GasPrices:

    response = SESSION.get(
        Config.API_URL,
        params={
            "module": "gastracker",
            "action": "gasoracle",
            "apikey": api_key,
        },
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    try:
        payload: Any = response.json()
    except json.JSONDecodeError as exc:
        raise InvalidPayloadError("Invalid JSON from Etherscan") from exc

    if not isinstance(payload, dict):
        raise InvalidPayloadError("Payload must be object")

    if payload.get("status") != ApiStatus.OK.value:
        raise EtherscanError(
            f"{payload.get('message')} | {payload.get('result')}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise InvalidPayloadError("Missing result")

    def parse(field: str) -> int:
        v = result.get(field)
        if not isinstance(v, str) or not v.isdigit():
            raise InvalidPayloadError(f"Invalid {field}: {v!r}")
        return int(v)

    return {
        "safe": parse("SafeGasPrice"),
        "propose": parse("ProposeGasPrice"),
        "fast": parse("FastGasPrice"),
    }


# ============================================================
# Output
# ============================================================


USE_RICH = False
_console = None
_table = None

try:
    from rich.console import Console
    from rich.table import Table

    USE_RICH = True
    _console = Console()
except ImportError:
    USE_RICH = False


def display_gas_prices(prices: GasPrices, as_json: bool) -> None:

    if as_json:
        print(json.dumps(prices, separators=(",", ":")))
        return

    if USE_RICH:
        table = Table(title="Ethereum Gas Prices (Gwei)", expand=True)

        table.add_column("Safe", justify="center")
        table.add_column("Propose", justify="center")
        table.add_column("Fast", justify="center")

        table.add_row(
            str(prices["safe"]),
            str(prices["propose"]),
            str(prices["fast"]),
        )

        _console.print(table)

    else:
        logger.info(
            "Gas | Safe=%d | Propose=%d | Fast=%d",
            prices["safe"],
            prices["propose"],
            prices["fast"],
        )


# ============================================================
# Runtime Control
# ============================================================

stop_event = Event()


def handle_exit_signal(signum, _frame) -> None:
    logger.info("Signal %s received — exiting", signum)
    stop_event.set()


def normalize_interval(interval: int) -> int:
    if interval < Config.MIN_INTERVAL:
        logger.warning(
            "Interval %ds too small. Using minimum %ds",
            interval,
            Config.MIN_INTERVAL,
        )
    return max(interval, Config.MIN_INTERVAL)


# ============================================================
# Scheduler
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
        except (ValueError, OSError):
            pass

    next_tick = time.monotonic()

    while not stop_event.is_set():

        try:
            prices = fetch_gas_prices(api_key)
            display_gas_prices(prices, as_json)

        except EtherscanError as exc:
            logger.error("API error: %s", exc)

        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)

        except Exception:
            logger.exception("Unexpected error")

        if run_once:
            break

        next_tick += interval

        now = time.monotonic()
        sleep_time = next_tick - now

        if sleep_time > 0:
            stop_event.wait(sleep_time)
        else:
            next_tick = now


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

    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()

    setup_logging(args.log_level)

    if not args.api_key:
        logger.error("Missing Etherscan API key")
        sys.exit(1)

    try:
        run_monitor(
            api_key=args.api_key,
            interval=args.interval,
            run_once=args.once,
            as_json=args.json,
        )

    except KeyboardInterrupt:
        logger.info("Interrupted by user")

    finally:
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
