# Ethereum Gas Price Monitor

This script monitors and prints the current gas prices on the Ethereum network every second. It uses the Etherscan API to retrieve the gas prices and handles graceful termination using a keyboard interrupt.

## Features
- Queries the Etherscan API every second to get the latest gas prices.
- Prints the Safe, Propose, and Fast gas prices in gwei.
- Allows graceful termination with a keyboard interrupt (Ctrl+C).

## Requirements
- Python 3.x
- `requests` library (install via `pip install requests`)

## Usage
1. Clone the repository or download the script `gas_price_monitor.py`.
2. Obtain an API key from [Etherscan](https://etherscan.io/apis).
3. Replace `YourEtherscanAPIKey` in the script with your actual API key.
4. Run the script:
    ```sh
    python gas_price_monitor.py
    ```
5. The script will start printing the gas prices every second. To stop the script, press `Ctrl+C`.

## Example Output
```
Starting Ethereum Gas Price Monitor (Press Ctrl+C to stop)...
Safe Gas Price: 20 gwei
Propose Gas Price: 30 gwei
Fast Gas Price: 40 gwei
Safe Gas Price: 21 gwei
Propose Gas Price: 31 gwei
Fast Gas Price: 41 gwei
...
```

## License
This project is licensed under the MIT License.

## Contributing
Feel free to submit issues or pull requests. For major changes, please open an issue first to discuss what you would like to change.
