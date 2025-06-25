import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, TypedDict

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
    if logger.handlers:
        return  # prevent duplicate handlers

    handler = logging.StreamHandler()
    formatter = logging.Formatter("\033[92m%(asctime)s\033[0m - \033[94m%(levelname)s\033[0m - %(message)s")
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


# === Gas Price Fetching ===
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

            logger.error(f"Etherscan API error: {data.get('message', 'Unknown error')}")
            return None  # No retry for logical errors

        except requests.RequestException as e:
            logger.warning(f"Request failed: {e} (Attempt {attempt}/{Config.RETRY_LIMIT})")
            if attempt < Config.RETRY_LIMIT:
                logger.debug(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                delay *= 2
            else:
                logger.error("Maximum retry attempts reached.")
                return None

    return None


# === Logging Output ===
def log_gas_prices(prices: GasPrices) -> None:
    logger.info(
        f"\033[93mGas Prices (Gwei):\033[0m Safe={prices['SafeGasPrice']} | "
        f"Propose={prices['ProposeGasPrice']} | Fast={prices['FastGasPrice']}"
    )


# === Signal Handling ===
def signal_handler(sig, frame) -> None:
    logger.info("Termination signal received. Exiting gracefully.")
    sys.exit(0)


# === Main Logic ===
def validate_interval(value: int) -> int:
    if value < Config.MIN_INTERVAL:
        logger.warning(f"Interval too short; using minimum of {Config.MIN_INTERVAL} seconds.")
    return max(value, Config.MIN_INTERVAL)


def run_monitor(api_key: str, interval: int, run_once: bool = False) -> None:
    logger.info("Ethereum Gas Price Monitor started (Press Ctrl+C to stop)")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    interval = validate_interval(interval)

    try:
        while True:
            start_time = time.monotonic()
            prices = fetch_gas_prices(api_key)

            if prices:
                log_gas_prices(prices)
            else:
                logger.warning("Failed to retrieve gas prices.")

            if run_once:
                break

            elapsed = time.monotonic() - start_time
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
        help="Etherscan API key (or set ETHERSCAN_API_KEY env variable)."
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=f"Polling interval in seconds (minimum {Config.MIN_INTERVAL})."
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Default is INFO."
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run once and exit (non-daemon mode)."
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    run_monitor(api_key=args.api_key, interval=args.interval, run_once=args.once)


if __name__ == "__main__":
    main()
