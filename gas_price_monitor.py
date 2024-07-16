import requests
import time
import signal
import sys
import logging

# Your Etherscan API key
API_KEY = 'YourEtherscanAPIKey'

# URL for the Etherscan API to get gas price
ETHERSCAN_API_URL = f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={API_KEY}'

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Function to get the current gas prices
def get_gas_prices():
    try:
        response = requests.get(ETHERSCAN_API_URL)
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

# Function to handle the keyboard interrupt signal
def signal_handler(sig, frame):
    logging.info("Gracefully stopping the script...")
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)

# Main loop to get gas prices every second
if __name__ == "__main__":
    logging.info("Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)...")
    
    interval = 1  # Interval in seconds between requests
    
    try:
        while True:
            gas_prices = get_gas_prices()
            
            if gas_prices:
                logging.info(f"Safe Gas Price: {gas_prices['SafeGasPrice']} gwei")
                logging.info(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} gwei")
                logging.info(f"Fast Gas Price: {gas_prices['FastGasPrice']} gwei")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        logging.info("Gracefully stopping the script...")
        sys.exit(0)
