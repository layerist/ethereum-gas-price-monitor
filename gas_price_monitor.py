import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, TypedDict, Dict

# === Configuration ===
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
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def fetch_gas_prices(api_key: str) -> Optional[GasPrices]:
    """
    Fetch Ethereum gas prices from the Etherscan API with retry logic.
    """
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    delay = INITIAL_RETRY_DELAY

    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            response = requests.get(API_URL, params=params, timeout=TIMEOUT)
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
            break  # No need to retry if API returns failure status

        except requests.RequestException as e:
            logger.warning(f"Request failed: {e} (Attempt {attempt}/{RETRY_LIMIT})")
            if attempt < RETRY_LIMIT:
                logger.info(f"Retrying in {delay} seconds...")
                time.sleep(delay)
                delay *= 2
            else:
                logger.error("Maximum retry attempts reached. Aborting.")

    return None


def log_gas_prices(prices: GasPrices) -> None:
    """Log formatted gas prices."""
    logger.info(
        "Gas Prices (Gwei) → Safe: %s | Propose: %s | Fast: %s",
        prices["SafeGasPrice"],
        prices["ProposeGasPrice"],
        prices["FastGasPrice"]
    )


def signal_handler(sig, frame) -> None:
    """Handle termination signals gracefully."""
    logger.info("Received termination signal. Exiting.")
    sys.exit(0)


def validate_interval(value: int) -> int:
    if value < MIN_INTERVAL:
        logger.warning(f"Interval too short; using minimum of {MIN_INTERVAL} seconds.")
    return max(value, MIN_INTERVAL)


def main(api_key: str, interval: int) -> None:
    logger.info("Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)")
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    interval = validate_interval(interval)

    while True:
        start_time = time.monotonic()
        gas_prices = fetch_gas_prices(api_key)

        if gas_prices:
            log_gas_prices(gas_prices)
        else:
            logger.warning("Could not retrieve gas prices.")

        elapsed = time.monotonic() - start_time
        sleep_time = max(0, interval - elapsed)
        time.sleep(sleep_time)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument(
        "--api_key",
        type=str,
        default=os.getenv("ETHERSCAN_API_KEY"),
        required=not bool(os.getenv("ETHERSCAN_API_KEY")),
        help="Etherscan API key (or set ETHERSCAN_API_KEY environment variable).",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=f"Polling interval in seconds (minimum {MIN_INTERVAL}).",
    )
    parser.add_argument(
        "--log_level",
        type=str,
        default="INFO",
        help="Logging level (DEBUG, INFO, WARNING, ERROR). Default is INFO.",
    )
    args = parser.parse_args()

    setup_logging(args.log_level)
    main(api_key=args.api_key, interval=args.interval)
