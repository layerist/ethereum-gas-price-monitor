import argparse
import atexit
import json
import logging
import os
import signal
import sys
import time
from enum import Enum
from typing import Optional, TypedDict, Dict, Final

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from tenacity import (
    retry, stop_after_attempt, wait_exponential_jitter,
    retry_if_exception_type, before_sleep_log
)


# === Configuration ===
class Config:
    API_URL: Final[str] = "https://api.etherscan.io/api"
    MIN_INTERVAL: Final[int] = 10
    RETRY_LIMIT: Final[int] = 5
    TIMEOUT: Final[int] = 10
    LOG_FORMAT: Final[str] = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"


class ApiStatus(Enum):
    OK = "1"
    ERROR = "0"
    NOTOK = "NOTOK"


# === Typed Result ===
class GasPrices(TypedDict, total=False):
    SafeGasPrice: str
    ProposeGasPrice: str
    FastGasPrice: str


# === Logger ===
logger = logging.getLogger("EthereumGasMonitor")


def setup_logging(level: str = "INFO") -> None:
    """Configure logging; prefer Rich if installed."""
    if logger.hasHandlers():
        logger.handlers.clear()

    try:
        from rich.logging import RichHandler
        handler = RichHandler(
            rich_tracebacks=False,
            show_time=True,
            show_path=False,
            markup=True,
        )
        fmt = "%(message)s"
    except ImportError:
        handler = logging.StreamHandler()
        fmt = Config.LOG_FORMAT

    handler.setFormatter(logging.Formatter(fmt, datefmt=Config.LOG_DATE_FORMAT))

    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# === HTTP Session ===
def create_session() -> requests.Session:
    """Create a shared session with retry strategy."""
    session = requests.Session()

    retries = Retry(
        total=Config.RETRY_LIMIT,
        backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET",),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retries)

    session.mount("https://", adapter)
    session.mount("http://", adapter)

    atexit.register(session.close)
    return session


session: Final[requests.Session] = create_session()


# === Fetch Gas Prices ===
@retry(
    stop=stop_after_attempt(Config.RETRY_LIMIT),
    wait=wait_exponential_jitter(initial=1, max=20),
    retry=retry_if_exception_type(requests.RequestException),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def fetch_gas_prices(api_key: str) -> Optional[GasPrices]:
    """Fetch current Ethereum gas prices from Etherscan API."""
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    response = session.get(Config.API_URL, params=params, timeout=Config.TIMEOUT)
    response.raise_for_status()

    try:
        data = response.json()
    except json.JSONDecodeError:
        logger.error("Invalid JSON received from API.")
        return None

    if not isinstance(data, dict):
        logger.error("Unexpected JSON structure: %s", data)
        return None

    status = data.get("status")
    result = data.get("result")
    message = str(data.get("message", "")).upper()

    if status == ApiStatus.OK.value and isinstance(result, dict):
        return GasPrices(
            SafeGasPrice=result.get("SafeGasPrice", "N/A"),
            ProposeGasPrice=result.get("ProposeGasPrice", "N/A"),
            FastGasPrice=result.get("FastGasPrice", "N/A")
        )

    if message == ApiStatus.NOTOK.value:
        logger.warning("⚠️ API rate limit or temporary error: %s", data.get("result"))
        return None

    logger.error("Unexpected API response: %s", data)
    return None


# === Display ===
def display_gas_prices(prices: GasPrices, as_json: bool) -> None:
    if as_json:
        print(json.dumps(prices, indent=2))
        return

    try:
        from rich.table import Table
        from rich.console import Console

        table = Table(title="⛽ Ethereum Gas Prices (Gwei)", expand=True)
        table.add_column("Safe", justify="center")
        table.add_column("Propose", justify="center")
        table.add_column("Fast", justify="center")
        table.add_row(
            prices.get("SafeGasPrice", "N/A"),
            prices.get("ProposeGasPrice", "N/A"),
            prices.get("FastGasPrice", "N/A"),
        )
        Console().print(table)
    except ImportError:
        logger.info(
            f"⛽ Gas: Safe={prices.get('SafeGasPrice')} | "
            f"Propose={prices.get('ProposeGasPrice')} | "
            f"Fast={prices.get('FastGasPrice')}"
        )


# === Signal Handling ===
def handle_exit_signal(signum, _frame):
    logger.info("🛑 Received signal %s. Exiting...", signum)
    sys.exit(0)


# === Helper ===
def validate_interval(value: int) -> int:
    if value < Config.MIN_INTERVAL:
        logger.warning(f"Polling interval too short; using minimum {Config.MIN_INTERVAL}s")
    return max(value, Config.MIN_INTERVAL)


# === Main Loop ===
def run_monitor(api_key: str, interval: int, run_once: bool, as_json: bool) -> None:
    logger.info("📡 Ethereum Gas Monitor started")
    interval = validate_interval(interval)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, handle_exit_signal)
        except Exception:
            pass  # ignore on Windows

    while True:
        start = time.monotonic()

        try:
            prices = fetch_gas_prices(api_key)
            if prices:
                display_gas_prices(prices, as_json)
            else:
                logger.warning("⚠️ No data received.")
        except Exception as e:
            logger.error("Error fetching gas prices: %s", e)

        if run_once:
            break

        sleep_time = max(0, interval - (time.monotonic() - start))
        logger.debug(f"Sleeping for {sleep_time:.1f}s")
        time.sleep(sleep_time)


# === Entry Point ===
def main():
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument("--api_key", type=str, default=os.getenv("ETHERSCAN_API_KEY"))
    parser.add_argument("--interval", type=int, default=int(os.getenv("ETH_GAS_INTERVAL", "60")))
    parser.add_argument("--log_level", type=str, default=os.getenv("ETH_GAS_LOG", "INFO"))
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--json", action="store_true")

    args = parser.parse_args()
    setup_logging(args.log_level)

    if not args.api_key:
        logger.error("❌ Missing Etherscan API key")
        sys.exit(1)

    run_monitor(
        api_key=args.api_key,
        interval=args.interval,
        run_once=args.once,
        as_json=args.json,
    )


if __name__ == "__main__":
    main()
