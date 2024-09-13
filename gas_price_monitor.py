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

def get_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    """
    Fetch current Ethereum gas prices from Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Dictionary containing Safe, Propose, and Fast gas prices in Gwei, or None on failure.
    """
    url = f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}'
    
    try:
        response = requests.get(url, timeout=10)  # Set a timeout for the request
        response.raise_for_status()

        data = response.json()

        if data.get('status') == '1':
            gas_prices = data.get('result', {})
            return {
                'SafeGasPrice': gas_prices.get('SafeGasPrice', 'N/A'),
                'ProposeGasPrice': gas_prices.get('ProposeGasPrice', 'N/A'),
                'FastGasPrice': gas_prices.get('FastGasPrice', 'N/A')
            }
        else:
            logger.error(f"API error: {data.get('message', 'Unknown error')}")
            return None

    except requests.exceptions.Timeout:
        logger.error("Request timed out while trying to reach the API.")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"HTTP Request exception: {e}")
        return None
    except ValueError as e:
        logger.error(f"Error parsing JSON response: {e}")
        return None

def signal_handler(sig, frame):
    """Handle script termination by user via Ctrl+C."""
    logger.info("Script terminated by user.")
    sys.exit(0)

def main(api_key: str, interval: int):
    """
    Main loop to fetch and log gas prices at regular intervals.

    Args:
        api_key (str): Etherscan API key.
        interval (int): Time interval between API requests, in seconds.
    """
    logger.info("Ethereum Gas Price Monitor started. (Press Ctrl+C to stop)")

    # Register the signal handler for graceful termination
    signal.signal(signal.SIGINT, signal_handler)

    try:
        while True:
            gas_prices = get_gas_prices(api_key)

            if gas_prices:
                logger.info(f"Safe Gas Price: {gas_prices['SafeGasPrice']} gwei")
                logger.info(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} gwei")
                logger.info(f"Fast Gas Price: {gas_prices['FastGasPrice']} gwei")
            else:
                logger.warning("Failed to retrieve gas prices. Retrying...")

            time.sleep(interval)

    except KeyboardInterrupt:
        logger.info("Gracefully stopping the script...")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument("--api_key", type=str, default=os.getenv("ETHERSCAN_API_KEY"), help="Etherscan API key")
    parser.add_argument("--interval", type=int, default=60, help="Interval in seconds between requests (minimum 10 seconds)")

    args = parser.parse_args()

    if not args.api_key:
        logger.error("API key is required. Set it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
        sys.exit(1)

    if args.interval < 10:
        logger.error("Interval must be at least 10 seconds.")
        sys.exit(1)

    main(api_key=args.api_key, interval=args.interval)
