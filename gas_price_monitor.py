import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, Dict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("EthereumGasMonitor")

# Constants
API_URL = "https://api.etherscan.io/api"
MIN_INTERVAL = 10  # Minimum interval between API requests (seconds)
RETRY_LIMIT = 5  # Max retries on API failure
RETRY_DELAY = 5  # Delay between retries (seconds)
TIMEOUT = 10  # Request timeout in seconds


def fetch_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    """
    Fetch Ethereum gas prices from the Etherscan API with retry logic.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Gas prices as a dictionary or None on failure.
    """
    params = {
        "module": "gastracker",
        "action": "gasoracle",
        "apikey": api_key,
    }

    for attempt in range(1, RETRY_LIMIT + 1):
        try:
            response = requests.get(API_URL, params=params, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()

            if data.get("status") == "1" and "result" in data:
                return {key: data["result"].get(key, "N/A") for key in ("SafeGasPrice", "ProposeGasPrice", "FastGasPrice")}

            logger.error(f"API error: {data.get('message', 'Unknown error')}")
            return None
        except requests.RequestException as e:
            logger.error(f"Request failed: {e} (Attempt {attempt}/{RETRY_LIMIT})")
            if attempt < RETRY_LIMIT:
                time.sleep(RETRY_DELAY)
            else:
                logger.error("Max retries reached. Giving up.")
    return None


def signal_handler(sig, frame):
    """Handle script termination gracefully."""
    logger.info("Shutting down gracefully...")
    sys.exit(0)


def validate_interval(interval: int) -> int:
    """Ensure the interval is above the minimum threshold."""
    return max(interval, MIN_INTERVAL)


def log_gas_prices(gas_prices: Dict[str, str]):
    """Log gas prices at the INFO level."""
    logger.info(
        "Gas Prices (Gwei) -> Safe: %(SafeGasPrice)s, Propose: %(ProposeGasPrice)s, Fast: %(FastGasPrice)s",
        gas_prices,
    )


def main(api_key: str, interval: int):
    """
    Main loop to fetch and log Ethereum gas prices periodically.
    """
    logger.info("Starting Ethereum Gas Price Monitor... (Press Ctrl+C to stop)")
    signal.signal(signal.SIGINT, signal_handler)
    interval = validate_interval(interval)

    while True:
        gas_prices = fetch_gas_prices(api_key)
        if gas_prices:
            log_gas_prices(gas_prices)
        else:
            logger.warning("Failed to retrieve gas prices. Retrying...")
        time.sleep(interval)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument(
        "--api_key",
        type=str,
        default=os.getenv("ETHERSCAN_API_KEY"),
        required=not os.getenv("ETHERSCAN_API_KEY"),
        help="Etherscan API key (or set via ETHERSCAN_API_KEY environment variable)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=f"Time interval between requests (minimum {MIN_INTERVAL} seconds).",
    )
    args = parser.parse_args()

    main(api_key=args.api_key, interval=args.interval)
