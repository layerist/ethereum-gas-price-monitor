import argparse
import atexit
import logging
import os
import signal
import sys
import time
from random import uniform
from typing import Optional, TypedDict, Dict, Final, Literal

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# === Configuration ===
class Config:
    API_URL: Final[str] = "https://api.etherscan.io/api"
    MIN_INTERVAL: Final[int] = 10  # seconds
    RETRY_LIMIT: Final[int] = 5
    INITIAL_RETRY_DELAY: Final[int] = 5  # seconds
    TIMEOUT: Final[int] = 10  # seconds
    LOG_FORMAT: Final[str] = "%(asctime)s - %(levelname)s - %(message)s"
    LOG_DATE_FORMAT: Final[str] = "%H:%M:%S"


# === Typed Result ===
class GasPrices(TypedDict, total=False):
    SafeGasPrice: str
    ProposeGasPrice: str
    FastGasPrice: str


# === Logger ===
logger = logging.getLogger("EthereumGasMonitor")


def setup_logging(level: str = "INFO") -> None:
    """Set up logging with RichHandler if available, fallback to standard logging."""
    if logger.hasHandlers():
        return

    try:
        from rich.logging import RichHandler
        handler = RichHandler(
            rich_tracebacks=True,
            show_time=True,
            show_path=False,
            markup=True,
        )
        handler.setFormatter(logging.Formatter("%(message)s"))  # Rich handles formatting
    except ImportError:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(Config.LOG_FORMAT, datefmt=Config.LOG_DATE_FORMAT))

    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# === Requests Session ===
def create_session() -> requests.Session:
    """Create a requests session with retry adapter."""
    session = requests.Session()
    retries = Retry(
        total=Config.RETRY_LIMIT,
        backoff_factor=1,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=("GET",),
    )
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    atexit.register(session.close)
    return session


session: Final[requests.Session] = create_session()


# === Retry helper ===
def sleep_with_jitter(base_delay: int) -> int:
    """Sleep with jitter and return the next delay (exponential backoff)."""
    jitter = uniform(0.8, 1.2)
    sleep_time = base_delay * jitter
    logger.debug(f"Retrying in {sleep_time:.1f}s...")
    time.sleep(sleep_time)
    return base_delay * 2


# === Fetch Gas Prices ===
def fetch_gas_prices(api_key: str) -> Optional[GasPrices]:
    """Fetch current Ethereum gas prices from Etherscan API with retries + jitter."""
    params: Dict[str, str] = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    delay = Config.INITIAL_RETRY_DELAY

    for attempt in range(1, Config.RETRY_LIMIT + 1):
        try:
            logger.debug(f"Fetching gas prices (attempt {attempt})...")
            response = session.get(Config.API_URL, params=params, timeout=Config.TIMEOUT)
            response.raise_for_status()

            data = response.json()
            if data.get("status") == "1" and "result" in data:
                result = data["result"]
                return {
                    "SafeGasPrice": result.get("SafeGasPrice", "N/A"),
                    "ProposeGasPrice": result.get("ProposeGasPrice", "N/A"),
                    "FastGasPrice": result.get("FastGasPrice", "N/A"),
                }

            logger.error(f"API error: {data.get('message', 'Unknown')} — {data}")
            return None

        except requests.RequestException as e:
            logger.warning(f"Request failed: {e} [Attempt {attempt}/{Config.RETRY_LIMIT}]")
            if attempt < Config.RETRY_LIMIT:
                delay = sleep_with_jitter(delay)
            else:
                logger.error("All retry attempts failed.")
    return None


# === Log Prices ===
def log_gas_prices(prices: GasPrices) -> None:
    """Log gas prices in a human-friendly format."""
    try:
        from rich.table import Table
        from rich.console import Console

        table = Table(title="⛽ Ethereum Gas Prices (Gwei)")
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
            f"⛽ Gas Prices (Gwei): "
            f"Safe={prices.get('SafeGasPrice','N/A')} | "
            f"Propose={prices.get('ProposeGasPrice','N/A')} | "
            f"Fast={prices.get('FastGasPrice','N/A')}"
        )


# === Signal Handling ===
def signal_handler(sig, frame) -> None:
    """Handle termination signals for graceful shutdown."""
    logger.info("Termination signal received. Exiting gracefully.")
    sys.exit(0)


# === Interval Validation ===
def validate_interval(value: int) -> int:
    """Ensure interval meets minimum allowed value."""
    if value < Config.MIN_INTERVAL:
        logger.warning(f"Interval too short. Using minimum: {Config.MIN_INTERVAL}s")
    return max(value, Config.MIN_INTERVAL)


# === Main Monitor Loop ===
def run_monitor(api_key: str, interval: int, run_once: bool = False) -> None:
    logger.info("📡 Ethereum Gas Price Monitor started")
    interval = validate_interval(interval)

    signal.signal(signal.SIGINT, signal_handler)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, signal_handler)

    try:
        while True:
            start = time.monotonic()

            prices = fetch_gas_prices(api_key)
            if prices:
                log_gas_prices(prices)
            else:
                logger.warning("Failed to retrieve gas prices.")

            if run_once:
                break

            elapsed = time.monotonic() - start
            sleep_time = max(0, interval - elapsed)
            logger.debug(f"Sleeping {sleep_time:.1f}s before next fetch...")
            time.sleep(sleep_time)

    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down.")


# === Entry Point ===
def main() -> None:
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument(
        "--api_key",
        type=str,
        default=os.getenv("ETHERSCAN_API_KEY"),
        required=not bool(os.getenv("ETHERSCAN_API_KEY")),
        help="Etherscan API key or set ENV ETHERSCAN_API_KEY"
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.getenv("ETH_GAS_INTERVAL", "60")),
        help=f"Polling interval in seconds (min {Config.MIN_INTERVAL})"
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default=os.getenv("ETH_GAS_LOG", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set logging level (default: INFO)"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit"
    )

    args = parser.parse_args()
    setup_logging(args.log_level)

    if not args.api_key:
        logger.critical("Missing API key. Use --api_key or set ETHERSCAN_API_KEY.")
        sys.exit(1)

    run_monitor(api_key=args.api_key, interval=args.interval, run_once=args.once)


if __name__ == "__main__":
    main()
