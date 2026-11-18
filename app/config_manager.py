#!/usr/bin/env python3
"""
Configuration Manager & Global Constants for Genshin Impact CV Assistant (v5)
"""

import os
import sys
import json
import math
import random
import ctypes
import threading
from typing import Dict, Any

# Platform-specific audio chime
try:
    import winsound
    HAS_WINSOUND = True
except ImportError:
    HAS_WINSOUND = False

# Windows GUI API for DPI awareness and client rect calculation
try:
    import win32gui
    import win32process
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


# ==============================================================================
# Global Directories & Paths
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

MINIMAP_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "minimap_border.png")
CHOICE_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "choice_icon.png")
QUEST_GOLD_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "quest_gold.png")
QUEST_BLUE_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "quest_blue.png")

# Native Target Resolutions
TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
HALF_WIDTH = 640
HALF_HEIGHT = 360

# Region of Interest (ROI) Definitions in 1280x720 Space: [y1, y2, x1, x2]
ROI_MINIMAP = (0, 180, 0, 250)         # Top-Left Minimap
ROI_NAV = (100, 540, 190, 1090)        # Central Quest Tracking Area
ROI_CHOICE = (320, 560, 700, 1090)     # Middle-Right Dialogue Option Area
ROI_COMBAT = (120, 560, 160, 1120)     # Enemy Health Bar Area (Excludes sky y < 120 & HUD y > 560)
ROI_INTERACT = (300, 490, 735, 960)    # Overworld [F] Prompt Area
ROI_PARTY = (170, 470, 1130, 1260)     # Right-Hand Party Portrait Stack

# Central Stamina Bar & Player Character Exclusion Zone in 1280x720:
# Zeros out the yellow stamina arc next to Lumine so it never biases the marker centroid!
ROI_EXCLUSION_STAMINA = (240, 540, 520, 760)

# ROI for Sealed Chest Ring in 1280x720 (Hollow ring check inside x: 360..920, y: 240..540)
ROI_SEALED_CHEST = (240, 540, 360, 920)
ROI_SEALED_CHEST_HALF = (120, 270, 180, 460)  # y1, y2, x1, x2 in 640x360
ROI_GROUND_HALF = (250, 340, 220, 420)        # Lower-center ground in 640x360

# Minimap Geometry (Full Res)
MINIMAP_CENTER = (114, 96)
MINIMAP_RADIUS = 68

# State Definitions (Strict Hierarchy: DIALOGUE > COMBAT_AUTO > CHEST_LOOT > NAVIGATE)
STATE_DIALOGUE = "DIALOGUE"
STATE_COMBAT_AUTO = "COMBAT_AUTO"
STATE_COMBAT_MANUAL = "COMBAT_MANUAL"
STATE_CHEST_LOOT = "CHEST_LOOT"
STATE_NAVIGATE = "NAVIGATE"

# ==============================================================================
# Hardcoded Calibrated Defaults (v5 Two-Zone PD Architecture)
# ==============================================================================
HARDCODED_DEFAULTS = {
    "Kp_fine": 0.26,
    "Kd": 0.09,
    "Kp_fast": 0.44,
    "steer_rate_hz": 28,
    "center_lock_deadzone": 24,
    "pivot_error_threshold": 115,
    "road_following_enabled": True,
    "blue_marker_enabled": True,
    "template_match_threshold": 0.64,
    "quest_v_pulse_cooldown": 4.5,
    "lost_marker_v_delay": 0.75,
    "lost_marker_sweep_delay": 1.65,
}

DEFAULT_CONFIG = HARDCODED_DEFAULTS.copy()


def load_config() -> Dict[str, Any]:
    """Load config.json from disk with fallback to defaults."""
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = {**DEFAULT_CONFIG, **data}
                return config
        except Exception as e:
            print(f"[CONFIG WARN] Could not read {CONFIG_PATH}, using defaults: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]) -> bool:
    """Save active configuration dictionary to config.json."""
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"[CONFIG] Saved active settings to {CONFIG_PATH}")
        return True
    except Exception as e:
        print(f"[CONFIG ERROR] Failed saving {CONFIG_PATH}: {e}")
        return False


def setup_dpi_awareness():
    """Ensure Windows desktop coordinates match actual pixels (no scaling distortion)."""
    if os.name != "nt":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception as e:
            print(f"[WARN] Failed to set Windows DPI awareness: {e}")


def play_alert_chime(frequency: int = 880, duration_ms: int = 220):
    """Play a non-blocking frequency tone on Windows to alert user for manual takeover."""
    if not HAS_WINSOUND:
        return

    def _beep():
        try:
            winsound.Beep(frequency, duration_ms)
        except Exception:
            pass

    threading.Thread(target=_beep, daemon=True).start()


def human_delay(
    mean: float = 0.24,
    sigma: float = 0.35,
    min_val: float = 0.14,
    max_val: float = 0.68,
    allow_micro_pause: bool = True,
) -> float:
    """Generate non-deterministic action delays following a log-normal distribution."""
    val = random.lognormvariate(math.log(max(mean, 0.05)), sigma)
    clamped = max(min_val, min(max_val, val))
    if allow_micro_pause and random.random() < 0.10:
        clamped += random.uniform(0.85, 1.55)
    return clamped


def dialogue_advance_delay() -> float:
    """Generate natural humanized dialogue advance delays (0.38s - 0.78s)."""
    val = random.lognormvariate(math.log(0.52), 0.22)
    delay = max(0.38, min(0.78, val))
    if random.random() < 0.12:
        delay += random.uniform(0.95, 1.45)
    return delay
