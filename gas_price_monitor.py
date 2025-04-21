import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, Dict

# === Configuration ===
API_URL = "https://api.etherscan.io/api"
MIN_INTERVAL = 10               # Minimum interval between API requests (seconds)
RETRY_LIMIT = 5                 # Max retries on API failure
INITIAL_RETRY_DELAY = 5        # Initial delay between retries (seconds)
TIMEOUT = 10                   # Request timeout in seconds

# === Logging Setup ===
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("EthereumGasMonitor")


def fetch_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    """
    Fetch Ethereum gas prices from the Etherscan API with retry logic.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Gas prices or None on failure.
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
            break  # No point retrying if status is 0

        except requests.RequestException as e:
            logger.warning(f"Request failed: {e} (Attempt {attempt}/{RETRY_LIMIT})")
            if attempt < RETRY_LIMIT:
                time.sleep(delay)
                delay *= 2  # Exponential backoff
            else:
                logger.error("Max retries reached. Giving up.")

    return None


def signal_handler(sig, frame):
    """Gracefully handle script interruption."""
    logger.info("Shutting down gracefully...")
    sys.exit(0)


def validate_interval(interval: int) -> int:
    """Ensure the interval meets the minimum requirement."""
    if interval < MIN_INTERVAL:
        logger.warning(f"Interval too low. Using minimum of {MIN_INTERVAL} seconds.")
    return max(interval, MIN_INTERVAL)


def log_gas_prices(gas_prices: Dict[str, str]) -> None:
    """Print gas prices to the log."""
    logger.info(
        "Gas Prices (Gwei) -> Safe: %s | Propose: %s | Fast: %s",
        gas_prices["SafeGasPrice"],
        gas_prices["ProposeGasPrice"],
        gas_prices["FastGasPrice"]
    )


def main(api_key: str, interval: int) -> None:
    """Main loop for periodically fetching gas prices."""
    logger.info("Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)...")
    signal.signal(signal.SIGINT, signal_handler)

    interval = validate_interval(interval)

    while True:
        gas_prices = fetch_gas_prices(api_key)
        if gas_prices:
            log_gas_prices(gas_prices)
        else:
            logger.warning("Failed to retrieve gas prices.")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument(
        "--api_key",
        type=str,
        default=os.getenv("ETHERSCAN_API_KEY"),
        required=not bool(os.getenv("ETHERSCAN_API_KEY")),
        help="Etherscan API key (or set ETHERSCAN_API_KEY env var)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=f"Polling interval in seconds (minimum {MIN_INTERVAL}).",
    )
    args = parser.parse_args()

    main(api_key=args.api_key, interval=args.interval)
