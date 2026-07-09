import sys
import os
from pathlib import Path
from loguru import logger

# Ensure the logs directory exists at the root of the project
PROJECT_ROOT = Path(__file__).parent.parent
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Remove the default logger (which only outputs to stderr)
logger.remove()

# 1. Add a console sink (stdout) with a readable format
logger.add(
    sys.stdout,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
)

# 2. Add a daily rotating file sink
logger.add(
    str(LOG_DIR / "app_{time:YYYY-MM-DD}.log"),
    rotation="00:00",  # Rotate daily at midnight
    retention="30 days",  # Keep logs for 30 days
    format="{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {name}:{function}:{line} - {message}",
    level="DEBUG",
    enqueue=True,  # Thread-safe writing
)

# Export the configured logger
__all__ = ["logger"]
