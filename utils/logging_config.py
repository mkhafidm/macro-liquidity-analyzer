import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logging(log_file="logs/app.log", level=logging.INFO):
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    
    logging.basicConfig(
        level=level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=5),
            logging.StreamHandler()
        ]
    )