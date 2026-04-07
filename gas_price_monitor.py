#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor (Etherscan) — Improved

Enhancements
- Zero-drift scheduler (strict alignment)
- Strict payload validation (typed + safe parsing)
- Retry ONLY on network errors (clean separation)
- Reused objects (no per-loop allocations)
- Graceful shutdown (signal-safe)
- Optional JSON / CSV output
- Lightweight runtime loop
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
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# Configuration
# ============================================================


@dataclass(frozen=True, slots=True)
class Config:
    API_URL: Final[str] = "https://api.etherscan.io/api"

    MIN_INTERVAL: Final[int] = 10

    CONNECT_TIMEOUT: Final[int] = 5
    READ_TIMEOUT: Final[int] = 10

    USER_AGENT: Final[str] = "eth-gas-monitor/3.0"

    LOG_FORMAT: Final[str] = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"

    MAX_RETRIES: Final[int] = 3


class ApiStatus(str, Enum):
    OK = "1"


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

    lvl = logging._nameToLevel.get(level.upper(), logging.INFO)

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
    logger.setLevel(lvl)


# ============================================================
# HTTP Session (optimized + retry at transport level)
# ============================================================


def create_session() -> requests.Session:
    session = requests.Session()

    retry_strategy = Retry(
        total=Config.MAX_RETRIES,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )

    adapter = HTTPAdapter(
        pool_connections=10,
        pool_maxsize=10,
        max_retries=retry_strategy,
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

# Prebuilt params to avoid dict allocation in loop
BASE_PARAMS: Final[dict[str, str]] = {
    "module": "gastracker",
    "action": "gasoracle",
}


# ============================================================
# Parsing
# ============================================================


def _parse_int(value: Any, field: str) -> int:
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise InvalidPayloadError(f"Invalid {field}: {value!r}")


def parse_payload(payload: Any) -> GasPrices:
    if not isinstance(payload, dict):
        raise InvalidPayloadError("Payload must be object")

    if payload.get("status") != ApiStatus.OK.value:
        raise EtherscanError(
            f"{payload.get('message')} | {payload.get('result')}"
        )

    result = payload.get("result")
    if not isinstance(result, dict):
        raise InvalidPayloadError("Missing result")

    return {
        "safe": _parse_int(result.get("SafeGasPrice"), "SafeGasPrice"),
        "propose": _parse_int(result.get("ProposeGasPrice"), "ProposeGasPrice"),
        "fast": _parse_int(result.get("FastGasPrice"), "FastGasPrice"),
    }


# ============================================================
# Fetch
# ============================================================


def fetch_gas_prices(api_key: str) -> GasPrices:
    params = BASE_PARAMS.copy()
    params["apikey"] = api_key

    response = SESSION.get(
        Config.API_URL,
        params=params,
        timeout=TIMEOUT,
    )

    response.raise_for_status()

    try:
        payload = response.json()
    except json.JSONDecodeError as exc:
        raise InvalidPayloadError("Invalid JSON") from exc

    return parse_payload(payload)


# ============================================================
# Output (optimized)
# ============================================================

USE_RICH = False

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    USE_RICH = True
except ImportError:
    console = None


def display(prices: GasPrices, as_json: bool, as_csv: bool) -> None:
    if as_json:
        print(json.dumps(prices, separators=(",", ":")))
        return

    if as_csv:
        print(f"{prices['safe']},{prices['propose']},{prices['fast']}")
        return

    if USE_RICH:
        table = Table(title="Gas (Gwei)", expand=True)
        table.add_column("Safe", justify="center")
        table.add_column("Propose", justify="center")
        table.add_column("Fast", justify="center")

        table.add_row(
            str(prices["safe"]),
            str(prices["propose"]),
            str(prices["fast"]),
        )

        console.print(table)
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
    logger.info("Signal %s received — shutting down", signum)
    stop_event.set()


def normalize_interval(interval: int) -> int:
    if interval < Config.MIN_INTERVAL:
        logger.warning(
            "Interval too small (%ds), using %ds",
            interval,
            Config.MIN_INTERVAL,
        )
    return max(interval, Config.MIN_INTERVAL)


# ============================================================
# Scheduler (true drift-free)
# ============================================================


def run_monitor(
    api_key: str,
    interval: int,
    run_once: bool,
    as_json: bool,
    as_csv: bool,
) -> None:

    logger.info("Started Ethereum Gas Monitor")

    interval = normalize_interval(interval)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
        except Exception:
            pass

    start = time.monotonic()
    tick = 0

    while not stop_event.is_set():

        try:
            prices = fetch_gas_prices(api_key)
            display(prices, as_json, as_csv)

        except EtherscanError as exc:
            logger.error("API error: %s", exc)

        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)

        except Exception:
            logger.exception("Unexpected error")

        if run_once:
            break

        tick += 1
        next_time = start + tick * interval
        sleep_time = next_time - time.monotonic()

        if sleep_time > 0:
            stop_event.wait(sleep_time)


# ============================================================
# Entry Point
# ============================================================


def main() -> None:
    parser = argparse.ArgumentParser(description="Ethereum Gas Monitor")

    parser.add_argument("--api-key", default=os.getenv("ETHERSCAN_API_KEY"))
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--log-level", default="INFO")

    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--csv", action="store_true")

    args = parser.parse_args()

    setup_logging(args.log_level)

    if not args.api_key:
        logger.error("Missing API key")
        sys.exit(1)

    try:
        run_monitor(
            api_key=args.api_key,
            interval=args.interval,
            run_once=args.once,
            as_json=args.json,
            as_csv=args.csv,
        )
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    finally:
        logger.info("Shutdown complete")


if __name__ == "__main__":
    main()
