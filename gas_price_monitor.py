import requests
import time
import signal
import sys
import logging
import argparse
import os
from typing import Optional, Dict

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

def get_gas_prices(api_key: str) -> Optional[Dict[str, str]]:
    url = f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={api_key}'
    try:
        response = requests.get(url)
        response.raise_for_status()  # Raise an HTTPError for bad responses (4xx and 5xx)
        data = response.json()

        if data['status'] == '1':
            gas_prices = data['result']
            return {
                'SafeGasPrice': gas_prices['SafeGasPrice'],
                'ProposeGasPrice': gas_prices['ProposeGasPrice'],
                'FastGasPrice': gas_prices['FastGasPrice']
            }
        else:
            logging.error(f"Error in response from Etherscan API: {data['message']}")
            return None

    except requests.exceptions.RequestException as e:
        logging.error(f"Request exception occurred: {e}")
        return None
    except ValueError as e:
        logging.error(f"Value error occurred: {e}")
        return None

def signal_handler(sig, frame):
    logging.info("Gracefully stopping the script...")
    sys.exit(0)

def main(api_key: str, interval: int):
    logging.info("Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)...")

    # Register the signal handler
    signal.signal(signal.SIGINT, signal_handler)

    try:
        while True:
            gas_prices = get_gas_prices(api_key)

            if gas_prices:
                logging.info(f"Safe Gas Price: {gas_prices['SafeGasPrice']} gwei")
                logging.info(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} gwei")
                logging.info(f"Fast Gas Price: {gas_prices['FastGasPrice']} gwei")

            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Gracefully stopping the script...")
        sys.exit(0)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ethereum Gas Price Monitor")
    parser.add_argument("--api_key", type=str, default=os.getenv("ETHERSCAN_API_KEY"), help="Etherscan API key")
    parser.add_argument("--interval", type=int, default=1, help="Interval in seconds between requests")

    args = parser.parse_args()

    if not args.api_key:
        logging.error("API key is required. Set it via --api_key argument or ETHERSCAN_API_KEY environment variable.")
        sys.exit(1)

    main(api_key=args.api_key, interval=args.interval)
