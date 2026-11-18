#!/usr/bin/env python3
"""
Lightweight, Line-Buffered Flight Recorder Telemetry Logger for Genshin Impact CV Automation
Records human-readable diagnostic traces of vision detections, state decisions, and HID commands.
"""

import os
import sys
import time
import threading
from datetime import datetime
from typing import Optional

from app.config_manager import LOGS_DIR


class FlightLogger:
    """Thread-safe, line-buffered flight telemetry logger writing to logs/run_YYYYMMDD_HHMMSS.txt."""

    def __init__(self, log_dir: Optional[str] = None):
        self.log_dir = log_dir or LOGS_DIR
        os.makedirs(self.log_dir, exist_ok=True)

        self.start_time = time.time()
        timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.filename = f"run_{timestamp_str}.txt"
        self.log_filepath = os.path.join(self.log_dir, self.filename)

        self.lock = threading.Lock()
        self._closed = False

        # Open in line-buffered mode (buffering=1) with UTF-8 encoding
        try:
            self.file = open(self.log_filepath, "w", encoding="utf-8", buffering=1)
        except Exception as e:
            print(f"[LOG ERROR] Failed to create flight recorder log file: {e}", file=sys.stderr)
            self.file = None

    def log_event(self, tag: str, message: str):
        """Logs a timestamped telemetry entry with a category tag.
        
        Args:
            tag: Category tag (e.g. 'BOOT', 'FOCUS', 'STATE', 'NAV', 'VISION', 'UNSTUCK', 'HID', 'TELEM').
            message: Informational trace or snapshot string.
        """
        if self._closed or not self.file:
            return

        elapsed = time.time() - self.start_time
        # Clean tag format: ensuring brackets e.g. [BOOT]
        clean_tag = tag if tag.startswith("[") and tag.endswith("]") else f"[{tag.strip()}]"
        line = f"[+{elapsed:07.3f}s] {clean_tag} {message}\n"

        with self.lock:
            if not self._closed and self.file and not self.file.closed:
                try:
                    self.file.write(line)
                    self.file.flush()
                except Exception as e:
                    print(f"[LOG ERROR] Write error: {e}", file=sys.stderr)

    def close(self):
        """Flushes and cleanly closes the log file, printing final path."""
        with self.lock:
            if self._closed:
                return
            self._closed = True
            if self.file and not self.file.closed:
                try:
                    self.file.flush()
                    self.file.close()
                except Exception as e:
                    print(f"[LOG ERROR] Error closing log file: {e}", file=sys.stderr)
                print(f"[LOG] Saved flight recorder trace to {self.log_filepath}")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
