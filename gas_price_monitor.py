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
    format='%(asctime)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

# Constants
API_URL_TEMPLATE = 'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}'
MIN_INTERVAL = 10  # Minimum interval between API requests (seconds)

def fetch_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    """
    Fetch Ethereum gas prices from the Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Gas prices as a dictionary or None on failure.
    """
    url = API_URL_TEMPLATE.format(api_key=api_key)
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get('status') == '1':
            return {
                'SafeGasPrice': data['result'].get('SafeGasPrice', 'N/A'),
                'ProposeGasPrice': data['result'].get('ProposeGasPrice', 'N/A'),
                'FastGasPrice': data['result'].get('FastGasPrice', 'N/A'),
            }
        logger.error(f"API error: {data.get('message', 'Unknown error')}")
    except requests.Timeout:
        logger.error("Request to Etherscan API timed out.")
    except requests.RequestException as e:
        logger.error(f"HTTP error occurred: {e}")
    except ValueError as e:
        logger.error(f"JSON parse error: {e}")
    return None

def signal_handler(sig, frame):
    """
    Handle script termination gracefully.

    Args:
        sig: Signal number.
        frame: Current stack frame.
    """
    logger.info("Shutting down gracefully...")
    sys.exit(0)

def validate_interval(interval: int) -> int:
    """
    Ensure the interval is above the minimum threshold.

    Args:
        interval (int): Desired interval in seconds.

    Returns:
        int: Validated interval.
    """
    if interval < MIN_INTERVAL:
        logger.warning(
            f"Interval too short. Using minimum of {MIN_INTERVAL} seconds."
        )
        return MIN_INTERVAL
    return interval

def log_gas_prices(gas_prices: Dict[str, str]):
    """
    Log gas prices at the INFO level.

    Args:
        gas_prices (Dict[str, str]): Dictionary containing gas prices.
    """
    logger.info(f"Safe Gas Price: {gas_prices['SafeGasPrice']} Gwei")
    logger.info(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} Gwei")
    logger.info(f"Fast Gas Price: {gas_prices['FastGasPrice']} Gwei")

def main(api_key: str, interval: int):
    """
    Main loop to fetch and log Ethereum gas prices periodically.

    Args:
        api_key (str): Etherscan API key.
        interval (int): Interval between API requests in seconds.
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
        help="Etherscan API key (or set via ETHERSCAN_API_KEY environment variable)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help=f"Time interval between requests (minimum {MIN_INTERVAL} seconds).",
    )
    args = parser.parse_args()

    if not args.api_key:
        logger.error("API key is required. Provide it via --api_key or the ETHERSCAN_API_KEY environment variable.")
        sys.exit(1)

    main(api_key=args.api_key, interval=args.interval)
