#!/usr/bin/env python3
"""
Ethereum Gas Price Monitor — v9

Highlights
----------
- Etherscan API V2 with configurable chain ID
- Decimal-safe parsing for sub-1-Gwei values
- API-key rotation with per-key cooldowns
- Retry-After support and interruptible exponential backoff
- No duplicate HTTP/API retry layers for rate-limit responses
- Drift-resistant monotonic scheduler with overrun recovery
- JSON, JSONL, CSV and terminal output
- Valid structured JSON logging
- Optional proxy, output file and non-duplicating CSV header
- Retry-After HTTP-date support and stricter CLI validation
- Thread-safe API-key pool and accurate failed-request latency metrics
- Rate-limit rotation independent from transient retry budget
- Permanent invalid-key quarantine without poisoning healthy keys
- Finite-number CLI validation (rejects NaN/Inf)
- Non-zero one-shot exit status on failed sample
- Fatal output errors instead of silent data loss
- Atomic, testable components and graceful shutdown
- Single interruptible retry layer with explicit retry classification
- Retryable HTTP status handling with Retry-After propagation
- Configurable backoff/jitter and validated proxy URLs
- Safer response previews and richer metrics

"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import random
import signal
from datetime import timezone
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from enum import Enum
from pathlib import Path
from threading import Event, Lock
from typing import Any, Final, Mapping, Sequence, TextIO
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

APP_NAME: Final = "eth-gas-monitor"
APP_VERSION: Final = "9.0"
DEFAULT_API_URL: Final = "https://api.etherscan.io/v2/api"
DEFAULT_CHAIN_ID: Final = "1"
DEFAULT_TIMEOUT: Final = (5.0, 10.0)
DEFAULT_MIN_INTERVAL: Final = 1.0
DEFAULT_KEY_COOLDOWN: Final = 60.0
DEFAULT_API_RETRIES: Final = 3
DEFAULT_TRANSPORT_RETRIES: Final = 3
DEFAULT_BACKOFF_BASE: Final = 1.7
DEFAULT_MAX_BACKOFF: Final = 60.0
DEFAULT_JITTER: Final = 0.25

logger = logging.getLogger(APP_NAME)
stop_event = Event()


class ApiStatus(str, Enum):
    OK = "1"


@dataclass(frozen=True, slots=True)
class Settings:
    api_url: str = DEFAULT_API_URL
    chain_id: str = DEFAULT_CHAIN_ID
    connect_timeout: float = DEFAULT_TIMEOUT[0]
    read_timeout: float = DEFAULT_TIMEOUT[1]
    min_interval: float = DEFAULT_MIN_INTERVAL
    key_cooldown: float = DEFAULT_KEY_COOLDOWN
    api_retries: int = DEFAULT_API_RETRIES
    transport_retries: int = DEFAULT_TRANSPORT_RETRIES
    backoff_base: float = DEFAULT_BACKOFF_BASE
    max_backoff: float = DEFAULT_MAX_BACKOFF
    max_jitter: float = DEFAULT_JITTER

    @property
    def timeout(self) -> tuple[float, float]:
        return self.connect_timeout, self.read_timeout


@dataclass(frozen=True, slots=True)
class GasPrices:
    safe: Decimal
    propose: Decimal
    fast: Decimal
    suggest_base_fee: Decimal | None = None
    gas_used_ratio: tuple[Decimal, ...] = ()

    def as_dict(self, *, numeric: bool = False) -> dict[str, Any]:
        convert = float if numeric else _decimal_text
        result: dict[str, Any] = {
            "safe": convert(self.safe),
            "propose": convert(self.propose),
            "fast": convert(self.fast),
        }
        if self.suggest_base_fee is not None:
            result["suggest_base_fee"] = convert(self.suggest_base_fee)
        if self.gas_used_ratio:
            result["gas_used_ratio"] = [convert(x) for x in self.gas_used_ratio]
        return result


@dataclass(slots=True)
class Metrics:
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rate_limits: int = 0
    transient_http_errors: int = 0
    transport_errors: int = 0
    invalid_payloads: int = 0
    invalid_keys: int = 0
    total_latency_ms: float = 0.0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def avg_latency_ms(self) -> float:
        return self.total_latency_ms / self.attempts if self.attempts else 0.0

    @property
    def uptime_seconds(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)


class EtherscanError(RuntimeError):
    pass


class InvalidPayloadError(EtherscanError):
    pass


class RateLimitError(EtherscanError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class AllKeysCoolingDown(RateLimitError):
    pass


class NoUsableApiKeys(EtherscanError):
    pass


class PermanentApiKeyError(EtherscanError):
    pass


class TransientHttpError(EtherscanError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class JsonFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "time": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def setup_logging(level: str, structured: bool) -> None:
    logger.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        JsonFormatter()
        if structured
        else logging.Formatter(
            "%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
        )
    )
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.propagate = False


def create_session(settings: Settings, proxy: str | None = None) -> requests.Session:
    # Keep retries in the application layer so waits are interruptible, key rotation is
    # deterministic, and every logical HTTP attempt is represented in Metrics.
    adapter = HTTPAdapter(max_retries=Retry(total=0), pool_connections=4, pool_maxsize=4)
    session = requests.Session()
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
        }
    )
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    return session


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidPayloadError(f"Invalid {field_name}: {value!r}") from exc
    if not parsed.is_finite() or parsed < 0:
        raise InvalidPayloadError(f"Invalid {field_name}: {value!r}")
    return parsed


def _parse_ratio(value: Any) -> tuple[Decimal, ...]:
    if value in (None, ""):
        return ()
    parts = value.split(",") if isinstance(value, str) else value
    if not isinstance(parts, (list, tuple)):
        raise InvalidPayloadError(f"Invalid gasUsedRatio: {value!r}")
    return tuple(_parse_decimal(item, "gasUsedRatio") for item in parts)


def _joined_lower(*values: Any) -> str:
    return " ".join(str(v) for v in values if v is not None).lower()


def _looks_rate_limited(*values: Any) -> bool:
    text = _joined_lower(*values)
    markers = ("rate limit", "max rate", "too many request", "throttl")
    return any(marker in text for marker in markers)


def _looks_invalid_api_key(*values: Any) -> bool:
    text = _joined_lower(*values)
    markers = (
        "invalid api key",
        "missing/invalid api key",
        "api key not activated",
        "invalidapikey",
    )
    return any(marker in text for marker in markers)


def parse_payload(payload: Any) -> GasPrices:
    if not isinstance(payload, Mapping):
        raise InvalidPayloadError("Response payload must be a JSON object")

    status = str(payload.get("status", ""))
    message = payload.get("message", "")
    result = payload.get("result")

    if status != ApiStatus.OK.value:
        if _looks_rate_limited(message, result):
            raise RateLimitError(str(result or message or "Rate limited"))
        if _looks_invalid_api_key(message, result):
            raise PermanentApiKeyError(str(result or message or "Invalid API key"))
        raise EtherscanError(f"Etherscan error: {message!s} | {result!s}")

    if not isinstance(result, Mapping):
        raise InvalidPayloadError("Missing or invalid result object")

    base_fee_raw = result.get("suggestBaseFee")
    return GasPrices(
        safe=_parse_decimal(result.get("SafeGasPrice"), "SafeGasPrice"),
        propose=_parse_decimal(result.get("ProposeGasPrice"), "ProposeGasPrice"),
        fast=_parse_decimal(result.get("FastGasPrice"), "FastGasPrice"),
        suggest_base_fee=(
            _parse_decimal(base_fee_raw, "suggestBaseFee")
            if base_fee_raw not in (None, "")
            else None
        ),
        gas_used_ratio=_parse_ratio(result.get("gasUsedRatio")),
    )


def parse_retry_after(value: str | None) -> float | None:
    """Parse Retry-After as delta-seconds or an HTTP date."""
    if not value:
        return None
    raw = value.strip()
    try:
        seconds = float(raw)
        if math.isfinite(seconds):
            return max(0.0, seconds)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(0.0, retry_at.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


class ApiKeyPool:
    def __init__(self, keys: Sequence[str], default_cooldown: float):
        unique_keys = tuple(dict.fromkeys(key.strip() for key in keys if key.strip()))
        if not unique_keys:
            raise ValueError("At least one API key is required")
        self._keys = unique_keys
        self._default_cooldown = default_cooldown
        self._cooldowns: dict[str, float] = {}
        self._disabled: set[str] = set()
        self._cursor = 0
        self._lock = Lock()

    def acquire(self) -> str:
        with self._lock:
            now = time.monotonic()
            count = len(self._keys)
            usable = 0
            for offset in range(count):
                index = (self._cursor + offset) % count
                key = self._keys[index]
                if key in self._disabled:
                    continue
                usable += 1
                if self._cooldowns.get(key, 0.0) <= now:
                    self._cursor = (index + 1) % count
                    return key
            if usable == 0:
                raise NoUsableApiKeys("All API keys are disabled")
            retry_after = self._seconds_until_available_unlocked(now)
        raise AllKeysCoolingDown("All API keys are cooling down", retry_after=retry_after)

    def cooldown(self, key: str, seconds: float | None = None) -> None:
        duration = self._default_cooldown if seconds is None else max(0.0, seconds)
        with self._lock:
            if key not in self._disabled:
                self._cooldowns[key] = time.monotonic() + duration

    def disable(self, key: str) -> None:
        with self._lock:
            self._disabled.add(key)
            self._cooldowns.pop(key, None)

    def _seconds_until_available_unlocked(self, now: float) -> float:
        deadlines = [
            self._cooldowns.get(key, now)
            for key in self._keys
            if key not in self._disabled
        ]
        if not deadlines:
            return 0.0
        return max(0.0, min(deadlines) - now)

    def seconds_until_available(self) -> float:
        with self._lock:
            return self._seconds_until_available_unlocked(time.monotonic())

    @property
    def enabled_count(self) -> int:
        with self._lock:
            return len(self._keys) - len(self._disabled)

    def __len__(self) -> int:
        return len(self._keys)


class EtherscanClient:
    def __init__(self, session: requests.Session, settings: Settings, metrics: Metrics):
        self.session = session
        self.settings = settings
        self.metrics = metrics

    def get_gas_prices(self, api_key: str) -> GasPrices:
        params = {
            "chainid": self.settings.chain_id,
            "module": "gastracker",
            "action": "gasoracle",
            "apikey": api_key,
        }
        started = time.perf_counter()
        self.metrics.attempts += 1
        try:
            response = self.session.get(
                self.settings.api_url, params=params, timeout=self.settings.timeout
            )
            logger.debug("HTTP %s", response.status_code)

            retry_after = parse_retry_after(response.headers.get("Retry-After"))
            if response.status_code == 429:
                self.metrics.rate_limits += 1
                raise RateLimitError("HTTP 429 Too Many Requests", retry_after)
            if response.status_code == 401:
                self.metrics.invalid_keys += 1
                raise PermanentApiKeyError("HTTP 401 Unauthorized")
            if response.status_code in {408, 425, 500, 502, 503, 504}:
                self.metrics.transient_http_errors += 1
                raise TransientHttpError(
                    f"HTTP {response.status_code} {response.reason}", retry_after
                )

            response.raise_for_status()
            try:
                payload = response.json()
            except requests.exceptions.JSONDecodeError as exc:
                self.metrics.invalid_payloads += 1
                preview = response.text[:200].replace("\r", " ").replace("\n", " ")
                raise InvalidPayloadError(f"Non-JSON response: {preview!r}") from exc

            try:
                prices = parse_payload(payload)
            except RateLimitError as exc:
                self.metrics.rate_limits += 1
                if exc.retry_after is None:
                    exc.retry_after = retry_after
                raise
            except PermanentApiKeyError:
                self.metrics.invalid_keys += 1
                raise
            except InvalidPayloadError:
                self.metrics.invalid_payloads += 1
                raise

            self.metrics.successes += 1
            return prices
        except Exception:
            self.metrics.failures += 1
            raise
        finally:
            self.metrics.total_latency_ms += (time.perf_counter() - started) * 1000.0


def interruptible_wait(seconds: float) -> bool:
    """Return True if interrupted by shutdown."""
    return stop_event.wait(max(0.0, seconds))


def backoff_delay(attempt: int, settings: Settings, retry_after: float | None) -> float:
    jitter = random.uniform(0.0, settings.max_jitter)
    if retry_after is not None:
        # Retry-After is a server instruction. Do not cap it below the requested wait.
        return max(0.0, retry_after) + jitter
    base = settings.backoff_base**attempt
    return min(settings.max_backoff, base + jitter)


def fetch_with_rotation(
    client: EtherscanClient,
    key_pool: ApiKeyPool,
    settings: Settings,
) -> GasPrices:
    """Fetch one sample with key rotation and bounded, interruptible retries.

    Rate-limited and permanently-invalid keys do not consume transient budgets.
    Transport/HTTP failures use ``transport_retries``; valid HTTP responses with
    malformed/transient API payloads use ``api_retries``.
    """
    last_error: BaseException | None = None
    api_failures = 0
    transport_failures = 0
    cooldown_waits = 0

    while not stop_event.is_set():
        try:
            key = key_pool.acquire()
        except NoUsableApiKeys:
            raise
        except AllKeysCoolingDown as exc:
            last_error = exc
            if cooldown_waits >= settings.api_retries:
                raise
            cooldown_waits += 1
            delay = backoff_delay(cooldown_waits - 1, settings, exc.retry_after)
            logger.warning("All enabled API keys cooling down; retrying in %.2fs", delay)
            if interruptible_wait(delay):
                raise InterruptedError("Shutdown requested")
            continue

        try:
            return client.get_gas_prices(key)
        except PermanentApiKeyError as exc:
            last_error = exc
            key_pool.disable(key)
            logger.error(
                "API key rejected permanently; disabled for this process (%d/%d enabled)",
                key_pool.enabled_count,
                len(key_pool),
            )
            continue
        except RateLimitError as exc:
            last_error = exc
            cooldown = (
                exc.retry_after
                if exc.retry_after is not None and exc.retry_after > 0.0
                else settings.key_cooldown
            )
            key_pool.cooldown(key, cooldown)
            logger.warning("API key rate-limited; cooling it for %.2fs", cooldown)
            continue
        except (requests.RequestException, TransientHttpError) as exc:
            last_error = exc
            client.metrics.transport_errors += isinstance(exc, requests.RequestException)
            if transport_failures >= settings.transport_retries:
                raise
            retry_after = exc.retry_after if isinstance(exc, TransientHttpError) else None
            delay = backoff_delay(transport_failures, settings, retry_after)
            transport_failures += 1
            logger.warning(
                "Transport/server failure (%s); retry %d/%d in %.2fs",
                exc, transport_failures, settings.transport_retries, delay,
            )
            if interruptible_wait(delay):
                raise InterruptedError("Shutdown requested")
        except (InvalidPayloadError, EtherscanError) as exc:
            last_error = exc
            if api_failures >= settings.api_retries:
                raise
            delay = backoff_delay(api_failures, settings, None)
            api_failures += 1
            logger.warning(
                "API response failure (%s); retry %d/%d in %.2fs",
                exc, api_failures, settings.api_retries, delay,
            )
            if interruptible_wait(delay):
                raise InterruptedError("Shutdown requested")

    if last_error is not None:
        raise EtherscanError("Gas request failed") from last_error
    raise InterruptedError("Shutdown requested")


class OutputWriter:
    def __init__(
        self,
        mode: str,
        stream: TextIO,
        include_header: bool,
        flush: bool,
    ) -> None:
        self.mode = mode
        self.stream = stream
        self.flush = flush
        self._csv = csv.writer(stream, lineterminator="\n") if mode == "csv" else None
        self._header_written = False
        self._console: Any | None = None
        if mode == "csv" and include_header:
            self._write_csv_header()

    def _write_csv_header(self) -> None:
        assert self._csv is not None
        self._csv.writerow(
            ["timestamp", "chain_id", "safe_gwei", "propose_gwei", "fast_gwei", "base_fee_gwei"]
        )
        self._header_written = True

    def write(self, prices: GasPrices, chain_id: str) -> None:
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        if self.mode in {"json", "jsonl"}:
            payload = {"timestamp": timestamp, "chain_id": chain_id, **prices.as_dict()}
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            self.stream.write(text + "\n")
        elif self.mode == "csv":
            assert self._csv is not None
            self._csv.writerow(
                [
                    timestamp,
                    chain_id,
                    _decimal_text(prices.safe),
                    _decimal_text(prices.propose),
                    _decimal_text(prices.fast),
                    _decimal_text(prices.suggest_base_fee) if prices.suggest_base_fee is not None else "",
                ]
            )
        else:
            self._write_terminal(prices)
        if self.flush:
            self.stream.flush()

    def _write_terminal(self, prices: GasPrices) -> None:
        try:
            from rich.console import Console
            from rich.table import Table
        except ImportError:
            self.stream.write(
                "Gas (Gwei) | Safe=%s | Propose=%s | Fast=%s%s\n"
                % (
                    _decimal_text(prices.safe),
                    _decimal_text(prices.propose),
                    _decimal_text(prices.fast),
                    (
                        f" | Base fee={_decimal_text(prices.suggest_base_fee)}"
                        if prices.suggest_base_fee is not None
                        else ""
                    ),
                )
            )
            return

        if self._console is None:
            self._console = Console(file=self.stream)
        table = Table(title="Ethereum Gas (Gwei)")
        for column in ("Safe", "Propose", "Fast", "Base fee"):
            table.add_column(column, justify="right")
        table.add_row(
            _decimal_text(prices.safe),
            _decimal_text(prices.propose),
            _decimal_text(prices.fast),
            _decimal_text(prices.suggest_base_fee) if prices.suggest_base_fee is not None else "—",
        )
        self._console.print(table)


def run_monitor(
    client: EtherscanClient,
    key_pool: ApiKeyPool,
    writer: OutputWriter,
    settings: Settings,
    interval: float,
    run_once: bool,
) -> bool:
    """Run monitor; return whether at least one sample was written successfully."""
    interval = max(interval, settings.min_interval)
    logger.info(
        "Started %s v%s | chain=%s | interval=%.3fs | keys=%d",
        APP_NAME,
        APP_VERSION,
        settings.chain_id,
        interval,
        len(key_pool),
    )

    next_run = time.monotonic()
    had_success = False
    while not stop_event.is_set():
        try:
            prices = fetch_with_rotation(client, key_pool, settings)
        except InterruptedError:
            break
        except requests.RequestException as exc:
            logger.error("Network error: %s", exc)
        except EtherscanError as exc:
            logger.error("Etherscan error: %s", exc)
        except Exception:
            logger.exception("Unexpected fetch error")
        else:
            # Output errors are intentionally fatal. Continuing after disk-full, broken
            # pipe, encoding or filesystem errors would silently lose samples.
            writer.write(prices, settings.chain_id)
            had_success = True

        if run_once:
            break

        next_run += interval
        now = time.monotonic()
        if next_run <= now:
            missed = int((now - next_run) // interval) + 1
            next_run += missed * interval
            logger.debug("Skipped %d overdue scheduler tick(s)", missed)
        if interruptible_wait(next_run - time.monotonic()):
            break

    return had_success


def env_api_keys() -> list[str]:
    values: list[str] = []
    single = os.getenv("ETHERSCAN_API_KEY", "")
    multiple = os.getenv("ETHERSCAN_API_KEYS", "")
    if single:
        values.append(single)
    if multiple:
        normalized = multiple.replace(";", ",").replace("\n", ",")
        values.extend(normalized.split(","))
    return [value.strip() for value in values if value.strip()]


def positive_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return parsed


def nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def positive_int_text(value: str) -> str:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return str(parsed)


def http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute http(s) URL")
    return value


def proxy_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https", "socks4", "socks5", "socks5h"} or not parsed.netloc:
        raise argparse.ArgumentTypeError("must be an absolute HTTP(S) or SOCKS proxy URL")
    return value


def nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number zero or greater")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Monitor Etherscan gas-price recommendations")
    parser.add_argument("--api-key", action="append", default=[], help="repeat for multiple keys")
    parser.add_argument("--chain-id", type=positive_int_text, default=os.getenv("ETHERSCAN_CHAIN_ID", DEFAULT_CHAIN_ID))
    parser.add_argument("--api-url", type=http_url, default=os.getenv("ETHERSCAN_API_URL", DEFAULT_API_URL))
    parser.add_argument("--interval", type=positive_float, default=60.0)
    parser.add_argument("--min-interval", type=positive_float, default=DEFAULT_MIN_INTERVAL)
    parser.add_argument("--connect-timeout", type=positive_float, default=DEFAULT_TIMEOUT[0])
    parser.add_argument("--read-timeout", type=positive_float, default=DEFAULT_TIMEOUT[1])
    parser.add_argument("--api-retries", type=nonnegative_int, default=DEFAULT_API_RETRIES)
    parser.add_argument("--transport-retries", type=nonnegative_int, default=DEFAULT_TRANSPORT_RETRIES)
    parser.add_argument("--key-cooldown", type=positive_float, default=DEFAULT_KEY_COOLDOWN)
    parser.add_argument("--proxy", type=proxy_url, default=os.getenv("HTTPS_PROXY") or os.getenv("HTTP_PROXY"))
    parser.add_argument("--backoff-base", type=positive_float, default=DEFAULT_BACKOFF_BASE)
    parser.add_argument("--max-backoff", type=positive_float, default=DEFAULT_MAX_BACKOFF)
    parser.add_argument("--max-jitter", type=nonnegative_float, default=DEFAULT_JITTER)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--output", type=Path, help="append output to this file")
    parser.add_argument("--no-flush", action="store_true", help="do not flush after every sample")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"), default="INFO")
    parser.add_argument("--structured-logs", action="store_true")
    parser.add_argument("--version", action="version", version=f"%(prog)s {APP_VERSION}")

    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="JSON Lines output")
    output.add_argument("--csv", action="store_true")
    parser.add_argument("--no-csv-header", action="store_true")
    return parser


def install_signal_handlers() -> None:
    def handler(signum: int, _frame: Any) -> None:
        logger.info("Signal %s received; shutting down", signum)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    stop_event.clear()
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.structured_logs)
    install_signal_handlers()

    keys = list(dict.fromkeys([*args.api_key, *env_api_keys()]))
    if not keys:
        logger.error("Missing API key: use --api-key or ETHERSCAN_API_KEY(S)")
        return 2

    settings = Settings(
        api_url=args.api_url,
        chain_id=str(args.chain_id),
        connect_timeout=args.connect_timeout,
        read_timeout=args.read_timeout,
        min_interval=args.min_interval,
        key_cooldown=args.key_cooldown,
        api_retries=args.api_retries,
        transport_retries=args.transport_retries,
        backoff_base=args.backoff_base,
        max_backoff=args.max_backoff,
        max_jitter=args.max_jitter,
    )
    metrics = Metrics()
    session = create_session(settings, args.proxy)
    stream: TextIO = sys.stdout
    owned_stream: TextIO | None = None

    try:
        output_was_empty = True
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            output_was_empty = not args.output.exists() or args.output.stat().st_size == 0
            owned_stream = args.output.open("a", encoding="utf-8", newline="")
            stream = owned_stream

        mode = "csv" if args.csv else "jsonl" if args.json else "terminal"
        writer = OutputWriter(
            mode=mode,
            stream=stream,
            include_header=not args.no_csv_header and output_was_empty,
            flush=not args.no_flush,
        )
        client = EtherscanClient(session, settings, metrics)
        key_pool = ApiKeyPool(keys, settings.key_cooldown)
        had_success = run_monitor(
            client, key_pool, writer, settings, args.interval, args.once
        )
        if args.once and not had_success:
            return 1
        return 0
    except (OSError, UnicodeError) as exc:
        logger.error("Output error: %s", exc)
        return 1
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
        return 130
    finally:
        session.close()
        if owned_stream is not None:
            owned_stream.close()
        logger.info(
            "Metrics | attempts=%d | success=%d | failures=%d | rate_limits=%d "
            "| transport_errors=%d | transient_http=%d | invalid_payloads=%d "
            "| invalid_keys=%d | avg_latency=%.2fms | uptime=%.1fs",
            metrics.attempts,
            metrics.successes,
            metrics.failures,
            metrics.rate_limits,
            metrics.transport_errors,
            metrics.transient_http_errors,
            metrics.invalid_payloads,
            metrics.invalid_keys,
            metrics.avg_latency_ms,
            metrics.uptime_seconds,
        )


if __name__ == "__main__":
    raise SystemExit(main())
