import logging
import sys
from typing import Any, Dict, Optional

import queue
from logging.handlers import QueueHandler, QueueListener

# Setup Logging
def setup_logging():
    """Configures the root logger to output JSON-like or structured English logs via a non-blocking queue."""
    log_queue = queue.Queue(-1)
    queue_handler = QueueHandler(log_queue)
    
    console_handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    console_handler.setFormatter(formatter)
    
    listener = QueueListener(log_queue, console_handler)
    listener.start()
    
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)
    root_logger.addHandler(queue_handler)
    
    # Return logger and listener (to stop it later if needed)
    return logging.getLogger("proxy"), listener

logger, log_listener = setup_logging()
