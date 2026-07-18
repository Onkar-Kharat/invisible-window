import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from config.settings import get_settings

def get_logger(name: str) -> logging.Logger:
    settings = get_settings()
    logger = logging.getLogger(name)
    
    if not logger.handlers:
        level = getattr(logging, settings.log_level.upper(), logging.INFO)
        logger.setLevel(level)
        
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        
        # Console Handler
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)
        
        # File Handler
        log_dir = "logs"
        os.makedirs(log_dir, exist_ok=True)
        file_handler = RotatingFileHandler(
            os.path.join(log_dir, "app.log"),
            maxBytes=10*1024*1024, # 10MB
            backupCount=5
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        
    return logger
