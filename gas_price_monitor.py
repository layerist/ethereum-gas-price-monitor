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
    Fetch current Ethereum gas prices from the Etherscan API.

    Args:
        api_key (str): Etherscan API key.

    Returns:
        Optional[Dict[str, str]]: Dictionary containing Safe, Propose, and Fast gas prices in Gwei, or None on failure.
    """
    url = f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}'
    
    try:
        response = requests.get(url, timeout=10)
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

    except requests.Timeout:
        logger.error("The request to Etherscan API timed out.")
        return None
    except requests.RequestException as e:
        logger.error(f"An HTTP error occurred: {e}")
        return None
    except ValueError as e:
        logger.error(f"Failed to parse JSON response: {e}")
        return None

def signal_handler(sig, frame):
    """Handle graceful shutdown on user interruption (Ctrl+C)."""
    logger.info("Shutting down the script gracefully...")
    sys.exit(0)

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

    while True:
        gas_prices = get_gas_prices(api_key)

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

    # Enforce a minimum interval of 10 seconds
    if args.interval < 10:
        logger.warning("Interval less than 10 seconds is too short. Using the minimum interval of 10 seconds.")
        args.interval = 10

    main(api_key=args.api_key, interval=args.interval)
