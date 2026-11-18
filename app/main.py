#!/usr/bin/env python3
"""
Genshin Impact Hardware-Assisted Computer Vision Automation (v3.2 Hybrid Quest)
=============================================================================
Architecture:
- Screen Capture: mss (direct desktop / client-rect capture normalized to 1280x720)
- Hardware Input: ESP32-S3 Microcontroller running TinyUSB HID Keyboard & 2-Axis Mouse
- Strict State Hierarchy:
    1. DIALOGUE      : Minimap hidden -> Strict top priority, halts all combat/movement/loot,
                       runs natural log-normal dialogue & choice progression.
    2. COMBAT_AUTO   : Red HP bar detected -> Instant W0 halt (no sprinting past enemies),
                       humanized paced combat (3-hit combo, 6.5-8.5s E, 12-15s Q, 7-10s swap),
                       dynamic party size (1-4 slots), and Amber Aim-Shot mode for elevated/stalled targets.
    3. CHEST_LOOT    : High-confidence sealed chest ring or overworld [F] loot trigger ->
                       halts movement (W0), opens chest, waits 0.65s lid animation, enters 2.2-2.8s
                       multi-tap [F] vacuum burst with left/right camera wiggle, and drains stacked loot drops.
    4. NAVIGATE      : Hybrid Template + Color Quest Detection for both Gold (Archon/Story) &
                       Blue (World/Commission) markers, Stamina/Player exclusion zone masking,
                       Hard Center Lock (abs(error_x) <= 28px disables steering & road bias),
                       and Lost-Marker recovery (pulses 'V' after >0.75s, sweeps after >1.65s).
- Performance & Safety:
    - Downscaled heavy vision passes (0.5x frame: 640x360 for sealed ring & optical flow unstuck)
    - Ground motion check throttled to every 3rd frame (frame_idx % 3 == 0, saving ~75% CPU)
    - Debug window rendering throttled to max 20 FPS while capture & control run at full speed
    - Active Window Focus Lock (win32gui.GetForegroundWindow() == hwnd)
    - Global F12 Instant Pause / Resume Toggle (pynput)
    - 15-Second Safety Watchdog in Microcontroller Firmware + 4.0s keepalive
    - Live HSV & Kp Tuner Window (persists to config.json with 'S')
    - Live Quest Diamond Template Capture (Hotkey 'C' or mouse click on Debug window)
    - Emergency STOP on exit (finally block)
"""

import os
import sys
import time
import json
import math
import random
import ctypes
import argparse
import threading
from typing import Optional, Tuple, Dict, Any, List

import cv2
import numpy as np
import mss
import serial
from pynput import keyboard

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
# Global Configuration & Paths
# ==============================================================================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")
os.makedirs(ASSETS_DIR, exist_ok=True)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

MINIMAP_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "minimap_border.png")
CHOICE_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "choice_icon.png")
QUEST_GOLD_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "quest_gold.png")
QUEST_BLUE_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "quest_blue.png")

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720
HALF_WIDTH = 640
HALF_HEIGHT = 360

# Region of Interest (ROI) Definitions in 1280x720 Space: [y1, y2, x1, x2]
ROI_MINIMAP = (0, 180, 0, 250)         # Top-Left Minimap
ROI_NAV = (100, 540, 190, 1090)        # Central Gold/Blue Quest Marker Tracking Area
ROI_CHOICE = (320, 560, 700, 1090)     # Middle-Right Dialogue Option Area
ROI_COMBAT = (36, 360, 100, 1180)      # Upper/Middle Enemy Health Bar Area
ROI_INTERACT = (300, 490, 740, 940)    # Overworld [F] Prompt Area
ROI_PARTY = (170, 470, 1130, 1260)     # Right-Hand Party Portrait Stack

# Central Stamina Bar & Player Character Exclusion Zone in 1280x720:
# Zeros out the yellow stamina arc next to Lumine so it never biases the marker centroid!
ROI_EXCLUSION_STAMINA = (240, 540, 520, 760)

# ROI for Sealed Chest Ring in 1280x720: tighter mid-screen area
ROI_SEALED_CHEST = (220, 560, 300, 980)

# Corresponding ROIs in 0.5x Space (640x360)
ROI_SEALED_CHEST_HALF = (110, 280, 150, 490)  # y1, y2, x1, x2 in 640x360
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
# Humanized Delay Helper (Log-Normal Reaction Distribution)
# ==============================================================================
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


# ==============================================================================
# DPI Awareness Setup
# ==============================================================================
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


# ==============================================================================
# Non-Blocking Audio Alert
# ==============================================================================
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


# ==============================================================================
# ESP32-S3 Serial HID Controller (2-Axis Mouse & Bow Aim Support)
# ==============================================================================
class ESP32HIDController:
    """Manages serial protocol communication with the ESP32-S3 TinyUSB HID firmware."""

    def __init__(self, port: str = "COM4", baud: int = 115200, dry_run: bool = False):
        self.port = port
        self.baud = baud
        self.dry_run = dry_run
        self.ser: Optional[serial.Serial] = None
        self.is_walking: bool = False
        self.walk_start_time: float = 0.0
        self.is_lmb_held: bool = False
        self.last_command_time: float = time.time()
        self.lock = threading.Lock()

        if not self.dry_run:
            self._connect()

    def _connect(self):
        try:
            print(f"[HID] Connecting to ESP32-S3 on port '{self.port}' at {self.baud} baud...")
            self.ser = serial.Serial(self.port, self.baud, timeout=1.2)
            time.sleep(1.8)  # Allow ESP32 CDC settlement
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()

            if self.ping():
                print(f"[HID] Handshake verified! ESP32-S3 ready on {self.port}.")
            else:
                print(f"[WARN] ESP32-S3 did not return 'PONG'. Continuing...")
        except Exception as e:
            print(f"[ERROR] Could not connect to serial port '{self.port}': {e}")
            print("[INFO] Falling back to DRY-RUN mode (visual state machine only, no HID output).")
            self.dry_run = True

    def send_command(self, cmd: str):
        """Send a newline-terminated command string to the microcontroller."""
        self.last_command_time = time.time()
        if self.dry_run:
            return
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    payload = f"{cmd}\n".encode("ascii")
                    self.ser.write(payload)
                    self.ser.flush()
                except Exception as e:
                    print(f"[HID ERROR] Failed writing command '{cmd}': {e}")

    def ping(self) -> bool:
        if self.dry_run:
            return True
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.write(b"PING\n")
                    self.ser.flush()
                    resp = self.ser.readline().decode("ascii", errors="ignore").strip()
                    return resp == "PONG"
                except Exception:
                    return False
        return False

    def select_slot(self, slot_num: int):
        """Select party member slot (1, 2, 3, or 4)."""
        if 1 <= slot_num <= 4:
            self.send_command(str(slot_num))

    def press_e(self):
        """Trigger Elemental Skill ('E')."""
        self.send_command("E")

    def press_q(self):
        """Trigger Elemental Burst ('Q')."""
        self.send_command("Q")

    def press_r(self):
        """Toggle Bow Aiming Mode ('R')."""
        self.send_command("R")

    def press_f(self):
        """Trigger dialogue / interact / loot key ('F')."""
        self.send_command("F")

    def press_v(self):
        """Trigger quest path marker key ('V')."""
        self.send_command("V")

    def press_space(self):
        """Trigger jump / unfreeze key ('SPACE')."""
        self.send_command("SPACE")

    def click_lmb(self):
        """Perform normal attack click (Left Mouse Button)."""
        self.send_command("LMB")

    def hold_lmb(self):
        """Begin charged attack / aim shot charge (hold Left Mouse Button)."""
        self.send_command("LMB_HOLD")
        self.is_lmb_held = True

    def release_lmb(self):
        """Release charged attack / fire shot (release Left Mouse Button)."""
        self.send_command("LMB_REL")
        self.is_lmb_held = False

    def click_rmb(self):
        """Perform sprint dash / dodge (Right Mouse Button)."""
        self.send_command("RMB")

    def start_walk(self):
        """Engage continuous forward walk ('W1')."""
        if not self.is_walking:
            self.send_command("W1")
            self.is_walking = True
            self.walk_start_time = time.time()

    def stop_walk(self):
        """Release forward walk ('W0')."""
        if self.is_walking:
            self.send_command("W0")
            self.is_walking = False
            self.walk_start_time = 0.0

    def stop_all(self):
        """Emergency stop: release all keyboard keys and mouse buttons ('STOP')."""
        self.send_command("STOP")
        self.is_walking = False
        self.is_lmb_held = False
        self.walk_start_time = 0.0

    def move_mouse(self, dx: int, dy: int = 0):
        """Move mouse horizontally and/or vertically (clamped to [-50, 50])."""
        clamped_dx = max(-50, min(50, int(dx)))
        clamped_dy = max(-50, min(50, int(dy)))
        if clamped_dx != 0 or clamped_dy != 0:
            if clamped_dy != 0:
                self.send_command(f"M{clamped_dx},{clamped_dy}")
            else:
                self.send_command(f"M{clamped_dx}")

    def close(self):
        """Safely release all keys and close serial connection."""
        self.stop_all()
        with self.lock:
            if self.ser and self.ser.is_open:
                try:
                    self.ser.close()
                except Exception:
                    pass
        print("[HID] Serial connection closed cleanly.")


# ==============================================================================
# Configuration Persistence
# ==============================================================================
DEFAULT_CONFIG = {
    "H_min": 18,
    "H_max": 38,
    "S_min": 140,
    "S_max": 255,
    "V_min": 180,
    "V_max": 255,
    "Kp_x100": 14,
    "steer_ema_alpha": 0.35,
    "road_following_enabled": True,
    "center_lock_deadzone": 28,
    "blue_marker_enabled": True,
    "quest_v_pulse_cooldown": 4.5,
    "lost_marker_v_delay": 0.75,
    "lost_marker_sweep_delay": 1.65,
}


def load_config() -> Dict[str, Any]:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                config = {**DEFAULT_CONFIG, **data}
                print(f"[CONFIG] Loaded settings from {CONFIG_PATH}")
                return config
        except Exception as e:
            print(f"[CONFIG WARN] Could not read {CONFIG_PATH}, using defaults: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config: Dict[str, Any]):
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
        print(f"[CONFIG] Saved active settings to {CONFIG_PATH}")
        return True
    except Exception as e:
        print(f"[CONFIG ERROR] Failed saving {CONFIG_PATH}: {e}")
        return False


# ==============================================================================
# OpenCV Tuner Window & Trackbars
# ==============================================================================
TUNER_WINDOW = "Genshin Tuner (HSV & Gain)"


def nothing(x):
    pass


def setup_tuner_window(initial_cfg: Dict[str, Any]):
    cv2.namedWindow(TUNER_WINDOW, cv2.WINDOW_AUTOSIZE)
    cv2.createTrackbar("H_min", TUNER_WINDOW, initial_cfg["H_min"], 179, nothing)
    cv2.createTrackbar("H_max", TUNER_WINDOW, initial_cfg["H_max"], 179, nothing)
    cv2.createTrackbar("S_min", TUNER_WINDOW, initial_cfg["S_min"], 255, nothing)
    cv2.createTrackbar("S_max", TUNER_WINDOW, initial_cfg["S_max"], 255, nothing)
    cv2.createTrackbar("V_min", TUNER_WINDOW, initial_cfg["V_min"], 255, nothing)
    cv2.createTrackbar("V_max", TUNER_WINDOW, initial_cfg["V_max"], 255, nothing)
    cv2.createTrackbar("Kp_x100", TUNER_WINDOW, min(initial_cfg["Kp_x100"], 40), 40, nothing)


def read_tuner_values() -> Dict[str, Any]:
    return {
        "H_min": cv2.getTrackbarPos("H_min", TUNER_WINDOW),
        "H_max": cv2.getTrackbarPos("H_max", TUNER_WINDOW),
        "S_min": cv2.getTrackbarPos("S_min", TUNER_WINDOW),
        "S_max": cv2.getTrackbarPos("S_max", TUNER_WINDOW),
        "V_min": cv2.getTrackbarPos("V_min", TUNER_WINDOW),
        "V_max": cv2.getTrackbarPos("V_max", TUNER_WINDOW),
        "Kp_x100": cv2.getTrackbarPos("Kp_x100", TUNER_WINDOW),
    }


# ==============================================================================
# Window Helper Functions
# ==============================================================================
def find_genshin_window() -> Tuple[int, Optional[Dict[str, int]]]:
    """Locate Genshin Impact window handle and calculate exact client rectangle."""
    if not HAS_WIN32:
        return 0, None

    hwnd = win32gui.FindWindow(None, "Genshin Impact")
    if hwnd == 0:
        return 0, None

    try:
        rect = win32gui.GetClientRect(hwnd)
        lt = win32gui.ClientToScreen(hwnd, (rect[0], rect[1]))
        rb = win32gui.ClientToScreen(hwnd, (rect[2], rect[3]))
        width = rb[0] - lt[0]
        height = rb[1] - lt[1]
        if width <= 0 or height <= 0:
            return hwnd, None
        return hwnd, {"left": lt[0], "top": lt[1], "width": width, "height": height}
    except Exception:
        return hwnd, None


def is_genshin_focused(hwnd: int) -> bool:
    """Return True if Genshin Impact currently owns keyboard and mouse focus."""
    if not HAS_WIN32 or hwnd == 0:
        return False
    try:
        return win32gui.GetForegroundWindow() == hwnd
    except Exception:
        return False


# ==============================================================================
# Vision Processing & Feature Detection
# ==============================================================================
class VisionDetector:
    def __init__(self):
        self.minimap_template = None
        self.choice_template = None
        self.quest_gold_template = None
        self.quest_blue_template = None
        self.reload_templates()

        # Precompute circular minimap mask for road & quest extraction
        self.minimap_circle_mask = np.zeros((TARGET_HEIGHT, TARGET_WIDTH), dtype=np.uint8)
        cv2.circle(self.minimap_circle_mask, MINIMAP_CENTER, MINIMAP_RADIUS, 255, -1)

    def reload_templates(self):
        """Load or reload all PNG templates from disk."""
        if os.path.exists(MINIMAP_TEMPLATE_PATH):
            tmpl = cv2.imread(MINIMAP_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.minimap_template = tmpl

        if os.path.exists(CHOICE_TEMPLATE_PATH):
            tmpl = cv2.imread(CHOICE_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.choice_template = tmpl

        if os.path.exists(QUEST_GOLD_TEMPLATE_PATH):
            tmpl = cv2.imread(QUEST_GOLD_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.quest_gold_template = tmpl
                print(f"[VISION] Loaded Gold Quest template from {QUEST_GOLD_TEMPLATE_PATH}")

        if os.path.exists(QUEST_BLUE_TEMPLATE_PATH):
            tmpl = cv2.imread(QUEST_BLUE_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.quest_blue_template = tmpl
                print(f"[VISION] Loaded Blue Quest template from {QUEST_BLUE_TEMPLATE_PATH}")

    def check_minimap_present(self, frame_bgr: np.ndarray) -> Tuple[bool, float]:
        """
        State 1 trigger: Detect presence of minimap UI.
        If minimap disappears, the game is strictly in cutscene / dialogue mode.
        """
        y1, y2, x1, x2 = ROI_MINIMAP
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if self.minimap_template is not None:
            res = cv2.matchTemplate(gray, self.minimap_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            return (max_val >= 0.58), float(max_val)

        edges = cv2.Canny(gray, 60, 160)
        total_pixels = (y2 - y1) * (x2 - x1)
        edge_density = float(np.count_nonzero(edges)) / float(total_pixels)
        is_present = edge_density > 0.032
        return is_present, edge_density

    def check_choice_icon(self, frame_bgr: np.ndarray) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
        """Detect dialogue speech-bubble / choice icon in middle-right ROI."""
        y1, y2, x1, x2 = ROI_CHOICE
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if self.choice_template is not None:
            res = cv2.matchTemplate(gray, self.choice_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= 0.68:
                tw, th = self.choice_template.shape[1], self.choice_template.shape[0]
                box = (x1 + max_loc[0], y1 + max_loc[1], tw, th)
                return True, box

        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        lower_white = np.array([0, 0, 225], dtype=np.uint8)
        upper_white = np.array([180, 48, 255], dtype=np.uint8)
        white_mask = cv2.inRange(hsv, lower_white, upper_white)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        clean_mask = cv2.morphologyEx(white_mask, cv2.MORPH_OPEN, kernel)

        contours, _ = cv2.findContours(clean_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area = cv2.contourArea(c)
            if 55 <= area <= 650:
                bx, by, bw, bh = cv2.boundingRect(c)
                aspect_ratio = float(bw) / float(bh) if bh > 0 else 0
                if 0.55 <= aspect_ratio <= 1.70 and 10 <= bw <= 48 and 10 <= bh <= 48:
                    return True, (x1 + bx, y1 + by, bw, bh)

        return False, None

    def detect_overworld_f_prompt(
        self, frame_bgr: np.ndarray
    ) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
        """
        Detect overworld [F] interaction keycap in y: 300..490, x: 740..940.
        Near-square icon (w, h: 18..42px) with interior high contrast letter 'F'.
        """
        y1, y2, x1, x2 = ROI_INTERACT
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        thresh = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
        )
        _, bright_mask = cv2.threshold(gray, 195, 255, cv2.THRESH_BINARY)
        combined = cv2.bitwise_and(thresh, bright_mask)

        contours, _ = cv2.findContours(combined, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            bx, by, bw, bh = cv2.boundingRect(c)
            if 18 <= bw <= 42 and 18 <= bh <= 42:
                aspect_ratio = float(bw) / float(bh)
                if 0.75 <= aspect_ratio <= 1.35:
                    patch = gray[by : by + bh, bx : bx + bw]
                    if patch.size > 0:
                        edges = cv2.Canny(patch, 60, 160)
                        edge_count = cv2.countNonZero(edges)
                        std_dev = float(np.std(patch))
                        if edge_count >= 10 and std_dev >= 20.0:
                            return True, (x1 + bx, y1 + by, bw, bh)

        return False, None

    def detect_party_size(self, frame_bgr: np.ndarray) -> int:
        """
        Detect active unlocked party size (1 to 4) by inspecting the right-hand
        portrait stack (ROI_PARTY: x: 1130..1260, y: 170..470).
        """
        slot_y_ranges = [
            (180, 245),  # Slot 1 (Lumine / Starter)
            (255, 320),  # Slot 2 (Amber)
            (330, 395),  # Slot 3 (Kaeya)
            (405, 470),  # Slot 4 (Lisa)
        ]

        active_count = 1  # Slot 1 is always unlocked

        for slot_idx in range(1, 4):  # Check slots 2, 3, 4
            sy1, sy2 = slot_y_ranges[slot_idx]
            slot_roi = frame_bgr[sy1:sy2, 1145:1245]
            if slot_roi.size == 0:
                break

            hp_band = slot_roi[44:60, 20:85]
            if hp_band.size == 0:
                break

            hsv_hp = cv2.cvtColor(hp_band, cv2.COLOR_BGR2HSV)
            green_mask = cv2.inRange(hsv_hp, np.array([35, 100, 100]), np.array([85, 255, 255]))
            red_mask1 = cv2.inRange(hsv_hp, np.array([0, 100, 100]), np.array([12, 255, 255]))
            red_mask2 = cv2.inRange(hsv_hp, np.array([170, 100, 100]), np.array([179, 255, 255]))
            hp_mask = cv2.bitwise_or(green_mask, cv2.bitwise_or(red_mask1, red_mask2))

            gray_slot = cv2.cvtColor(slot_roi[6:45, 15:85], cv2.COLOR_BGR2GRAY)
            edges = cv2.Canny(gray_slot, 60, 150)
            edge_density = float(np.count_nonzero(edges)) / float(gray_slot.size)

            if cv2.countNonZero(hp_mask) >= 18 or edge_density > 0.08:
                active_count = slot_idx + 1
            else:
                break

        return max(1, min(4, active_count))

    def detect_combat_red_hp_bars(
        self, frame_bgr: np.ndarray
    ) -> Tuple[bool, int, float, Optional[Tuple[int, int]], bool]:
        """
        Combat trigger: Detect enemy red HP bars in upper/middle screen (y: 36..360).
        Returns: (in_combat, pixel_count, largest_contour_area, target_centroid, is_elevated)
        """
        y1, y2, x1, x2 = ROI_COMBAT
        roi = frame_bgr[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        mask1 = cv2.inRange(hsv, np.array([0, 150, 140]), np.array([10, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([170, 150, 140]), np.array([179, 255, 255]))
        red_mask = cv2.bitwise_or(mask1, mask2)

        bar_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 2))
        filtered = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, bar_kernel)
        pixel_count = int(cv2.countNonZero(filtered))

        largest_area = 0.0
        target_centroid = None
        is_elevated = False

        if pixel_count >= 130:
            contours, _ = cv2.findContours(filtered, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for c in contours:
                area = cv2.contourArea(c)
                if area > largest_area:
                    largest_area = area
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = int(M["m10"] / M["m00"]) + x1
                        cy = int(M["m01"] / M["m00"]) + y1
                        target_centroid = (cx, cy)
                        if cy < 290:
                            is_elevated = True

        in_combat = pixel_count >= 130
        return in_combat, pixel_count, largest_area, target_centroid, is_elevated

    def detect_sealed_chest_ring(
        self, frame_half: np.ndarray
    ) -> Tuple[bool, Optional[Tuple[int, int, int, int]], Optional[Tuple[int, int]]]:
        """
        Detect glowing red/crimson or anemo-teal sealed-chest lock rings in tighter mid-screen ROI.
        Evaluated on 0.5x downscaled frame (ROI_SEALED_CHEST_HALF: y: 110..280, x: 150..490).
        """
        y1, y2, x1, x2 = ROI_SEALED_CHEST_HALF
        roi_half = frame_half[y1:y2, x1:x2]
        if roi_half.size == 0:
            return False, None, None

        hsv_half = cv2.cvtColor(roi_half, cv2.COLOR_BGR2HSV)

        red_ring1 = cv2.inRange(hsv_half, np.array([0, 175, 170]), np.array([8, 255, 255]))
        red_ring2 = cv2.inRange(hsv_half, np.array([172, 175, 170]), np.array([180, 255, 255]))
        red_ring = cv2.bitwise_or(red_ring1, red_ring2)

        teal_ring = cv2.inRange(hsv_half, np.array([82, 160, 170]), np.array([98, 255, 255]))
        ring_mask = cv2.bitwise_or(red_ring, teal_ring)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        cleaned = cv2.morphologyEx(ring_mask, cv2.MORPH_OPEN, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in contours:
            area_half = cv2.contourArea(c)
            if 87 <= area_half <= 800:
                bx, by, bw, bh = cv2.boundingRect(c)
                if bh > 0:
                    aspect_ratio = float(bw) / float(bh)
                    if 0.70 <= aspect_ratio <= 1.35:
                        cx_half = x1 + bx + bw // 2
                        cy_half = y1 + by + bh // 2
                        cx_full = cx_half * 2
                        cy_full = cy_half * 2
                        bbox_full = ((x1 + bx) * 2, (y1 + by) * 2, bw * 2, bh * 2)
                        return True, bbox_full, (cx_full, cy_full)

        return False, None, None

    def analyze_minimap_road_and_quest(
        self, frame_bgr: np.ndarray
    ) -> Tuple[Optional[float], Optional[float], bool]:
        """
        Extract quest bearing and segment Mondstadt dirt/stone paths inside circular minimap.
        Returns: (quest_bearing, road_bearing, has_road)
        """
        cx, cy = MINIMAP_CENTER
        r = MINIMAP_RADIUS

        mini_roi = frame_bgr[cy - r : cy + r, cx - r : cx + r]
        if mini_roi.size == 0:
            return None, None, False

        hsv_mini = cv2.cvtColor(mini_roi, cv2.COLOR_BGR2HSV)

        # Quest Marker detection inside minimap (gold diamond / arrow)
        gold_mask = cv2.inRange(hsv_mini, np.array([18, 130, 175]), np.array([36, 255, 255]))
        q_contours, _ = cv2.findContours(gold_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        quest_bearing = None
        for qc in q_contours:
            if 8 <= cv2.contourArea(qc) <= 450:
                M = cv2.moments(qc)
                if M["m00"] > 0:
                    q_local_x = M["m10"] / M["m00"]
                    q_local_y = M["m01"] / M["m00"]
                    dx = q_local_x - r
                    dy = q_local_y - r
                    quest_bearing = math.atan2(dx, -dy)
                    break

        # Road Segmentation inside minimap
        road_mask = cv2.inRange(hsv_mini, np.array([12, 15, 155]), np.array([32, 95, 245]))

        local_circle = np.zeros((2 * r, 2 * r), dtype=np.uint8)
        cv2.circle(local_circle, (r, r), r - 4, 255, -1)
        road_in_minimap = cv2.bitwise_and(road_mask, local_circle)

        ref_angle = quest_bearing if quest_bearing is not None else 0.0
        cone_limit = math.radians(55)

        y_indices, x_indices = np.where(road_in_minimap > 0)
        valid_road_x = []
        valid_road_y = []

        for px, py in zip(x_indices, y_indices):
            p_dx = px - r
            p_dy = py - r
            dist = math.hypot(p_dx, p_dy)
            if dist > 14:
                p_angle = math.atan2(p_dx, -p_dy)
                diff_angle = abs(math.atan2(math.sin(p_angle - ref_angle), math.cos(p_angle - ref_angle)))
                if diff_angle <= cone_limit:
                    valid_road_x.append(px)
                    valid_road_y.append(py)

        road_bearing = None
        has_road = False

        if len(valid_road_x) >= 35:
            mean_rx = np.mean(valid_road_x)
            mean_ry = np.mean(valid_road_y)
            road_bearing = math.atan2(mean_rx - r, -(mean_ry - r))
            has_road = True

        return quest_bearing, road_bearing, has_road

    def track_quest_marker(
        self, frame_bgr: np.ndarray, tuner_cfg: Dict[str, Any]
    ) -> Tuple[Optional[Tuple[int, int]], Optional[str], np.ndarray, Optional[Tuple[int, int, int, int]]]:
        """
        Hybrid Template + Color Quest Marker Detector supporting both:
        1. Gold Markers (Archon / Story quests): H: 18..38, S: 140..255, V: 180..255
        2. Blue Markers (World / Commission quests): H: 88..108, S: 155..255, V: 175..255
        
        Guards & Safety:
        - Masks out central character/stamina exclusion zone (x: 520..760, y: 240..540).
        - Rejects blue sky/water false positives by verifying surrounding 48x48 ring (<45% blue pixels).
        - Multi-scale template matching (scales: 0.85, 1.0, 1.15; threshold >= 0.62) with color confirmation.
        
        Returns: (centroid (cx, cy), marker_type ("GOLD" | "BLUE"), display_mask, bbox (x, y, w, h))
        """
        y1, y2, x1, x2 = ROI_NAV
        roi = frame_bgr[y1:y2, x1:x2]
        gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # 1. Gold Mask (Archon / Story quests)
        lower_gold = np.array([tuner_cfg["H_min"], tuner_cfg["S_min"], tuner_cfg["V_min"]], dtype=np.uint8)
        upper_gold = np.array([tuner_cfg["H_max"], tuner_cfg["S_max"], tuner_cfg["V_max"]], dtype=np.uint8)
        gold_mask = cv2.inRange(hsv_roi, lower_gold, upper_gold)

        # 2. Blue Mask (World / Daily Commission quests)
        lower_blue = np.array([88, 155, 175], dtype=np.uint8)
        upper_blue = np.array([108, 255, 255], dtype=np.uint8)
        blue_mask = cv2.inRange(hsv_roi, lower_blue, upper_blue)

        # 3. ZERO OUT STAMINA BAR & PLAYER CHARACTER EXCLUSION ZONE
        # In 1280x720: x: 520..760, y: 240..540.
        # Relative to ROI_NAV (y: 100..540, x: 190..1090):
        # y: 140..440, x: 330..570
        ey1 = max(0, 240 - y1)
        ey2 = min(roi.shape[0], 540 - y1)
        ex1 = max(0, 520 - x1)
        ex2 = min(roi.shape[1], 760 - x1)
        gold_mask[ey1:ey2, ex1:ex2] = 0
        blue_mask[ey1:ey2, ex1:ex2] = 0

        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        gold_mask = cv2.morphologyEx(gold_mask, cv2.MORPH_OPEN, kernel)
        blue_mask = cv2.morphologyEx(blue_mask, cv2.MORPH_OPEN, kernel)

        # Preview display mask
        display_mask = cv2.bitwise_or(gold_mask, blue_mask)

        candidates: List[Dict[str, Any]] = []

        # 4. Multi-Scale Template Matching for Gold
        if self.quest_gold_template is not None:
            for scale in (0.85, 1.0, 1.15):
                tw = int(self.quest_gold_template.shape[1] * scale)
                th = int(self.quest_gold_template.shape[0] * scale)
                if tw < 8 or th < 8 or tw >= roi.shape[1] or th >= roi.shape[0]:
                    continue
                scaled_tmpl = cv2.resize(self.quest_gold_template, (tw, th))
                res = cv2.matchTemplate(gray_roi, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
                # Zero out exclusion zone in template result
                res[ey1:ey2, ex1:ex2] = -1.0
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= 0.62:
                    tx, ty = max_loc
                    tcx = x1 + tx + tw // 2
                    tcy = y1 + ty + th // 2
                    # Verify interior gold hue
                    patch_hsv = hsv_roi[ty : ty + th, tx : tx + tw]
                    if patch_hsv.size > 0:
                        gold_pts = cv2.inRange(patch_hsv, lower_gold, upper_gold)
                        if cv2.countNonZero(gold_pts) >= 12:
                            candidates.append({
                                "type": "GOLD",
                                "score": float(max_val) + 0.35,  # Template bonus
                                "centroid": (tcx, tcy),
                                "bbox": (x1 + tx, y1 + ty, tw, th),
                            })
                            break

        # 5. Multi-Scale Template Matching for Blue
        if self.quest_blue_template is not None:
            for scale in (0.85, 1.0, 1.15):
                tw = int(self.quest_blue_template.shape[1] * scale)
                th = int(self.quest_blue_template.shape[0] * scale)
                if tw < 8 or th < 8 or tw >= roi.shape[1] or th >= roi.shape[0]:
                    continue
                scaled_tmpl = cv2.resize(self.quest_blue_template, (tw, th))
                res = cv2.matchTemplate(gray_roi, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
                res[ey1:ey2, ex1:ex2] = -1.0
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= 0.62:
                    tx, ty = max_loc
                    tcx = x1 + tx + tw // 2
                    tcy = y1 + ty + th // 2
                    # Sky/Water Rejection Check
                    if not self._is_sky_or_water_background(frame_bgr, tcx, tcy):
                        patch_hsv = hsv_roi[ty : ty + th, tx : tx + tw]
                        if patch_hsv.size > 0:
                            blue_pts = cv2.inRange(patch_hsv, lower_blue, upper_blue)
                            if cv2.countNonZero(blue_pts) >= 10:
                                candidates.append({
                                    "type": "BLUE",
                                    "score": float(max_val) + 0.30,
                                    "centroid": (tcx, tcy),
                                    "bbox": (x1 + tx, y1 + ty, tw, th),
                                })
                                break

        # 6. Complementary / Fallback HSV Contour Matching (Gold)
        g_contours, _ = cv2.findContours(gold_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in g_contours:
            area = cv2.contourArea(c)
            if 22 <= area <= 950:
                bx, by, bw, bh = cv2.boundingRect(c)
                if 10 <= bw <= 36 and 10 <= bh <= 36:
                    aspect_ratio = float(bw) / float(bh)
                    if 0.70 <= aspect_ratio <= 1.40:
                        M = cv2.moments(c)
                        if M["m00"] > 0:
                            cx = int(M["m10"] / M["m00"]) + x1
                            cy = int(M["m01"] / M["m00"]) + y1
                            candidates.append({
                                "type": "GOLD",
                                "score": area / 100.0,
                                "centroid": (cx, cy),
                                "bbox": (x1 + bx, y1 + by, bw, bh),
                            })

        # 7. Complementary / Fallback HSV Contour Matching (Blue)
        b_contours, _ = cv2.findContours(blue_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in b_contours:
            area = cv2.contourArea(c)
            if 22 <= area <= 950:
                bx, by, bw, bh = cv2.boundingRect(c)
                if 10 <= bw <= 36 and 10 <= bh <= 36:
                    aspect_ratio = float(bw) / float(bh)
                    if 0.70 <= aspect_ratio <= 1.40:
                        M = cv2.moments(c)
                        if M["m00"] > 0:
                            cx = int(M["m10"] / M["m00"]) + x1
                            cy = int(M["m01"] / M["m00"]) + y1
                            # Check sky/water rejection
                            if not self._is_sky_or_water_background(frame_bgr, cx, cy):
                                candidates.append({
                                    "type": "BLUE",
                                    "score": (area / 100.0) * 0.90,  # Slight Gold preference
                                    "centroid": (cx, cy),
                                    "bbox": (x1 + bx, y1 + by, bw, bh),
                                })

        if not candidates:
            return None, None, display_mask, None

        # Sort candidates: Gold prioritized, then highest score
        candidates.sort(key=lambda item: (1 if item["type"] == "GOLD" else 0, item["score"]), reverse=True)
        best = candidates[0]
        return best["centroid"], best["type"], display_mask, best["bbox"]

    def _is_sky_or_water_background(self, frame_bgr: np.ndarray, cx: int, cy: int) -> bool:
        """
        Sky & Water Rejection: Inspect surrounding 48x48 box centered at (cx, cy).
        If surrounding pixels are > 45% blue (sky / water / river reflection), reject!
        """
        sy1 = max(0, cy - 24)
        sy2 = min(TARGET_HEIGHT, cy + 24)
        sx1 = max(0, cx - 24)
        sx2 = min(TARGET_WIDTH, cx + 24)
        surround = frame_bgr[sy1:sy2, sx1:sx2]
        if surround.size == 0:
            return True

        hsv_surround = cv2.cvtColor(surround, cv2.COLOR_BGR2HSV)
        # Sky / lake blue hue
        sky_blue_mask = cv2.inRange(hsv_surround, np.array([85, 70, 120]), np.array([115, 255, 255]))
        blue_pixel_count = cv2.countNonZero(sky_blue_mask)
        total_pixels = surround.shape[0] * surround.shape[1]
        blue_ratio = float(blue_pixel_count) / float(total_pixels)
        return blue_ratio > 0.45

    def calculate_ground_motion_downscaled(
        self, frame_half: np.ndarray, prev_ground_gray_half: Optional[np.ndarray]
    ) -> Tuple[float, np.ndarray]:
        """
        Frame-differencing motion score on the lower-center ground region on 0.5x frame.
        ROI_GROUND_HALF: y: 250..340, x: 220..420 in 640x360 space.
        """
        y1, y2, x1, x2 = ROI_GROUND_HALF
        roi = frame_half[y1:y2, x1:x2]
        curr_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if prev_ground_gray_half is None:
            return 10.0, curr_gray

        diff = cv2.absdiff(curr_gray, prev_ground_gray_half)
        motion_score = float(np.mean(diff))
        return motion_score, curr_gray


# ==============================================================================
# Main Assistant Orchestrator
# ==============================================================================
class GenshinAssistant:
    def __init__(self, port: str = "COM4", dry_run: bool = False, capture_icon_mode: Optional[str] = None):
        self.port = port
        self.dry_run = dry_run
        self.capture_icon_mode = capture_icon_mode

        self.hid = ESP32HIDController(port=self.port, dry_run=self.dry_run)
        self.detector = VisionDetector()
        self.config = load_config()

        # Strict State Hierarchy & Variables
        self.current_state = STATE_NAVIGATE
        self.previous_minimap_state = True
        self.detected_party_size: int = 1
        self.marker_last_seen_time = time.time()
        self.active_marker_type: Optional[str] = None
        self.active_marker_centroid: Optional[Tuple[int, int]] = None
        self.active_marker_bbox: Optional[Tuple[int, int, int, int]] = None

        # Camera Smoothing (EMA) & Rate Limiter
        self.ema_error_x: float = 0.0
        self.last_mouse_packet_time: float = 0.0

        # Humanized Navigation Steering & Movement
        self.last_steer_time = 0.0
        self.next_steer_interval = random.uniform(0.055, 0.075)
        self.last_sprint_time = time.time()
        self.next_sprint_interval = random.uniform(2.1, 3.8)
        self.pending_v_pulse_time: Optional[float] = None
        self.scan_sweep_direction: int = 1
        self.last_scan_sweep_time: float = 0.0
        self.next_scan_sweep_interval: float = 0.22
        self.is_scanning_for_marker: bool = False

        # Lost-Marker Pulse & Sweep Timers
        self.last_quest_v_pulse_time: float = 0.0
        self.marker_lost_reported: bool = False

        # Road-Following & Unstuck Variables
        self.active_road_following: bool = False
        self.unstuck_still_start_time: float = 0.0
        self.last_lateral_unstuck_time: float = 0.0

        # Downscaled Ground Motion Differencing
        self.prev_ground_gray_half: Optional[np.ndarray] = None
        self.smooth_motion_score = 10.0
        self.is_stuck_alert = False
        self.frame_idx: int = 0

        # Multi-Second Loot Vacuum & Overworld [F] Prompt
        self.f_prompt_consecutive_frames: int = 0
        self.f_prompt_box: Optional[Tuple[int, int, int, int]] = None
        self.last_overworld_f_time: float = 0.0
        self.overworld_f_cooldown: float = 1.4
        self.show_overworld_f_box_until: float = 0.0
        self.active_chest_box: Optional[Tuple[int, int, int, int]] = None

        # Loot Vacuum State Machine
        self.vacuum_active: bool = False
        self.vacuum_phase: str = "IDLE"  # "WAIT_ANIMATION", "BURST", "DRAIN_PROMPT", "IDLE"
        self.vacuum_animation_end_time: float = 0.0
        self.vacuum_burst_start_time: float = 0.0
        self.vacuum_burst_end_time: float = 0.0
        self.next_vacuum_f_time: float = 0.0
        self.vacuum_taps_count: int = 0
        self.vacuum_wiggle_stage: int = 0
        self.vacuum_drain_timeout: float = 0.0

        # Dialogue Skipping
        self.next_dialogue_f_time: float = 0.0

        # Deliberate, Paced Auto-Combat System
        self.combat_start_time = 0.0
        self.combat_clear_start = 0.0
        self.combat_step_ready_time = 0.0
        self.active_combat_action_label = "Idle"
        self.combat_target_centroid: Optional[Tuple[int, int]] = None
        self.combat_largest_area: float = 0.0
        self.current_party_slot: int = 1
        self.last_party_swap_time: float = time.time()
        self.next_party_swap_interval: float = random.uniform(7.0, 10.0)
        self.last_skill_e_time: float = 0.0
        self.next_skill_e_interval: float = random.uniform(6.5, 8.5)
        self.last_burst_q_time: float = 0.0
        self.next_burst_q_interval: float = random.uniform(12.0, 15.0)
        self.combat_combo_hit_count: int = 0
        self.melee_damage_stall_timer: float = time.time()
        self.last_enemy_hp_pixels: int = 0
        self.is_aiming_bow: bool = False
        self.bow_aim_shot_count: int = 0

        # Performance Throttling (20 FPS Debug Rendering)
        self.last_render_time: float = 0.0

        # Latest frame reference for click/key template capture
        self.latest_frame: Optional[np.ndarray] = None
        self.template_banner_until: float = 0.0
        self.template_banner_text: str = ""

        # Global Hotkey Pause State
        self.global_paused = False
        self.setup_global_hotkeys()

        # Notification Banner Timer
        self.save_banner_until = 0.0

    def setup_global_hotkeys(self):
        """Bind global F12 key to toggle pause instantly from any window."""
        def on_press(key):
            if key == keyboard.Key.f12:
                self.global_paused = not self.global_paused
                status = "PAUSED" if self.global_paused else "RESUMED"
                print(f"\n[HOTKEY F12] Automation {status} by user.")
                if self.global_paused:
                    self.hid.stop_all()
                    self.f_prompt_consecutive_frames = 0
                    self.vacuum_active = False
                    self.vacuum_phase = "IDLE"
                    self.is_scanning_for_marker = False
                    if self.is_aiming_bow:
                        self.hid.press_r()
                        self.is_aiming_bow = False

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        print("[HOTKEY] Registered global 'F12' instant toggle pause listener.")

    def _on_debug_mouse_click(self, event, x, y, flags, param):
        """Click on the Debug window to crop a 28x28 template diamond at click location."""
        if event == cv2.EVENT_LBUTTONDOWN and self.latest_frame is not None:
            cy, cx = y, x
            py1 = max(0, cy - 14)
            py2 = min(TARGET_HEIGHT, cy + 14)
            px1 = max(0, cx - 14)
            px2 = min(TARGET_WIDTH, cx + 14)
            patch = self.latest_frame[py1:py2, px1:px2]

            if patch.shape[0] == 28 and patch.shape[1] == 28:
                # Check center color to distinguish gold vs blue
                center_bgr = patch[14, 14]
                center_hsv = cv2.cvtColor(np.uint8([[center_bgr]]), cv2.COLOR_BGR2HSV)[0][0]
                if 85 <= center_hsv[0] <= 115:
                    target_name = "quest_blue.png"
                    mtype = "BLUE"
                else:
                    target_name = "quest_gold.png"
                    mtype = "GOLD"

                out_path = os.path.join(ASSETS_DIR, target_name)
                cv2.imwrite(out_path, patch)
                self.detector.reload_templates()
                self.template_banner_text = f"SAVED {mtype} TEMPLATE ({target_name}) FROM CLICK!"
                self.template_banner_until = time.time() + 2.5
                print(f"[TEMPLATE] Saved 28x28 {mtype} template from click at ({cx}, {cy}) to {out_path}")

    def capture_active_diamond_template(self):
        """Crop 28x28 patch around currently tracked diamond and save to assets."""
        if self.latest_frame is not None and self.active_marker_centroid is not None:
            cx, cy = self.active_marker_centroid
            py1 = max(0, cy - 14)
            py2 = min(TARGET_HEIGHT, cy + 14)
            px1 = max(0, cx - 14)
            px2 = min(TARGET_WIDTH, cx + 14)
            patch = self.latest_frame[py1:py2, px1:px2]

            if patch.shape[0] == 28 and patch.shape[1] == 28:
                mtype = self.active_marker_type or "GOLD"
                target_name = "quest_gold.png" if mtype == "GOLD" else "quest_blue.png"
                out_path = os.path.join(ASSETS_DIR, target_name)
                cv2.imwrite(out_path, patch)
                self.detector.reload_templates()
                self.template_banner_text = f"SAVED {mtype} TEMPLATE ({target_name})!"
                self.template_banner_until = time.time() + 2.5
                print(f"[TEMPLATE] Hotkey 'C' saved {mtype} template at ({cx}, {cy}) to {out_path}")
        else:
            print("[TEMPLATE] No candidate quest diamond tracked right now. Click on the Debug window to crop!")

    def start_loot_vacuum(self, now: float):
        """Initiates multi-second loot vacuum sequence."""
        self.hid.stop_walk()
        self.hid.press_f()
        self.vacuum_active = True
        self.vacuum_phase = "WAIT_ANIMATION"
        self.vacuum_animation_end_time = now + 0.65
        burst_duration = random.uniform(2.2, 2.8)
        self.vacuum_burst_start_time = self.vacuum_animation_end_time
        self.vacuum_burst_end_time = self.vacuum_animation_end_time + burst_duration
        self.next_vacuum_f_time = self.vacuum_animation_end_time
        self.vacuum_taps_count = 1
        self.vacuum_wiggle_stage = 0
        self.vacuum_drain_timeout = self.vacuum_burst_end_time + 4.0
        self.current_state = STATE_CHEST_LOOT
        print(f"[LOOT VACUUM] Chest/loot triggered: waiting 0.65s lid animation then {burst_duration:.2f}s vacuum burst.")

    def run(self):
        setup_dpi_awareness()
        setup_tuner_window(self.config)

        cv2.namedWindow("Genshin Assistant Debug", cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback("Genshin Assistant Debug", self._on_debug_mouse_click)

        fps_counter = 0
        fps_start_time = time.time()
        current_fps = 0.0

        print("\n" + "=" * 70)
        print(" Genshin Impact CV Assistant Running (v3.2 Hybrid Quest)")
        print(" Hierarchy: DIALOGUE > COMBAT_AUTO > CHEST_LOOT > NAVIGATE")
        print(" Features : Gold & Blue Quests, Stamina Exclusion, Hard Center Lock")
        print(" Hotkeys:")
        print("   [F12] : Instant Global Pause / Resume Toggle")
        print("   [C]   : Crop & Save 28x28 Diamond Template (or Click on Screen)")
        print("   [S]   : Save current Tuner settings to config.json")
        print("   [Q]   : Clean Shutdown & Emergency Key Release")
        print("=" * 70 + "\n")

        mss_factory = getattr(mss, "MSS", mss.mss)
        with mss_factory() as sct:
            try:
                while True:
                    self.frame_idx += 1
                    now = time.time()

                    # 1. Window Discovery & Focus Check
                    hwnd, client_rect = find_genshin_window()
                    is_focused = is_genshin_focused(hwnd) if hwnd != 0 else False

                    # 2. Capture Screen Frame
                    frame = None
                    if client_rect is not None and client_rect["width"] > 0 and client_rect["height"] > 0:
                        grab_rect = {
                            "top": client_rect["top"],
                            "left": client_rect["left"],
                            "width": client_rect["width"],
                            "height": client_rect["height"],
                        }
                        try:
                            sct_img = sct.grab(grab_rect)
                            frame_raw = np.array(sct_img)[:, :, :3]
                            if frame_raw.shape[1] != TARGET_WIDTH or frame_raw.shape[0] != TARGET_HEIGHT:
                                frame = cv2.resize(frame_raw, (TARGET_WIDTH, TARGET_HEIGHT))
                            else:
                                frame = frame_raw
                        except Exception:
                            frame = None
                    else:
                        mon = sct.monitors[1]
                        sct_img = sct.grab(mon)
                        frame_raw = np.array(sct_img)[:, :, :3]
                        frame = cv2.resize(frame_raw, (TARGET_WIDTH, TARGET_HEIGHT))

                    if frame is None:
                        time.sleep(0.02)
                        continue

                    self.latest_frame = frame
                    frame_half = cv2.resize(frame, (HALF_WIDTH, HALF_HEIGHT))

                    # 3. SAFETY GUARD: Active-Window Focus Check & F12 Pause
                    if not is_focused:
                        self.hid.stop_all()
                        self.vacuum_active = False
                        self.vacuum_phase = "IDLE"
                        if self.is_aiming_bow:
                            self.hid.press_r()
                            self.is_aiming_bow = False
                    elif self.global_paused:
                        self.hid.stop_all()
                        self.vacuum_active = False
                        self.vacuum_phase = "IDLE"
                        if self.is_aiming_bow:
                            self.hid.press_r()
                            self.is_aiming_bow = False
                    else:
                        # 4. Execute Strict State Hierarchy
                        self._execute_state_machine(frame, frame_half)

                    # 5. Calculate Internal Loop Processing FPS
                    fps_counter += 1
                    if now - fps_start_time >= 1.0:
                        current_fps = fps_counter / (now - fps_start_time)
                        fps_counter = 0
                        fps_start_time = now

                    # 6. Throttle Debug Window Rendering to max 20 FPS (~50ms)
                    if now - self.last_render_time >= 0.05:
                        self.last_render_time = now

                        self.config = {**self.config, **read_tuner_values()}

                        debug_frame = frame.copy()
                        if not is_focused:
                            self._draw_overlay_notice(debug_frame, "PAUSED (Window Unfocused)", (0, 0, 255))
                        elif self.global_paused:
                            self._draw_overlay_notice(debug_frame, "PAUSED (User F12 Toggle)", (0, 165, 255))
                        else:
                            self._draw_hud(debug_frame, current_fps, is_focused)

                        cv2.imshow("Genshin Assistant Debug", debug_frame)

                        # Display Tuner Mask thumbnail
                        _, _, gold_mask, _ = self.detector.track_quest_marker(frame, self.config)
                        mask_display = cv2.cvtColor(cv2.resize(gold_mask, (480, 270)), cv2.COLOR_GRAY2BGR)
                        cv2.putText(
                            mask_display,
                            "Quest Mask Preview (Stamina Masked Out)",
                            (15, 25),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.50,
                            (0, 255, 255),
                            1,
                        )
                        cv2.imshow(TUNER_WINDOW, mask_display)

                        # Handle Hotkeys
                        key = cv2.waitKey(1) & 0xFF
                        if key in (ord("q"), ord("Q"), 27):
                            print("\n[INFO] Clean exit requested by user.")
                            break
                        elif key in (ord("s"), ord("S")):
                            if save_config(self.config):
                                self.save_banner_until = time.time() + 2.0
                        elif key in (ord("c"), ord("C")):
                            self.capture_active_diamond_template()

            finally:
                self.cleanup()

    # ==========================================================================
    # Strict State Machine Hierarchy
    # ==========================================================================
    def _execute_state_machine(self, frame: np.ndarray, frame_half: np.ndarray):
        now = time.time()

        self.detected_party_size = self.detector.detect_party_size(frame)

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 1: DIALOGUE (Strict Top Priority)
        # ----------------------------------------------------------------------
        minimap_present, minimap_score = self.detector.check_minimap_present(frame)

        if not minimap_present:
            if self.current_state != STATE_DIALOGUE:
                self.current_state = STATE_DIALOGUE
                print(f"[STATE] Entered {STATE_DIALOGUE} (Minimap hidden, metric: {minimap_score:.3f})")
                self.hid.stop_all()
                self.hid.release_lmb()
                self.is_scanning_for_marker = False
                self.f_prompt_consecutive_frames = 0
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.next_dialogue_f_time = now + dialogue_advance_delay()
                if self.is_aiming_bow:
                    self.hid.press_r()
                    self.is_aiming_bow = False

            if now >= self.next_dialogue_f_time:
                self.hid.press_f()
                self.next_dialogue_f_time = now + dialogue_advance_delay()

            self.previous_minimap_state = False
            return

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 2: COMBAT_AUTO / COMBAT_MANUAL
        # ----------------------------------------------------------------------
        in_combat, red_pixels, largest_area, enemy_centroid, is_elevated = self.detector.detect_combat_red_hp_bars(frame)

        if in_combat:
            self.combat_clear_start = 0.0
            self.combat_target_centroid = enemy_centroid
            self.combat_largest_area = largest_area

            if self.current_state not in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
                self.current_state = STATE_COMBAT_AUTO
                self.combat_start_time = now
                self.melee_damage_stall_timer = now
                self.last_enemy_hp_pixels = red_pixels
                self.is_scanning_for_marker = False
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.hid.stop_walk()
                self.hid.stop_all()
                print(f"[STATE] Entered {STATE_COMBAT_AUTO} (Enemies: {red_pixels}px, elevated: {is_elevated})")

            if self.current_state == STATE_COMBAT_AUTO and (now - self.combat_start_time > 45.0):
                print(f"[STATE] Combat exceeded 45s threshold -> {STATE_COMBAT_MANUAL}")
                self.current_state = STATE_COMBAT_MANUAL
                self.hid.stop_all()
                play_alert_chime(880, 300)

            if self.current_state == STATE_COMBAT_AUTO:
                self._execute_auto_combat_step(now, is_elevated, red_pixels)
            else:
                self.hid.stop_all()

            self.previous_minimap_state = True
            return

        elif self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            if self.combat_clear_start == 0.0:
                self.combat_clear_start = now

            if now - self.combat_clear_start >= 1.5:
                print("[STATE] Combat ended (0 HP bars for 1.5s). Resuming overworld.")
                self.hid.stop_all()
                if self.is_aiming_bow:
                    self.hid.press_r()
                    self.is_aiming_bow = False
                self.current_state = STATE_NAVIGATE
                self.pending_v_pulse_time = now + random.uniform(0.4, 0.8)

            self.previous_minimap_state = True
            return

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 3: CHEST_LOOT (Active Vacuum OR High-Confidence Ring)
        # ----------------------------------------------------------------------
        if self.vacuum_active:
            self.current_state = STATE_CHEST_LOOT
            self._execute_loot_vacuum_step(now, frame)
            self.previous_minimap_state = True
            return

        ring_detected, ring_box, ring_centroid = self.detector.detect_sealed_chest_ring(frame_half)
        if ring_detected and ring_centroid:
            self.current_state = STATE_CHEST_LOOT
            self.active_chest_box = ring_box
            cx, cy = ring_centroid
            error_x = cx - 640

            f_detected, _ = self.detector.detect_overworld_f_prompt(frame)
            if f_detected or cy >= 440:
                self.start_loot_vacuum(now)
            else:
                self._apply_humanized_steering(error_x, now)
                self.hid.start_walk()

            self.previous_minimap_state = True
            return
        elif self.current_state == STATE_CHEST_LOOT:
            self.current_state = STATE_NAVIGATE
            self.active_chest_box = None

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 4: NAVIGATE (with Road-Following & EMA Smoothing)
        # ----------------------------------------------------------------------
        self.current_state = STATE_NAVIGATE
        self._execute_navigate_step(now, frame, frame_half)
        self.previous_minimap_state = True

    # ==========================================================================
    # Multi-Second Loot Vacuum Step
    # ==========================================================================
    def _execute_loot_vacuum_step(self, now: float, frame: np.ndarray):
        """Drives the multi-second loot vacuum."""
        self.hid.stop_walk()

        if self.vacuum_phase == "WAIT_ANIMATION":
            if now >= self.vacuum_animation_end_time:
                self.vacuum_phase = "BURST"
                self.next_vacuum_f_time = now
            return

        if self.vacuum_phase == "BURST":
            if now >= self.next_vacuum_f_time:
                self.hid.press_f()
                self.vacuum_taps_count += 1
                self.next_vacuum_f_time = now + random.uniform(0.16, 0.24)

            burst_duration = max(0.1, self.vacuum_burst_end_time - self.vacuum_burst_start_time)
            progress = (now - self.vacuum_burst_start_time) / burst_duration

            if progress >= 0.22 and self.vacuum_wiggle_stage == 0:
                self.hid.move_mouse(-18, 0)
                self.vacuum_wiggle_stage = 1
            elif progress >= 0.50 and self.vacuum_wiggle_stage == 1:
                self.hid.move_mouse(36, 0)
                self.vacuum_wiggle_stage = 2
            elif progress >= 0.78 and self.vacuum_wiggle_stage == 2:
                self.hid.move_mouse(-18, 0)
                self.vacuum_wiggle_stage = 3

            if now >= self.vacuum_burst_end_time:
                self.vacuum_phase = "DRAIN_PROMPT"
                self.next_vacuum_f_time = now
            return

        if self.vacuum_phase == "DRAIN_PROMPT":
            f_detected, _ = self.detector.detect_overworld_f_prompt(frame)
            if f_detected and (now < self.vacuum_drain_timeout):
                if now >= self.next_vacuum_f_time:
                    self.hid.press_f()
                    self.vacuum_taps_count += 1
                    self.next_vacuum_f_time = now + random.uniform(0.18, 0.26)
            else:
                print(f"[LOOT VACUUM] Vacuum completed ({self.vacuum_taps_count} total taps). Resuming overworld.")
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.active_chest_box = None
                self.last_overworld_f_time = now
                self.overworld_f_cooldown = random.uniform(1.4, 2.0)
                self.f_prompt_consecutive_frames = 0
                self.current_state = STATE_NAVIGATE

    # ==========================================================================
    # Slower, Paced Starter Combat & Amber Bow Aiming
    # ==========================================================================
    def _execute_auto_combat_step(self, now: float, is_elevated: bool, red_pixels: int):
        """Executes deliberate, humanized starter combat."""
        if red_pixels < self.last_enemy_hp_pixels:
            self.melee_damage_stall_timer = now
            self.last_enemy_hp_pixels = red_pixels
        elif now - self.melee_damage_stall_timer > 4.0:
            is_elevated = True

        amber_slot = 2 if self.detected_party_size >= 2 else 1
        if is_elevated and self.detected_party_size >= 2:
            if not self.is_aiming_bow:
                if self.current_party_slot != amber_slot:
                    self.hid.select_slot(amber_slot)
                    self.current_party_slot = amber_slot
                    self.last_party_swap_time = now
                    time.sleep(0.4)
                self.hid.press_r()
                self.is_aiming_bow = True
                self.bow_aim_shot_count = 0
                self.active_combat_action_label = "Amber: Entering Aim Mode [R]"
                return

            if self.combat_target_centroid is not None:
                ecx, ecy = self.combat_target_centroid
                target_x = ecx
                target_y = ecy + 35

                aim_err_x = target_x - 640
                aim_err_y = target_y - 360

                dx = max(-40, min(40, int(aim_err_x * 0.18)))
                dy = max(-40, min(40, int(aim_err_y * 0.18)))
                self.hid.move_mouse(dx, dy)

                if abs(aim_err_x) < 24 and abs(aim_err_y) < 24:
                    self.active_combat_action_label = f"Amber: Charged Shot ({self.bow_aim_shot_count + 1}/3)"
                    self.hid.hold_lmb()
                    time.sleep(1.6)
                    self.hid.release_lmb()
                    self.bow_aim_shot_count += 1
                    time.sleep(0.3)

                    if self.bow_aim_shot_count >= 3:
                        self.hid.press_r()
                        self.is_aiming_bow = False
                        self.melee_damage_stall_timer = now
                        self.active_combat_action_label = "Amber: Exiting Aim Mode [R]"
            return

        if self.is_aiming_bow:
            self.hid.press_r()
            self.is_aiming_bow = False
            time.sleep(0.3)

        if now < self.combat_step_ready_time:
            return

        # 1. Deliberate Party Swap (every 7.0 - 10.0s)
        if self.detected_party_size > 1 and (now - self.last_party_swap_time >= self.next_party_swap_interval):
            next_slot = (self.current_party_slot % self.detected_party_size) + 1
            self.hid.select_slot(next_slot)
            self.current_party_slot = next_slot
            self.last_party_swap_time = now
            self.next_party_swap_interval = random.uniform(7.0, 10.0)
            self.active_combat_action_label = f"Swap to Slot {next_slot}"
            self.combat_step_ready_time = now + random.uniform(0.40, 0.60)
            return

        # 2. Elemental Skill (E) - every 6.5 - 8.5s
        if now - self.last_skill_e_time >= self.next_skill_e_interval:
            self.hid.press_e()
            self.last_skill_e_time = now
            self.next_skill_e_interval = random.uniform(6.5, 8.5)
            self.active_combat_action_label = f"Slot {self.current_party_slot}: Skill [E]"
            self.combat_step_ready_time = now + random.uniform(0.45, 0.70)
            return

        # 3. Elemental Burst (Q) - every 12.0 - 15.0s
        if now - self.last_burst_q_time >= self.next_burst_q_interval:
            self.hid.press_q()
            self.last_burst_q_time = now
            self.next_burst_q_interval = random.uniform(12.0, 15.0)
            self.active_combat_action_label = f"Slot {self.current_party_slot}: Burst [Q]"
            self.combat_step_ready_time = now + random.uniform(0.55, 0.85)
            return

        # 4. 3-Hit Normal Attack Combos (0.32 - 0.58s spacing) + Pause / Dodge
        if self.combat_combo_hit_count < 3:
            self.hid.click_lmb()
            self.combat_combo_hit_count += 1
            self.active_combat_action_label = f"Normal Attack ({self.combat_combo_hit_count}/3)"
            self.combat_step_ready_time = now + random.uniform(0.32, 0.58)
        else:
            self.combat_combo_hit_count = 0
            if random.random() < 0.35:
                self.hid.click_rmb()
                self.active_combat_action_label = "Reposition: Dodge [RMB]"
                self.combat_step_ready_time = now + random.uniform(0.50, 0.75)
            else:
                self.active_combat_action_label = "Combo Recovery Pause"
                self.combat_step_ready_time = now + random.uniform(0.45, 0.75)

    # ==========================================================================
    # Overworld Navigation (Hybrid Quest Detection & Hard Center Lock)
    # ==========================================================================
    def _execute_navigate_step(self, now: float, frame: np.ndarray, frame_half: np.ndarray):
        # 1. Transition V pulse from Dialogue
        if not self.previous_minimap_state:
            self.pending_v_pulse_time = now + random.uniform(0.3, 0.6)

        if self.pending_v_pulse_time and now >= self.pending_v_pulse_time:
            self.hid.press_v()
            self.pending_v_pulse_time = None

        # 2. Overworld [F] Prompt Detection (Triggers Loot Vacuum!)
        f_prompt_detected, prompt_box = self.detector.detect_overworld_f_prompt(frame)
        if f_prompt_detected and prompt_box:
            self.f_prompt_consecutive_frames += 1
            self.f_prompt_box = prompt_box
            if self.f_prompt_consecutive_frames >= 2 and (now - self.last_overworld_f_time >= self.overworld_f_cooldown):
                self.show_overworld_f_box_until = now + 1.2
                self.start_loot_vacuum(now)
                return
        else:
            self.f_prompt_consecutive_frames = 0

        # 3. Downscaled Ground Motion Differencing (Run only every 3rd frame to save CPU)
        if self.frame_idx % 3 == 0:
            raw_motion, self.prev_ground_gray_half = self.detector.calculate_ground_motion_downscaled(
                frame_half, self.prev_ground_gray_half
            )
            self.smooth_motion_score = 0.75 * self.smooth_motion_score + 0.25 * raw_motion

        # Vertical Jump / Obstacle Unstuck check: if motion < 1.05 for > 1.8s while walking
        if self.hid.is_walking and self.smooth_motion_score < 1.05:
            if self.unstuck_still_start_time == 0.0:
                self.unstuck_still_start_time = now
            elif now - self.unstuck_still_start_time > 1.8:
                if now - self.last_lateral_unstuck_time > 2.0:
                    print("[UNSTUCK] Obstacle detected: pulsing SPACE jump & lateral steering.")
                    self.hid.press_space()
                    lateral_dx = 65 if random.random() < 0.5 else -65
                    self.hid.move_mouse(lateral_dx, 0)
                    self.last_lateral_unstuck_time = now
                    self.unstuck_still_start_time = 0.0
        else:
            self.unstuck_still_start_time = 0.0

        # 4. Minimap Road-Following Angle Analysis
        quest_bearing, road_bearing, has_road = self.detector.analyze_minimap_road_and_quest(frame)
        self.active_road_following = has_road

        # 5. Hybrid Gold & Blue Quest Marker Tracking (with Stamina Exclusion Zone)
        centroid, marker_type, _, bbox = self.detector.track_quest_marker(frame, self.config)
        self.active_marker_type = marker_type
        self.active_marker_centroid = centroid
        self.active_marker_bbox = bbox

        if centroid is not None:
            self.marker_last_seen_time = now
            self.is_stuck_alert = False
            self.is_scanning_for_marker = False
            self.marker_lost_reported = False
            cx, cy = centroid

            raw_error_x = cx - 640

            # ------------------------------------------------------------------
            # HARD CENTER LOCK & ROAD BIAS ARBITRATION
            # ------------------------------------------------------------------
            # When abs(error_x) <= 28 px (marker is centered):
            # Immediately zero out steering (dx = 0) and suppress minimap road bias
            # to prevent post-lock camera drift!
            lock_deadzone = int(self.config.get("center_lock_deadzone", 28))

            if abs(raw_error_x) <= lock_deadzone:
                # HARD CENTER LOCK ENGAGED
                effective_error_x = 0.0
                self.ema_error_x = 0.0
                # Camera is centered onto diamond -> No mouse movement!
            else:
                # Only apply subtle minimap road-following bias when marker is
                # distant/off-center (abs(error_x) > 45 px)
                if abs(raw_error_x) > 45 and has_road and road_bearing is not None and self.config.get("road_following_enabled", True):
                    road_offset_x = road_bearing * (640.0 / (math.pi / 2.5))
                    effective_error_x = 0.70 * raw_error_x + 0.30 * road_offset_x
                else:
                    effective_error_x = float(raw_error_x)

                # Exponential Moving Average (EMA) for smooth camera panning
                alpha = float(self.config.get("steer_ema_alpha", 0.35))
                self.ema_error_x = (1.0 - alpha) * self.ema_error_x + alpha * effective_error_x

                # Update sweep direction toward marker
                if raw_error_x > 20:
                    self.scan_sweep_direction = 1
                elif raw_error_x < -20:
                    self.scan_sweep_direction = -1

                # Humanized camera steering (with deadzone & rate limiter)
                self._apply_humanized_steering(int(self.ema_error_x), now)

            # Hard Turn Check: if abs(error_x) > 160 px, pause walking while turning camera hard
            is_hard_turning = abs(raw_error_x) > 160
            if is_hard_turning:
                if self.hid.is_walking:
                    self.hid.stop_walk()
            else:
                # Normal forward navigation
                walk_time = now - self.hid.walk_start_time if self.hid.is_walking else 0.0
                if self.hid.is_walking and walk_time > 2.5 and self.smooth_motion_score < 1.05:
                    self.hid.stop_all()
                    self.is_stuck_alert = True
                    play_alert_chime(880, 220)
                elif not self.is_stuck_alert and not self.vacuum_active:
                    self.hid.start_walk()

                    # Variable Sprint Cadence: Trigger RMB dash at randomized intervals (2.1 - 3.8s)
                    if now - self.last_sprint_time >= self.next_sprint_interval:
                        self.hid.click_rmb()
                        self.last_sprint_time = now
                        self.next_sprint_interval = random.uniform(2.1, 3.8)

                    # Firmware Keepalive: re-send W1 every 4.0s if no other command was sent in that window
                    if self.hid.is_walking and (now - self.hid.last_command_time >= 4.0):
                        self.hid.send_command("W1")

        else:
            # ------------------------------------------------------------------
            # LOST-MARKER HANDLING: PULSE 'V' FIRST, WAIT 0.9s, THEN SWEEP
            # ------------------------------------------------------------------
            time_lost = now - self.marker_last_seen_time

            # 1. When neither gold nor blue marker is detected for > 0.75s:
            # First dispatch 'V' (with 4.5s cooldown) to re-display trail & marker
            v_delay = float(self.config.get("lost_marker_v_delay", 0.75))
            v_cooldown = float(self.config.get("quest_v_pulse_cooldown", 4.5))

            if time_lost > v_delay and (now - self.last_quest_v_pulse_time >= v_cooldown):
                self.hid.press_v()
                self.last_quest_v_pulse_time = now
                print(f"[NAV] Quest marker lost ({time_lost:.2f}s > {v_delay}s): dispatched 'V' quest path pulse.")

            # 2. Wait 0.9s after 'V' (total > 1.65s): only if STILL lost, pause walk and sweep camera
            sweep_delay = float(self.config.get("lost_marker_sweep_delay", 1.65))

            if time_lost > sweep_delay:
                if self.hid.is_walking:
                    self.hid.stop_walk()

                self.is_scanning_for_marker = True

                # Horizontal sweep bursts (22-36 px every 0.18-0.32s)
                if now - self.last_scan_sweep_time >= self.next_scan_sweep_interval:
                    burst_dx = random.randint(22, 36) * self.scan_sweep_direction
                    self.hid.move_mouse(burst_dx, 0)
                    self.last_scan_sweep_time = now
                    self.next_scan_sweep_interval = random.uniform(0.18, 0.32)

    # ==========================================================================
    # Smooth, Rate-Limited Camera Steering (with 28px Deadzone)
    # ==========================================================================
    def _apply_humanized_steering(self, error_x: int, now: float):
        """
        Applies smoothed, rate-limited (15-20 Hz) camera steering with a 28px deadzone.
        Clamps steering to [-45, 45] with Gaussian noise and human overshoot.
        """
        lock_deadzone = int(self.config.get("center_lock_deadzone", 28))
        if abs(error_x) <= lock_deadzone:
            return

        if now - self.last_mouse_packet_time < 0.055:
            return

        kp = max(0.01, float(self.config["Kp_x100"]) / 100.0)

        noise = random.gauss(0, 2.0)
        overshoot = random.uniform(0.85, 1.15)
        raw_dx = (error_x * (kp * overshoot)) + noise
        steer_dx = max(-45, min(45, int(raw_dx)))

        self.hid.move_mouse(steer_dx, 0)
        self.last_mouse_packet_time = now

    # ==========================================================================
    # Visual HUD & Overlay Rendering (Rendered at max 20 FPS)
    # ==========================================================================
    def _draw_hud(self, debug_frame: np.ndarray, fps: float, is_focused: bool):
        """Draw bounding boxes, telemetry stats, reticles, and hotkey banners on debug frame."""
        # 1. Minimap Circle ROI (Cyan)
        cv2.circle(debug_frame, MINIMAP_CENTER, MINIMAP_RADIUS, (255, 255, 0), 1)
        road_tag = "Road: ON" if self.active_road_following else "Road: OFF"
        cv2.putText(debug_frame, road_tag, (MINIMAP_CENTER[0] - 28, MINIMAP_CENTER[1] + 82), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (255, 255, 0), 1)

        # 2. Navigation ROI (Yellow)
        ny1, ny2, nx1, nx2 = ROI_NAV
        cv2.rectangle(debug_frame, (nx1, ny1), (nx2, ny2), (0, 255, 255), 1)

        # 3. Stamina / Character Exclusion Zone (Gray dashed outline)
        ey1, ey2, ex1, ex2 = ROI_EXCLUSION_STAMINA
        cv2.rectangle(debug_frame, (ex1, ey1), (ex2, ey2), (90, 90, 90), 1)
        cv2.putText(debug_frame, "Stamina Exclusion Zone", (ex1 + 6, ey1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (120, 120, 120), 1)

        # 4. Choice ROI (Green)
        cy1, cy2, cx1, cx2 = ROI_CHOICE
        cv2.rectangle(debug_frame, (cx1, cy1), (cx2, cy2), (0, 255, 0), 1)

        # 5. Ground Motion ROI (Magenta in 1280x720)
        gy1, gy2, gx1, gx2 = ROI_GROUND_HALF[0] * 2, ROI_GROUND_HALF[1] * 2, ROI_GROUND_HALF[2] * 2, ROI_GROUND_HALF[3] * 2
        cv2.rectangle(debug_frame, (gx1, gy1), (gx2, gy2), (255, 0, 255), 1)
        cv2.putText(debug_frame, f"Ground Motion: {self.smooth_motion_score:.2f}", (gx1 + 5, gy1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)

        # 6. Combat HP Scan ROI (Red)
        kby1, kby2, kbx1, kbx2 = ROI_COMBAT
        combat_color = (0, 0, 255) if self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL) else (50, 50, 160)
        cv2.rectangle(debug_frame, (kbx1, kby1), (kbx2, kby2), combat_color, 1)

        # 7. Interact Prompt ROI (Teal)
        iy1, iy2, ix1, ix2 = ROI_INTERACT
        cv2.rectangle(debug_frame, (ix1, iy1), (ix2, iy2), (180, 180, 0), 1)

        # 8. Party Portrait Stack ROI
        py1, py2, px1, px2 = ROI_PARTY
        cv2.rectangle(debug_frame, (px1, py1), (px2, py2), (140, 100, 220), 1)
        cv2.putText(debug_frame, f"Party: {self.detected_party_size} Slots", (px1 - 25, py1 - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (140, 100, 220), 1)

        # 9. Draw Active Quest Reticle (Gold or Blue)
        if self.active_marker_centroid is not None and self.current_state == STATE_NAVIGATE:
            mcx, mcy = self.active_marker_centroid
            raw_err = mcx - 640
            is_locked = abs(raw_err) <= int(self.config.get("center_lock_deadzone", 28))

            if self.active_marker_type == "BLUE":
                reticle_color = (255, 200, 0)  # Cyan-blue
                reticle_label = "QUEST [BLUE]"
            else:
                reticle_color = (0, 215, 255)  # Gold/Yellow
                reticle_label = "QUEST [GOLD]"

            if is_locked:
                reticle_label += " [CENTER LOCKED]"
                # Draw locking corner brackets
                cv2.rectangle(debug_frame, (mcx - 18, mcy - 18), (mcx + 18, mcy + 18), (0, 255, 0), 2)
            else:
                cv2.circle(debug_frame, (mcx, mcy), 10, reticle_color, 2)

            cv2.line(debug_frame, (mcx - 16, mcy), (mcx + 16, mcy), reticle_color, 2)
            cv2.line(debug_frame, (mcx, mcy - 16), (mcx, mcy + 16), reticle_color, 2)
            cv2.putText(debug_frame, reticle_label, (mcx - 45, mcy - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.50, reticle_color, 2)

        # Draw Overworld [F] Prompt Box (Cyan)
        if (time.time() < self.show_overworld_f_box_until or self.f_prompt_consecutive_frames >= 2) and self.f_prompt_box:
            bx, by, bw, bh = self.f_prompt_box
            cv2.rectangle(debug_frame, (bx, by), (bx + bw, by + bh), (255, 255, 0), 2)
            cv2.putText(debug_frame, "OVERWORLD [F]", (bx - 10, by - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)

        # Draw Sealed Chest Ring Box if spotted
        if self.active_chest_box:
            bx, by, bw, bh = self.active_chest_box
            cv2.rectangle(debug_frame, (bx, by), (bx + bw, by + bh), (0, 165, 255), 2)
            cv2.putText(debug_frame, "SEALED CHEST RING", (bx, by - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 165, 255), 2)

        # Draw enemy centroid if in combat
        if self.combat_target_centroid is not None and self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            ecx, ecy = self.combat_target_centroid
            cv2.drawMarker(debug_frame, (ecx, ecy), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 16, 2)
            elevated_tag = " [ELEVATED]" if ecy < 290 else ""
            cv2.putText(
                debug_frame,
                f"TARGET ({int(self.combat_largest_area)}px){elevated_tag}",
                (ecx - 40, ecy - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 0, 255),
                1,
            )

        # Top Information Banner
        cv2.rectangle(debug_frame, (0, 0), (TARGET_WIDTH, 36), (20, 20, 20), -1)

        state_color = (0, 255, 0)
        if self.current_state == STATE_COMBAT_AUTO:
            state_color = (0, 100, 255)
        elif self.current_state == STATE_COMBAT_MANUAL:
            state_color = (0, 0, 255)
        elif self.current_state == STATE_DIALOGUE:
            state_color = (255, 180, 0)
        elif self.current_state == STATE_CHEST_LOOT:
            state_color = (0, 165, 255)

        cv2.putText(
            debug_frame,
            f"STATE: {self.current_state}",
            (15, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            state_color,
            2,
        )

        # Telemetry info
        if self.current_state == STATE_COMBAT_AUTO:
            elapsed = time.time() - self.combat_start_time
            aim_tag = " [AIM-SHOT]" if self.is_aiming_bow else ""
            telemetry_text = f"Combat: {elapsed:.1f}s/45s | Slot {self.current_party_slot}/{self.detected_party_size} | {self.active_combat_action_label}{aim_tag}"
            cv2.putText(debug_frame, telemetry_text, (270, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (0, 220, 255), 1)
        elif self.current_state == STATE_CHEST_LOOT:
            if self.vacuum_active:
                vac_text = f"VACUUM: {self.vacuum_phase} | Taps: {self.vacuum_taps_count}"
            else:
                vac_text = "Approaching Sealed Chest..."
            cv2.putText(debug_frame, vac_text, (320, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (0, 165, 255), 2)
        else:
            walk_label = "WALK [W1]" if self.hid.is_walking else "IDLE"
            walk_color = (0, 255, 0) if self.hid.is_walking else (160, 160, 160)
            cv2.putText(debug_frame, f"Input: {walk_label}", (320, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, walk_color, 2)

            focus_label = "FOCUSED" if is_focused else "UNFOCUSED"
            focus_color = (0, 255, 0) if is_focused else (0, 0, 255)
            cv2.putText(debug_frame, f"Focus: {focus_label}", (530, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, focus_color, 2)

            mode_label = "DRY-RUN" if self.hid.dry_run else f"HID ({self.port})"
            cv2.putText(debug_frame, f"Mode: {mode_label}", (740, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (200, 200, 200), 1)

        cv2.putText(debug_frame, f"FPS: {fps:.1f}", (1160, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)

        # Bottom Hotkey Bar
        cv2.rectangle(debug_frame, (0, TARGET_HEIGHT - 32), (TARGET_WIDTH, TARGET_HEIGHT), (25, 25, 25), -1)
        hotkey_text = "[F12] Pause | [C] Capture Diamond Template | [S] Save Tuner | [Q] Exit"
        cv2.putText(
            debug_frame,
            hotkey_text,
            (TARGET_WIDTH // 2 - 310, TARGET_HEIGHT - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.52,
            (220, 220, 220),
            1,
        )

        # Config Saved notification
        if time.time() < self.save_banner_until:
            cv2.rectangle(debug_frame, (500, 50), (780, 95), (0, 140, 0), -1)
            cv2.putText(debug_frame, "CONFIG SAVED!", (535, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)

        # Template Captured notification
        if time.time() < self.template_banner_until:
            cv2.rectangle(debug_frame, (380, 50), (900, 95), (140, 80, 0), -1)
            cv2.putText(debug_frame, self.template_banner_text, (400, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)

        # Stuck Alert Banner
        if self.is_stuck_alert:
            cv2.rectangle(debug_frame, (420, 120), (860, 170), (0, 0, 200), -1)
            cv2.putText(debug_frame, "OBSTACLE / STUCK DETECTED!", (445, 155), cv2.FONT_HERSHEY_SIMPLEX, 0.75, (255, 255, 255), 2)

        # Off-Screen Quest Marker Scan Banner
        if self.is_scanning_for_marker and self.current_state == STATE_NAVIGATE:
            cv2.rectangle(debug_frame, (430, 80), (850, 118), (0, 120, 255), -1)
            cv2.putText(debug_frame, "SCANNING FOR QUEST MARKER...", (450, 107), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)

        # Combat Manual Timeout Banner
        if self.current_state == STATE_COMBAT_MANUAL:
            cv2.rectangle(debug_frame, (320, 110), (960, 165), (0, 0, 180), -1)
            cv2.putText(debug_frame, "COMBAT TIMEOUT (>45s) - MANUAL TAKEOVER", (345, 145), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)

    def _draw_overlay_notice(self, frame: np.ndarray, text: str, color: Tuple[int, int, int]):
        """Draw semi-transparent overlay to indicate safe paused state."""
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

        cv2.rectangle(frame, (240, 310), (1040, 410), (20, 20, 20), -1)
        cv2.rectangle(frame, (240, 310), (1040, 410), color, 3)
        cv2.putText(frame, text, (TARGET_WIDTH // 2 - 280, 370), cv2.FONT_HERSHEY_SIMPLEX, 0.95, color, 2)

    def cleanup(self):
        """Emergency stop inputs, disconnect serial, and close OpenCV windows."""
        print("[CLEANUP] Stopping HID inputs and releasing resources...")
        if self.is_aiming_bow:
            self.hid.press_r()
            self.is_aiming_bow = False
        self.hid.close()
        cv2.destroyAllWindows()
        print("[CLEANUP] Genshin Assistant terminated safely.")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Genshin Impact CV Automation with ESP32-S3 HID (v3.2 Hybrid Quest)")
    parser.add_argument("--port", type=str, default="COM4", help="Serial COM port of ESP32-S3 (default: COM4)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run screen capture, state machine, and tuner without requiring physical ESP32-S3 connected",
    )
    parser.add_argument(
        "--capture-icon",
        type=str,
        choices=["gold", "blue"],
        default=None,
        help="Run in diamond icon capture mode: specify 'gold' or 'blue'",
    )
    args = parser.parse_args()

    assistant = GenshinAssistant(port=args.port, dry_run=args.dry_run, capture_icon_mode=args.capture_icon)
    assistant.run()


if __name__ == "__main__":
    main()
