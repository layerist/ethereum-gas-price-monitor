#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor — Production Grade (Improved)

Features
--------
- Drift-free scheduler (monotonic)
- Adaptive rate-limit handling
- API key rotation + cooldown
- Retry-After support
- Rich terminal UI
- Structured logging
- Metrics tracking
- Graceful shutdown
- Hardened payload validation
- Transport retry separation
- Efficient hot loop
"""

from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import random
import signal
import sys
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from threading import Event
from typing import TypedDict, Any, Final, Sequence

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ============================================================
# Config
# ============================================================

@dataclass(frozen=True, slots=True)
class Config:
    API_URL: Final[str] = "https://api.etherscan.io/api"

    USER_AGENT: Final[str] = "eth-gas-monitor/5.0"

    CONNECT_TIMEOUT: Final[int] = 5
    READ_TIMEOUT: Final[int] = 10

    MIN_INTERVAL: Final[int] = 10

    MAX_TRANSPORT_RETRIES: Final[int] = 3
    MAX_API_RETRIES: Final[int] = 3

    API_BACKOFF_BASE: Final[float] = 1.7
    MAX_JITTER: Final[float] = 0.25

    API_KEY_COOLDOWN: Final[int] = 60

    LOG_FORMAT: Final[str] = (
        "%(asctime)s - %(levelname)s - %(message)s"
    )
    LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"


class ApiStatus(str, Enum):
    OK = "1"


# ============================================================
# Types
# ============================================================

class GasPrices(TypedDict):
    safe: int
    propose: int
    fast: int


@dataclass(slots=True)
class Metrics:
    requests: int = 0
    failures: int = 0
    rate_limits: int = 0
    total_latency_ms: float = 0.0

    @property
    def avg_latency_ms(self) -> float:
        if self.requests == 0:
            return 0.0
        return self.total_latency_ms / self.requests


# ============================================================
# Exceptions
# ============================================================

class EtherscanError(RuntimeError):
    pass


class InvalidPayloadError(ValueError):
    pass


class RateLimitError(EtherscanError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


# ============================================================
# Logging
# ============================================================

logger = logging.getLogger("eth-gas-monitor")


def setup_logging(level: str, structured: bool) -> None:
    logger.handlers.clear()

    lvl = logging._nameToLevel.get(
        level.upper(),
        logging.INFO
    )

    handler = logging.StreamHandler()

    if structured:
        formatter = logging.Formatter(
            '{"time":"%(asctime)s",'
            '"level":"%(levelname)s",'
            '"message":"%(message)s"}'
        )
    else:
        formatter = logging.Formatter(
            Config.LOG_FORMAT,
            datefmt=Config.LOG_DATE_FORMAT,
        )

    handler.setFormatter(formatter)

    logger.addHandler(handler)
    logger.setLevel(lvl)


# ============================================================
# HTTP Session
# ============================================================

def create_session() -> requests.Session:
    session = requests.Session()

    retry_strategy = Retry(
        total=Config.MAX_TRANSPORT_RETRIES,
        connect=Config.MAX_TRANSPORT_RETRIES,
        read=Config.MAX_TRANSPORT_RETRIES,
        backoff_factor=0.5,
        allowed_methods=frozenset({"GET"}),
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        pool_connections=20,
        pool_maxsize=20,
        max_retries=retry_strategy,
        pool_block=False,
    )

    session.mount("https://", adapter)

    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "User-Agent": Config.USER_AGENT,
        }
    )

    atexit.register(session.close)
    return session


SESSION: Final = create_session()

TIMEOUT: Final = (
    Config.CONNECT_TIMEOUT,
    Config.READ_TIMEOUT,
)


# ============================================================
# Parsing
# ============================================================

def _parse_int(value: Any, field: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise InvalidPayloadError(
            f"Invalid {field}: {value!r}"
        )


def parse_payload(payload: Any) -> GasPrices:
    if not isinstance(payload, dict):
        raise InvalidPayloadError(
            "Payload must be object"
        )

    if payload.get("status") != ApiStatus.OK.value:
        result = str(payload.get("result", ""))
        message = str(payload.get("message", ""))

        if "rate limit" in result.lower():
            raise RateLimitError(result)

        raise EtherscanError(
            f"{message} | {result}"
        )

    result = payload.get("result")

    if not isinstance(result, dict):
        raise InvalidPayloadError(
            "Missing result"
        )

    return {
        "safe": _parse_int(
            result.get("SafeGasPrice"),
            "SafeGasPrice"
        ),
        "propose": _parse_int(
            result.get("ProposeGasPrice"),
            "ProposeGasPrice"
        ),
        "fast": _parse_int(
            result.get("FastGasPrice"),
            "FastGasPrice"
        ),
    }


# ============================================================
# API Key Rotation
# ============================================================

class ApiKeyPool:
    def __init__(self, keys: Sequence[str]):
        self._keys = deque(keys)
        self._cooldowns: dict[str, float] = {}

    def get(self) -> str:
        now = time.monotonic()

        for _ in range(len(self._keys)):
            key = self._keys[0]
            self._keys.rotate(-1)

            cooldown_until = (
                self._cooldowns.get(key, 0)
            )

            if cooldown_until <= now:
                return key

        raise RateLimitError(
            "All API keys cooling down"
        )

    def cooldown(self, key: str) -> None:
        self._cooldowns[key] = (
            time.monotonic()
            + Config.API_KEY_COOLDOWN
        )


# ============================================================
# Fetch
# ============================================================

def fetch_gas_prices(
    api_key: str,
    metrics: Metrics,
) -> GasPrices:

    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    for attempt in range(
        Config.MAX_API_RETRIES + 1
    ):

        started_ns = time.perf_counter_ns()

        try:
            response = SESSION.get(
                Config.API_URL,
                params=params,
                timeout=TIMEOUT,
            )

            latency_ms = (
                time.perf_counter_ns()
                - started_ns
            ) / 1_000_000

            metrics.requests += 1
            metrics.total_latency_ms += latency_ms

            response.raise_for_status()

            payload = response.json()

            result = parse_payload(payload)

            logger.debug(
                "Latency: %.2f ms",
                latency_ms
            )

            return result

        except RateLimitError:
            metrics.rate_limits += 1

            if (
                attempt
                >= Config.MAX_API_RETRIES
            ):
                raise

            retry_after = (
                Config.API_BACKOFF_BASE
                ** attempt
            )

            logger.warning(
                "Rate limited "
                "(retry %.2fs)",
                retry_after
            )

            time.sleep(retry_after)

    raise RuntimeError("Unreachable")


# ============================================================
# Output
# ============================================================

USE_RICH = False

try:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    USE_RICH = True

except ImportError:
    console = None


def display(
    prices: GasPrices,
    as_json: bool,
    as_csv: bool,
) -> None:

    if as_json:
        print(
            json.dumps(
                prices,
                separators=(",", ":")
            )
        )
        return

    if as_csv:
        print(
            f"{prices['safe']},"
            f"{prices['propose']},"
            f"{prices['fast']}"
        )
        return

    if USE_RICH:
        table = Table(
            title="Ethereum Gas (Gwei)",
            expand=True
        )

        table.add_column(
            "Safe",
            justify="center"
        )
        table.add_column(
            "Propose",
            justify="center"
        )
        table.add_column(
            "Fast",
            justify="center"
        )

        table.add_row(
            str(prices["safe"]),
            str(prices["propose"]),
            str(prices["fast"]),
        )

        console.print(table)

    else:
        logger.info(
            "Gas | Safe=%d "
            "| Propose=%d "
            "| Fast=%d",
            prices["safe"],
            prices["propose"],
            prices["fast"],
        )


# ============================================================
# Runtime
# ============================================================

stop_event = Event()


def handle_exit_signal(
    signum,
    _frame
) -> None:
    logger.info(
        "Signal %s received",
        signum
    )
    stop_event.set()


def normalize_interval(
    interval: int
) -> int:
    return max(
        interval,
        Config.MIN_INTERVAL
    )


# ============================================================
# Monitor Loop
# ============================================================

def run_monitor(
    api_keys: Sequence[str],
    interval: int,
    run_once: bool,
    as_json: bool,
    as_csv: bool,
) -> None:

    logger.info(
        "Started Ethereum Gas Monitor"
    )

    interval = normalize_interval(
        interval
    )

    metrics = Metrics()
    key_pool = ApiKeyPool(api_keys)

    start_time = time.monotonic()
    tick = 0

    while not stop_event.is_set():

        try:
            api_key = key_pool.get()

            prices = fetch_gas_prices(
                api_key,
                metrics,
            )

            display(
                prices,
                as_json,
                as_csv,
            )

        except RateLimitError:
            logger.warning(
                "Key exhausted, cooling down"
            )

            try:
                key_pool.cooldown(api_key)
            except Exception:
                pass

        except requests.RequestException as exc:
            metrics.failures += 1
            logger.error(
                "Network error: %s",
                exc,
            )

        except Exception:
            metrics.failures += 1
            logger.exception(
                "Unexpected error"
            )

        if run_once:
            break

        tick += 1

        next_run = (
            start_time
            + tick * interval
        )

        jitter = random.uniform(
            0,
            Config.MAX_JITTER,
        )

        sleep_for = (
            next_run
            - time.monotonic()
            + jitter
        )

        if sleep_for > 0:
            stop_event.wait(sleep_for)

    logger.info(
        "Metrics | req=%d "
        "| fail=%d "
        "| rate_limit=%d "
        "| avg_latency=%.2fms",
        metrics.requests,
        metrics.failures,
        metrics.rate_limits,
        metrics.avg_latency_ms,
    )


# ============================================================
# Main
# ============================================================

def main() -> None:

    parser = argparse.ArgumentParser(
        description="Ethereum Gas Monitor"
    )

    parser.add_argument(
        "--api-key",
        action="append",
    )

    parser.add_argument(
        "--interval",
        type=int,
        default=60,
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
    )

    parser.add_argument(
        "--once",
        action="store_true",
    )

    parser.add_argument(
        "--json",
        action="store_true",
    )

    parser.add_argument(
        "--csv",
        action="store_true",
    )

    parser.add_argument(
        "--structured-logs",
        action="store_true",
    )

    args = parser.parse_args()

    setup_logging(
        args.log_level,
        args.structured_logs,
    )

    api_keys = (
        args.api_key
        or [
            os.getenv(
                "ETHERSCAN_API_KEY"
            )
        ]
    )

    api_keys = [
        x.strip()
        for x in api_keys
        if x and x.strip()
    ]

    if not api_keys:
        logger.error(
            "Missing API key"
        )
        sys.exit(1)

    for sig in (
        signal.SIGINT,
        signal.SIGTERM,
    ):
        try:
            signal.signal(
                sig,
                handle_exit_signal
            )
        except Exception:
            pass

    try:
        run_monitor(
            api_keys=api_keys,
            interval=args.interval,
            run_once=args.once,
            as_json=args.json,
            as_csv=args.csv,
        )

    except KeyboardInterrupt:
        logger.info(
            "Interrupted by user"
        )

    finally:
        logger.info(
            "Shutdown complete"
        )


if __name__ == "__main__":
    main()
