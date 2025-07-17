import argparse
import logging
import os
import signal
import sys
import time
from random import uniform
from typing import Literal, Optional, TypedDict

import requests


# === Configuration ===
class Config:
    API_URL = "https://api.etherscan.io/api"
    MIN_INTERVAL = 10
    RETRY_LIMIT = 5
    INITIAL_RETRY_DELAY = 5
    TIMEOUT = 10


# === Typed Result ===
class GasPrices(TypedDict):
    SafeGasPrice: str
    ProposeGasPrice: str
    FastGasPrice: str


# === Logging ===
logger = logging.getLogger("EthereumGasMonitor")


def setup_logging(level: str = "INFO") -> None:
    if logger.hasHandlers():
        return

    try:
        from rich.logging import RichHandler
        handler = RichHandler(rich_tracebacks=True)
    except ImportError:
        handler = logging.StreamHandler()

    formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s", datefmt="%H:%M:%S"
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# === Fetch Gas Prices ===
def fetch_gas_prices(api_key: str) -> Optional[GasPrices]:
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    delay = Config.INITIAL_RETRY_DELAY

    for attempt in range(1, Config.RETRY_LIMIT + 1):
        try:
            response = requests.get(Config.API_URL, params=params, timeout=Config.TIMEOUT)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "1" and "result" in data:
                result = data["result"]
                return {
                    "SafeGasPrice": result.get("SafeGasPrice", "N/A"),
                    "ProposeGasPrice": result.get("ProposeGasPrice", "N/A"),
                    "FastGasPrice": result.get("FastGasPrice", "N/A"),
                }

            logger.error(f"API responded with error: {data.get('message', 'Unknown')} — {data}")
            return None

        except requests.RequestException as e:
            logger.warning(f"[Attempt {attempt}/{Config.RETRY_LIMIT}] Request failed: {e}")
            if attempt < Config.RETRY_LIMIT:
                jitter = uniform(0.5, 1.5)
                sleep_time = delay * jitter
                logger.debug(f"Retrying in {sleep_time:.1f} seconds...")
                time.sleep(sleep_time)
                delay *= 2
            else:
                logger.error("All retry attempts failed.")
                return None

    return None


# === Log Prices ===
def log_gas_prices(prices: GasPrices) -> None:
    logger.info(
        f"⛽ Gas Prices (Gwei): Safe = {prices['SafeGasPrice']} | "
        f"Propose = {prices['ProposeGasPrice']} | Fast = {prices['FastGasPrice']}"
    )


# === Signal Handling ===
def signal_handler(sig, frame):
    logger.info("Termination signal received. Exiting gracefully.")
    sys.exit(0)


# === Validate interval ===
def validate_interval(value: int) -> int:
    if value < Config.MIN_INTERVAL:
        logger.warning(f"Interval too short, using minimum: {Config.MIN_INTERVAL}s")
    return max(value, Config.MIN_INTERVAL)


# === Monitor Logic ===
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
            time.sleep(max(0, interval - elapsed))
    except KeyboardInterrupt:
        logger.info("Interrupted by user. Shutting down.")


# === Entry Point ===
def main():
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
        default=60,
        help=f"Polling interval in seconds (min {Config.MIN_INTERVAL})"
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Set the logging level (default: INFO)"
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
