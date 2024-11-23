import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, Dict

# Configure logging with a detailed format
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

API_URL = 'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}'
MIN_INTERVAL = 10  # Minimum allowed interval between API requests in seconds

def fetch_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    """
    Fetch current Ethereum gas prices from the Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Dictionary containing Safe, Propose, and Fast gas prices in Gwei, or None on failure.
    """
    url = API_URL.format(api_key=api_key)
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
        else:
            logger.error(f"API error: {data.get('message', 'Unknown error')}")
            return None
    except requests.Timeout:
        logger.error("The request to Etherscan API timed out.")
    except requests.RequestException as e:
        logger.error(f"An HTTP error occurred: {e}")
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
    return None

def signal_handler(sig, frame):
    """Handle graceful shutdown on user interruption (Ctrl+C)."""
    logger.info("Shutting down the script gracefully...")
    sys.exit(0)

def validate_interval(interval: int) -> int:
    """
    Ensure the interval meets a minimum threshold.

    Args:
        interval (int): Desired time interval between API requests in seconds.

    Returns:
        int: Validated interval.
    """
    if interval < MIN_INTERVAL:
        logger.warning(f"Interval less than {MIN_INTERVAL} seconds is too short. Using minimum interval of {MIN_INTERVAL} seconds.")
        return MIN_INTERVAL
    return interval

def main(api_key: str, interval: int):
    """
    Main loop to fetch and log Ethereum gas prices at regular intervals.

    Args:
        api_key (str): Etherscan API key.
        interval (int): Time interval between API requests in seconds.
    """
    logger.info("Starting Ethereum Gas Price Monitor... (Press Ctrl+C to stop)")

    # Handle user interruption gracefully
    signal.signal(signal.SIGINT, signal_handler)

    interval = validate_interval(interval)

    while True:
        gas_prices = fetch_gas_prices(api_key)
        if gas_prices:
            logger.info(f"Safe Gas Price: {gas_prices['SafeGasPrice']} Gwei")
            logger.info(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} Gwei")
            logger.info(f"Fast Gas Price: {gas_prices['FastGasPrice']} Gwei")
        else:
            logger.warning("Failed to retrieve gas prices. Retrying in the next interval...")
        time.sleep(interval)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument("--api_key", type=str, default=os.getenv("ETHERSCAN_API_KEY"), 
                        help="Etherscan API key (can also be set via environment variable ETHERSCAN_API_KEY)")
    parser.add_argument("--interval", type=int, default=60, 
                        help="Time interval between API requests in seconds (minimum 10 seconds)")

    args = parser.parse_args()

    # Validate API key
    if not args.api_key:
        logger.error("API key is required. Set it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
        sys.exit(1)

    main(api_key=args.api_key, interval=args.interval)
