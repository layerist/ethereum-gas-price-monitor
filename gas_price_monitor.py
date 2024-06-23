import requests
import time
import signal
import sys

# Your Etherscan API key
API_KEY = 'YourEtherscanAPIKey'

# URL for the Etherscan API to get gas price
ETHERSCAN_API_URL = f'https://api.etherscan.io/api?module=gastracker&action=gasoracle&apikey={API_KEY}'

# Function to get the current gas prices
def get_gas_prices():
    try:
        response = requests.get(ETHERSCAN_API_URL)
        data = response.json()

        if data['status'] == '1':
            gas_prices = data['result']
            return {
                'SafeGasPrice': gas_prices['SafeGasPrice'],
                'ProposeGasPrice': gas_prices['ProposeGasPrice'],
                'FastGasPrice': gas_prices['FastGasPrice']
            }
        else:
            print("Error in response from Etherscan API")
            return None

    except Exception as e:
        print(f"Exception occurred: {e}")
        return None

# Function to handle the keyboard interrupt signal
def signal_handler(sig, frame):
    print("\nGracefully stopping the script...")
    sys.exit(0)

# Register the signal handler
signal.signal(signal.SIGINT, signal_handler)

# Main loop to get gas prices every second
if __name__ == "__main__":
    print("Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)...")
    
    try:
        while True:
            gas_prices = get_gas_prices()
            
            if gas_prices:
                print(f"Safe Gas Price: {gas_prices['SafeGasPrice']} gwei")
                print(f"Propose Gas Price: {gas_prices['ProposeGasPrice']} gwei")
                print(f"Fast Gas Price: {gas_prices['FastGasPrice']} gwei")
            
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nGracefully stopping the script...")
        sys.exit(0)
