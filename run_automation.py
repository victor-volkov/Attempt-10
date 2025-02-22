import os
import sys
import time
import signal
import logging
from datetime import datetime
from automation_worker import AutomationWorker
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Set up logging
log_level = logging.DEBUG if os.getenv('DEBUG_MODE', 'false').lower() == 'true' else logging.INFO
logging.basicConfig(
    level=log_level,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('automation.log'),
        logging.StreamHandler(sys.stdout)
    ]
)

def signal_handler(signum, frame):
    logging.info("Received signal to stop. Shutting down gracefully...")
    sys.exit(0)

def main():
    # Register signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logging.info("Starting automation worker...")
    
    try:
        worker = AutomationWorker()
        logging.info("Automation worker initialized successfully")
        worker.run()
    except Exception as e:
        logging.error(f"Error in automation worker: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 