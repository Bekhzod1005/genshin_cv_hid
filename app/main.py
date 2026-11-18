#!/usr/bin/env python3
"""
Genshin Impact Hardware-Assisted Computer Vision Automation
============================================================
Architecture:
- Screen Capture: mss (direct desktop / client-rect capture normalized to 1280x720)
- Hardware Input: ESP32-S3 Microcontroller running TinyUSB HID Keyboard & Mouse
- State Machine:
    1. COMBAT_AUTO   : Non-deterministic starter-roster auto-combat (AR 1-10 friendly)
                       with dynamic step queues, distance closing, and 45s safety fallback.
    2. COMBAT_MANUAL : Fallback manual state when auto-combat exceeds 45s timeout.
    3. DIALOGUE      : Minimap disappearance -> choice icon detection -> randomized F.
    4. NAVIGATE      : Humanized camera steering (deadzone + Gaussian noise + throttled flicks),
                       variable sprint cadence (RMB), quest trail (V pulse), and anti-stuck differencing.
- Safety & Guards:
    - Active Window Focus Lock (win32gui.GetForegroundWindow() == hwnd)
    - Global F12 Instant Pause / Resume Toggle (pynput)
    - 8-Second Safety Watchdog in Microcontroller Firmware
    - Live HSV & Kp Tuner Window (persists to config.json with 'S')
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
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

MINIMAP_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "minimap_border.png")
CHOICE_TEMPLATE_PATH = os.path.join(ASSETS_DIR, "choice_icon.png")

TARGET_WIDTH = 1280
TARGET_HEIGHT = 720

# Region of Interest (ROI) Definitions in 1280x720 Space: [y1, y2, x1, x2]
ROI_MINIMAP = (0, 180, 0, 250)         # Top-Left Minimap
ROI_NAV = (100, 540, 190, 1090)        # Central Gold Quest Marker Tracking Area
ROI_CHOICE = (320, 560, 700, 1090)     # Middle-Right Dialogue Option Area
ROI_GROUND = (500, 680, 440, 840)      # Lower-Center Ground Motion Differencing
ROI_COMBAT = (36, 360, 100, 1180)      # Upper/Middle Enemy Health Bar Area

# State Definitions
STATE_NAVIGATE = "NAVIGATE"
STATE_DIALOGUE = "DIALOGUE"
STATE_COMBAT_AUTO = "COMBAT_AUTO"
STATE_COMBAT_MANUAL = "COMBAT_MANUAL"


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
    """
    Generate non-deterministic action delays following a log-normal distribution.
    Features a 10% chance per cycle to inject a natural 0.85s-1.55s human micro-pause.
    """
    val = random.lognormvariate(math.log(max(mean, 0.05)), sigma)
    clamped = max(min_val, min(max_val, val))
    if allow_micro_pause and random.random() < 0.10:
        clamped += random.uniform(0.85, 1.55)
    return clamped


# ==============================================================================
# DPI Awareness Setup
# ==============================================================================
def setup_dpi_awareness():
    """Ensure Windows desktop coordinates match actual pixels (no scaling distortion)."""
    if os.name != "nt":
        return
    try:
        # Per-Monitor DPI Aware v2
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
# ESP32-S3 Serial HID Controller
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

            # Handshake verification
            if self.ping():
                print(f"[HID] Handshake verified! ESP32-S3 ready on {self.port}.")
            else:
                print(f"[WARN] ESP32-S3 did not return 'PONG'. Serial connection open, continuing...")
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

    def press_f(self):
        """Trigger dialogue / interact key ('F')."""
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
        """Begin charged attack (hold Left Mouse Button)."""
        self.send_command("LMB_HOLD")
        self.is_lmb_held = True

    def release_lmb(self):
        """Release charged attack (release Left Mouse Button)."""
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

    def move_mouse(self, dx: int):
        """Move mouse horizontally by dx pixels (clamped to [-50, 50])."""
        clamped_dx = max(-50, min(50, int(dx)))
        if clamped_dx != 0:
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
    "Kp_x100": 15,
}


def load_config() -> Dict[str, int]:
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


def save_config(config: Dict[str, int]):
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


def setup_tuner_window(initial_cfg: Dict[str, int]):
    cv2.namedWindow(TUNER_WINDOW, cv2.WINDOW_AUTOSIZE)
    cv2.createTrackbar("H_min", TUNER_WINDOW, initial_cfg["H_min"], 179, nothing)
    cv2.createTrackbar("H_max", TUNER_WINDOW, initial_cfg["H_max"], 179, nothing)
    cv2.createTrackbar("S_min", TUNER_WINDOW, initial_cfg["S_min"], 255, nothing)
    cv2.createTrackbar("S_max", TUNER_WINDOW, initial_cfg["S_max"], 255, nothing)
    cv2.createTrackbar("V_min", TUNER_WINDOW, initial_cfg["V_min"], 255, nothing)
    cv2.createTrackbar("V_max", TUNER_WINDOW, initial_cfg["V_max"], 255, nothing)
    cv2.createTrackbar("Kp_x100", TUNER_WINDOW, initial_cfg["Kp_x100"], 100, nothing)


def read_tuner_values() -> Dict[str, int]:
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
        # Load optional PNG templates if present
        self.minimap_template = None
        if os.path.exists(MINIMAP_TEMPLATE_PATH):
            tmpl = cv2.imread(MINIMAP_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.minimap_template = tmpl
                print(f"[VISION] Loaded minimap template from {MINIMAP_TEMPLATE_PATH}")

        self.choice_template = None
        if os.path.exists(CHOICE_TEMPLATE_PATH):
            tmpl = cv2.imread(CHOICE_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.choice_template = tmpl
                print(f"[VISION] Loaded dialogue choice template from {CHOICE_TEMPLATE_PATH}")

    def check_minimap_present(self, frame_bgr: np.ndarray) -> Tuple[bool, float]:
        """
        State 2 trigger: Detect presence of minimap UI.
        If minimap disappears, the game is in cutscene / dialogue mode.
        """
        y1, y2, x1, x2 = ROI_MINIMAP
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # 1. Template match if template image exists
        if self.minimap_template is not None:
            res = cv2.matchTemplate(gray, self.minimap_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(res)
            return (max_val >= 0.58), float(max_val)

        # 2. Canny edge-density fallback
        edges = cv2.Canny(gray, 60, 160)
        total_pixels = (y2 - y1) * (x2 - x1)
        edge_density = float(np.count_nonzero(edges)) / float(total_pixels)
        # In overworld, minimap circle border + icons produce > 3.2% edge density
        is_present = edge_density > 0.032
        return is_present, edge_density

    def check_choice_icon(self, frame_bgr: np.ndarray) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
        """
        Detect dialogue speech-bubble / choice icon in middle-right ROI.
        Returns (detected, (x, y, w, h) in 1280x720 coordinates).
        """
        y1, y2, x1, x2 = ROI_CHOICE
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        # 1. Template match if available
        if self.choice_template is not None:
            res = cv2.matchTemplate(gray, self.choice_template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(res)
            if max_val >= 0.68:
                tw, th = self.choice_template.shape[1], self.choice_template.shape[0]
                box = (x1 + max_loc[0], y1 + max_loc[1], tw, th)
                return True, box

        # 2. High-contrast white icon detection fallback
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

    def detect_combat_red_hp_bars(
        self, frame_bgr: np.ndarray
    ) -> Tuple[bool, int, float, Optional[Tuple[int, int]]]:
        """
        Combat trigger: Detect enemy red HP bars in upper/middle screen (y: 36..360).
        Returns:
            - in_combat: bool (True if enemy red HP pixels exceed threshold)
            - pixel_count: int
            - largest_contour_area: float
            - target_centroid: Optional (cx, cy) in 1280x720 frame
        """
        y1, y2, x1, x2 = ROI_COMBAT
        roi = frame_bgr[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        # Bright red health bar ranges wrapping around Hue limits
        mask1 = cv2.inRange(hsv, np.array([0, 150, 140]), np.array([10, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([170, 150, 140]), np.array([179, 255, 255]))
        red_mask = cv2.bitwise_or(mask1, mask2)

        # Morphological filter tuned for horizontal health bars
        bar_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 2))
        filtered = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, bar_kernel)
        pixel_count = int(cv2.countNonZero(filtered))

        largest_area = 0.0
        target_centroid = None

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

        in_combat = pixel_count >= 130
        return in_combat, pixel_count, largest_area, target_centroid

    def track_gold_quest_marker(
        self, frame_bgr: np.ndarray, tuner_cfg: Dict[str, int]
    ) -> Tuple[Optional[Tuple[int, int]], np.ndarray]:
        """
        State 3: Track Gold Quest Marker centroid (cx, cy) inside Central Navigation ROI.
        """
        y1, y2, x1, x2 = ROI_NAV
        roi = frame_bgr[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        lower_gold = np.array([tuner_cfg["H_min"], tuner_cfg["S_min"], tuner_cfg["V_min"]], dtype=np.uint8)
        upper_gold = np.array([tuner_cfg["H_max"], tuner_cfg["S_max"], tuner_cfg["V_max"]], dtype=np.uint8)
        mask = cv2.inRange(hsv, lower_gold, upper_gold)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        clean_mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        clean_mask = cv2.morphologyEx(clean_mask, cv2.MORPH_CLOSE, kernel)

        contours, _ = cv2.findContours(clean_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        best_centroid = None
        max_area = 0.0

        for c in contours:
            area = cv2.contourArea(c)
            if 30 <= area <= 3200:
                if area > max_area:
                    M = cv2.moments(c)
                    if M["m00"] > 0:
                        cx = int(M["m10"] / M["m00"]) + x1
                        cy = int(M["m01"] / M["m00"]) + y1
                        best_centroid = (cx, cy)
                        max_area = area

        return best_centroid, clean_mask

    def calculate_ground_motion(
        self, frame_bgr: np.ndarray, prev_ground_gray: Optional[np.ndarray]
    ) -> Tuple[float, np.ndarray]:
        """
        Frame-differencing motion score on the lower-center ground region.
        Used to detect if the character is stuck running into a wall or cliff.
        """
        y1, y2, x1, x2 = ROI_GROUND
        roi = frame_bgr[y1:y2, x1:x2]
        curr_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if prev_ground_gray is None:
            return 10.0, curr_gray

        diff = cv2.absdiff(curr_gray, prev_ground_gray)
        motion_score = float(np.mean(diff))
        return motion_score, curr_gray


# ==============================================================================
# Main Assistant Orchestrator
# ==============================================================================
class GenshinAssistant:
    def __init__(self, port: str = "COM4", dry_run: bool = False):
        self.port = port
        self.dry_run = dry_run

        self.hid = ESP32HIDController(port=self.port, dry_run=self.dry_run)
        self.detector = VisionDetector()
        self.config = load_config()

        # State machine variables
        self.current_state = STATE_NAVIGATE
        self.previous_minimap_state = True
        self.last_choice_f_time = 0.0
        self.pending_f_press_time: Optional[float] = None
        self.marker_last_seen_time = time.time()

        # Humanized Navigation Steering & Movement
        self.last_steer_time = 0.0
        self.next_steer_interval = random.uniform(0.12, 0.28)
        self.last_sprint_time = time.time()
        self.next_sprint_interval = random.uniform(2.1, 3.8)
        self.pending_v_pulse_time: Optional[float] = None

        # Stuck detection
        self.prev_ground_gray: Optional[np.ndarray] = None
        self.smooth_motion_score = 10.0
        self.is_stuck_alert = False

        # Non-Deterministic Auto-Combat System
        self.combat_start_time = 0.0
        self.combat_clear_start = 0.0
        self.combat_queue: List[Dict[str, Any]] = []
        self.combat_step_ready_time = 0.0
        self.active_combat_action_label = "Idle"
        self.combat_target_centroid: Optional[Tuple[int, int]] = None
        self.combat_largest_area: float = 0.0
        self.is_closing_distance: bool = False
        self.close_distance_finish_time: float = 0.0

        # Global hotkey pause state
        self.global_paused = False
        self.setup_global_hotkeys()

        # Notification banner timer
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
                    self.combat_queue.clear()
                    self.is_closing_distance = False

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        print("[HOTKEY] Registered global 'F12' instant toggle pause listener.")

    def run(self):
        setup_dpi_awareness()
        setup_tuner_window(self.config)

        cv2.namedWindow("Genshin Assistant Debug", cv2.WINDOW_AUTOSIZE)

        fps_counter = 0
        fps_start_time = time.time()
        current_fps = 0.0

        print("\n" + "=" * 68)
        print(" Genshin Impact CV Assistant Running (v2 Humanized Auto-Combat)")
        print(" Hotkeys:")
        print("   [F12] : Instant Global Pause / Resume Toggle")
        print("   [S]   : Save current Tuner settings to config.json")
        print("   [Q]   : Clean Shutdown & Emergency Key Release")
        print("=" * 68 + "\n")

        with mss.mss() as sct:
            try:
                while True:
                    loop_start = time.time()

                    # 1. Update Tuner sliders
                    self.config = read_tuner_values()

                    # 2. Window Discovery & Focus Check
                    hwnd, client_rect = find_genshin_window()
                    is_focused = is_genshin_focused(hwnd) if hwnd != 0 else False

                    # 3. Capture Screen Frame
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
                        # Fallback capture primary monitor when game window isn't located yet
                        mon = sct.monitors[1]
                        sct_img = sct.grab(mon)
                        frame_raw = np.array(sct_img)[:, :, :3]
                        frame = cv2.resize(frame_raw, (TARGET_WIDTH, TARGET_HEIGHT))

                    if frame is None:
                        time.sleep(0.04)
                        continue

                    # Create a copy for visual debug annotations
                    debug_frame = frame.copy()

                    # 4. SAFETY GUARD: Check Window Focus and Global Pause
                    if not is_focused:
                        # Unfocused: Immediately release keys and freeze outputs
                        self.hid.stop_all()
                        self.combat_queue.clear()
                        self._draw_overlay_notice(debug_frame, "PAUSED (Window Unfocused)", (0, 0, 255))
                    elif self.global_paused:
                        # User-toggled F12 pause
                        self.hid.stop_all()
                        self.combat_queue.clear()
                        self._draw_overlay_notice(debug_frame, "PAUSED (User F12 Toggle)", (0, 165, 255))
                    else:
                        # Active automation loop
                        self._execute_state_machine(frame, debug_frame)

                    # 5. Calculate FPS
                    fps_counter += 1
                    if time.time() - fps_start_time >= 1.0:
                        current_fps = fps_counter / (time.time() - fps_start_time)
                        fps_counter = 0
                        fps_start_time = time.time()

                    # 6. Render HUD, ROIs, and Banners
                    self._draw_hud(debug_frame, current_fps, is_focused)

                    # 7. Show Debug Preview and Live Tuner Mask
                    cv2.imshow("Genshin Assistant Debug", debug_frame)

                    # Display Tuner Mask thumbnail in Tuner window
                    _, gold_mask = self.detector.track_gold_quest_marker(frame, self.config)
                    mask_display = cv2.cvtColor(cv2.resize(gold_mask, (480, 270)), cv2.COLOR_GRAY2BGR)
                    cv2.putText(
                        mask_display,
                        "Quest Gold Mask Preview",
                        (15, 25),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.6,
                        (0, 255, 255),
                        2,
                    )
                    cv2.imshow(TUNER_WINDOW, mask_display)

                    # 8. User Interaction (Hotkeys inside OpenCV window)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (ord("q"), ord("Q"), 27):  # Q or ESC
                        print("\n[INFO] Clean exit requested by user.")
                        break
                    elif key in (ord("s"), ord("S")):
                        if save_config(self.config):
                            self.save_banner_until = time.time() + 2.0

            finally:
                self.cleanup()

    # ==========================================================================
    # State Machine Core
    # ==========================================================================
    def _execute_state_machine(self, frame: np.ndarray, debug_frame: np.ndarray):
        now = time.time()

        # ----------------------------------------------------------------------
        # STATE 1: Combat Evaluation (COMBAT_AUTO / COMBAT_MANUAL)
        # ----------------------------------------------------------------------
        in_combat, red_pixels, largest_area, enemy_centroid = self.detector.detect_combat_red_hp_bars(frame)
        self.combat_target_centroid = enemy_centroid
        self.combat_largest_area = largest_area

        if in_combat:
            # Handle state transitions into Combat
            if self.current_state not in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
                self.current_state = STATE_COMBAT_AUTO
                self.combat_start_time = now
                self.combat_clear_start = 0.0
                self.combat_queue.clear()
                self.hid.stop_walk()
                print(f"[STATE] Entered {STATE_COMBAT_AUTO} (Red HP Bar detected: {red_pixels} px, area: {largest_area:.1f})")

            # 45-Second Safety Fallback check
            if self.current_state == STATE_COMBAT_AUTO and (now - self.combat_start_time > 45.0):
                print(f"[STATE] Combat exceeded 45s threshold! Falling back to {STATE_COMBAT_MANUAL}.")
                self.current_state = STATE_COMBAT_MANUAL
                self.hid.stop_all()
                self.combat_queue.clear()
                play_alert_chime(880, 300)

            # Execute Auto-Combat sequence if in COMBAT_AUTO
            if self.current_state == STATE_COMBAT_AUTO:
                self.combat_clear_start = 0.0
                self._execute_auto_combat_step(now)
            else:
                # In COMBAT_MANUAL: hold keys released, await manual resolution
                self.hid.stop_all()

            return

        elif self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            # Enemies not currently detected -> check 2.0-second clear timer
            if self.combat_clear_start == 0.0:
                self.combat_clear_start = now

            if now - self.combat_clear_start >= 2.0:
                print("[STATE] Combat resolved. Returning to Overworld Navigation.")
                self.hid.stop_all()
                self.combat_queue.clear()
                self.is_closing_distance = False
                self.current_state = STATE_NAVIGATE
                # Schedule randomized V pulse after 0.4s - 0.9s delay
                self.pending_v_pulse_time = now + random.uniform(0.4, 0.9)
            return

        # ----------------------------------------------------------------------
        # STATE 2: Dialogue Mode Evaluation (Minimap disappearance)
        # ----------------------------------------------------------------------
        minimap_present, minimap_score = self.detector.check_minimap_present(frame)

        if not minimap_present:
            # Minimap disappeared -> In cutscene / conversation
            if self.current_state != STATE_DIALOGUE:
                self.current_state = STATE_DIALOGUE
                print(f"[STATE] Entered {STATE_DIALOGUE} (Minimap hidden, metric: {minimap_score:.3f})")
                self.hid.stop_all()
                self.combat_queue.clear()

            # Check for dialogue speech bubble / choice icon
            choice_detected, choice_box = self.detector.check_choice_icon(frame)
            if choice_detected:
                if choice_box:
                    bx, by, bw, bh = choice_box
                    cv2.rectangle(debug_frame, (bx, by), (bx + bw, by + bh), (0, 255, 0), 2)
                    cv2.putText(
                        debug_frame,
                        "CHOICE ICON",
                        (bx, by - 6),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.45,
                        (0, 255, 0),
                        1,
                    )

                # Log-normal human reaction delay + 2.0s cooldown
                if now - self.last_choice_f_time >= 2.0:
                    if self.pending_f_press_time is None:
                        # Humanized delay using helper
                        reaction_delay = human_delay(mean=0.35, sigma=0.30, min_val=0.22, max_val=0.75, allow_micro_pause=False)
                        self.pending_f_press_time = now + reaction_delay

                if self.pending_f_press_time and now >= self.pending_f_press_time:
                    print("[DIALOGUE] Option selected: dispatching 'F' keypress.")
                    self.hid.press_f()
                    self.last_choice_f_time = now
                    self.pending_f_press_time = None

            self.previous_minimap_state = False
            return

        # ----------------------------------------------------------------------
        # STATE 3: Overworld Navigation
        # ----------------------------------------------------------------------
        # If transitioning from DIALOGUE -> OVERWORLD, schedule V pulse
        if not self.previous_minimap_state and minimap_present:
            print("[STATE] Transitioned back to Overworld. Scheduling 'V' quest path pulse.")
            self.pending_v_pulse_time = now + random.uniform(0.3, 0.6)

        self.previous_minimap_state = True
        self.current_state = STATE_NAVIGATE

        # Dispatch pending V pulse if ready
        if self.pending_v_pulse_time and now >= self.pending_v_pulse_time:
            self.hid.press_v()
            self.pending_v_pulse_time = None

        # 1. Ground motion calculation (stuck detection)
        raw_motion, self.prev_ground_gray = self.detector.calculate_ground_motion(frame, self.prev_ground_gray)
        self.smooth_motion_score = 0.75 * self.smooth_motion_score + 0.25 * raw_motion

        # 2. Track Gold Quest Marker
        centroid, _ = self.detector.track_gold_quest_marker(frame, self.config)

        if centroid is not None:
            self.marker_last_seen_time = now
            self.is_stuck_alert = False
            cx, cy = centroid

            # Draw centroid and crosshair
            cv2.circle(debug_frame, (cx, cy), 8, (0, 215, 255), -1)
            cv2.line(debug_frame, (cx - 15, cy), (cx + 15, cy), (0, 215, 255), 2)
            cv2.line(debug_frame, (cx, cy - 15), (cx, cy + 15), (0, 215, 255), 2)

            # Steering calculation: error_x relative to screen center (640)
            error_x = cx - 640
            cv2.arrowedLine(debug_frame, (640, cy), (cx, cy), (0, 255, 255), 2, tipLength=0.2)

            # Humanized Noisy Camera Steering with 38px Deadzone
            self._apply_humanized_steering(error_x, now)

            # Check if stuck against obstacle/wall while walking forward
            walk_time = now - self.hid.walk_start_time if self.hid.is_walking else 0.0
            if self.hid.is_walking and walk_time > 2.5 and self.smooth_motion_score < 1.05:
                # Character is stuck against rock / tree / cliff
                self.hid.stop_all()
                self.is_stuck_alert = True
                play_alert_chime(880, 220)
                print(f"[STUCK] Low ground motion ({self.smooth_motion_score:.2f}) while walking. STOP sent!")
            elif not self.is_stuck_alert:
                self.hid.start_walk()

                # Variable Sprint Cadence: Trigger RMB dash at randomized intervals (2.1 - 3.8s)
                if now - self.last_sprint_time >= self.next_sprint_interval:
                    self.hid.click_rmb()
                    self.last_sprint_time = now
                    self.next_sprint_interval = random.uniform(2.1, 3.8)

                # Lightweight keepalive: re-send W1 every 4.0s if no other command was sent in that window
                if self.hid.is_walking and (now - self.hid.last_command_time >= 4.0):
                    self.hid.send_command("W1")

        else:
            # Gold quest marker not visible in central ROI
            time_lost = now - self.marker_last_seen_time
            if self.hid.is_walking and time_lost > 1.2:
                self.hid.stop_all()
                play_alert_chime(880, 220)
                print(f"[NAV] Quest marker lost for {time_lost:.1f}s. STOP sent for safety.")

    # ==========================================================================
    # Humanized Camera Steering Logic
    # ==========================================================================
    def _apply_humanized_steering(self, error_x: int, now: float):
        """
        Applies throttled, noisy camera steering with a horizontal center deadzone (38px).
        Mimics natural human wrist flicks instead of continuous mechanical updates.
        """
        # Horizontal center deadzone: within 38px, no steering adjustment
        if abs(error_x) < 38:
            return

        # Throttle steering updates to randomized intervals (0.12s - 0.28s)
        if now - self.last_steer_time < self.next_steer_interval:
            return

        kp = float(self.config["Kp_x100"]) / 100.0

        # Gaussian noise (mean 0, sigma 2.2) and slight human overshoot (0.85 - 1.15x)
        noise = random.gauss(0, 2.2)
        overshoot = random.uniform(0.85, 1.15)
        steer_dx = int((error_x * (kp * overshoot)) + noise)

        self.hid.move_mouse(steer_dx)

        self.last_steer_time = now
        self.next_steer_interval = random.uniform(0.12, 0.28)

    # ==========================================================================
    # Starter-Roster Stochastic Auto-Combat
    # ==========================================================================
    def _execute_auto_combat_step(self, now: float):
        """
        Non-blocking step execution for stochastic starter-roster combat.
        Supports 1 to 4 unlocked characters with dynamic cycle generation.
        """
        # 1. Close distance if red HP bar is small (< 280 px area)
        if self.combat_largest_area > 0 and self.combat_largest_area < 280:
            if not self.is_closing_distance:
                # Pulse forward walk for 0.35s - 0.55s
                pulse_duration = random.uniform(0.35, 0.55)
                self.hid.start_walk()
                self.is_closing_distance = True
                self.close_distance_finish_time = now + pulse_duration
                self.active_combat_action_label = "Closing Distance (W1)"
                return

        if self.is_closing_distance:
            if now >= self.close_distance_finish_time:
                self.hid.stop_walk()
                self.is_closing_distance = False
            return

        # 2. Steer camera toward target if enemy centroid is available
        if self.combat_target_centroid is not None:
            enemy_error_x = self.combat_target_centroid[0] - 640
            self._apply_humanized_steering(enemy_error_x, now)

        # 3. Generate a fresh randomized combat cycle if queue is empty
        if not self.combat_queue:
            self._generate_stochastic_combat_cycle()

        # 4. Execute the next non-blocking combat step
        if now >= self.combat_step_ready_time and self.combat_queue:
            step = self.combat_queue.pop(0)
            self.active_combat_action_label = step.get("label", "Combat Action")
            step_type = step.get("type", "CMD")

            if step_type == "SLOT":
                self.hid.select_slot(step["slot"])
            elif step_type == "SKILL":
                self.hid.press_e()
            elif step_type == "BURST":
                self.hid.press_q()
            elif step_type == "ATTACK":
                self.hid.click_lmb()
            elif step_type == "CHARGED_START":
                self.hid.hold_lmb()
            elif step_type == "CHARGED_FINISH":
                self.hid.release_lmb()
            elif step_type == "DASH":
                self.hid.click_rmb()
            elif step_type == "JUMP":
                self.hid.press_space()
            elif step_type == "STOP":
                self.hid.stop_all()

            # Schedule next step execution
            self.combat_step_ready_time = now + step.get("delay", human_delay())

    def _generate_stochastic_combat_cycle(self):
        """
        Dynamically generates a randomized starter-roster combat sequence:
        - Slot 1 (E, 40% Q, 2-5 LMB or 20% charged attack)
        - Shuffled remaining slots [2, 3, 4] (E 85%, Q 35%, 1-4 LMB)
        - 15% chance to inject a repositioning Dash (RMB) or Jump (SPACE)
        """
        # --- Part A: Slot 1 Sequence (Main DPS / Starter Traveler) ---
        self.combat_queue.append({
            "type": "SLOT",
            "slot": 1,
            "label": "Slot 1 (Select)",
            "delay": human_delay(0.24, 0.35, 0.16, 0.42, allow_micro_pause=False),
        })

        # Elemental Skill (E)
        self.combat_queue.append({
            "type": "SKILL",
            "label": "Slot 1: Skill [E]",
            "delay": human_delay(0.26, 0.35, 0.18, 0.48, allow_micro_pause=False),
        })

        # 40% probability for Elemental Burst (Q)
        if random.random() < 0.40:
            self.combat_queue.append({
                "type": "BURST",
                "label": "Slot 1: Burst [Q]",
                "delay": human_delay(0.32, 0.35, 0.22, 0.58, allow_micro_pause=False),
            })

        # 20% chance of 0.6s Charged Attack, otherwise 2-5 normal attacks
        if random.random() < 0.20:
            self.combat_queue.append({
                "type": "CHARGED_START",
                "label": "Slot 1: Charged Atk (Hold)",
                "delay": 0.60,
            })
            self.combat_queue.append({
                "type": "CHARGED_FINISH",
                "label": "Slot 1: Charged Atk (Release)",
                "delay": human_delay(0.22, 0.30, 0.15, 0.40, allow_micro_pause=False),
            })
        else:
            num_hits = random.randint(2, 5)
            for i in range(num_hits):
                self.combat_queue.append({
                    "type": "ATTACK",
                    "label": f"Slot 1: Normal Atk ({i+1}/{num_hits})",
                    "delay": human_delay(0.20, 0.30, 0.14, 0.38, allow_micro_pause=False),
                })

        # --- Part B: Shuffle Remaining Party Slots (AR 1-10 Friendly) ---
        # Select 2 to 3 slots from [2, 3, 4] in randomized order
        k_slots = random.randint(2, 3)
        shuffled_slots = random.sample([2, 3, 4], k=k_slots)

        for slot_num in shuffled_slots:
            # Slot switch
            self.combat_queue.append({
                "type": "SLOT",
                "slot": slot_num,
                "label": f"Slot {slot_num} (Select)",
                "delay": human_delay(0.28, 0.35, 0.18, 0.45, allow_micro_pause=False),
            })

            # 85% probability for Elemental Skill (E)
            if random.random() < 0.85:
                self.combat_queue.append({
                    "type": "SKILL",
                    "label": f"Slot {slot_num}: Skill [E]",
                    "delay": human_delay(0.25, 0.35, 0.18, 0.50, allow_micro_pause=False),
                })

            # 35% probability for Elemental Burst (Q)
            if random.random() < 0.35:
                self.combat_queue.append({
                    "type": "BURST",
                    "label": f"Slot {slot_num}: Burst [Q]",
                    "delay": human_delay(0.30, 0.35, 0.20, 0.55, allow_micro_pause=False),
                })

            # 1 to 4 Normal Attacks
            num_attacks = random.randint(1, 4)
            for j in range(num_attacks):
                self.combat_queue.append({
                    "type": "ATTACK",
                    "label": f"Slot {slot_num}: Atk ({j+1}/{num_attacks})",
                    "delay": human_delay(0.20, 0.30, 0.14, 0.38, allow_micro_pause=False),
                })

        # --- Part C: Natural Repositioning (15% chance per cycle) ---
        if random.random() < 0.15:
            if random.random() < 0.60:
                self.combat_queue.append({
                    "type": "DASH",
                    "label": "Reposition: Dash [RMB]",
                    "delay": human_delay(0.35, 0.35, 0.22, 0.65, allow_micro_pause=True),
                })
            else:
                self.combat_queue.append({
                    "type": "JUMP",
                    "label": "Reposition: Jump [SPACE]",
                    "delay": human_delay(0.35, 0.35, 0.22, 0.65, allow_micro_pause=True),
                })

    # ==========================================================================
    # Visual HUD & Overlay Rendering
    # ==========================================================================
    def _draw_hud(self, debug_frame: np.ndarray, fps: float, is_focused: bool):
        """Draw bounding boxes, telemetry stats, and hotkey banners on debug frame."""
        # 1. Minimap ROI (Cyan)
        my1, my2, mx1, mx2 = ROI_MINIMAP
        cv2.rectangle(debug_frame, (mx1, my1), (mx2, my2), (255, 255, 0), 1)
        cv2.putText(debug_frame, "Minimap ROI", (mx1 + 5, my2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1)

        # 2. Navigation ROI (Yellow)
        ny1, ny2, nx1, nx2 = ROI_NAV
        cv2.rectangle(debug_frame, (nx1, ny1), (nx2, ny2), (0, 255, 255), 1)
        cv2.putText(debug_frame, "Quest Track ROI", (nx1 + 5, ny1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)

        # 3. Choice ROI (Green)
        cy1, cy2, cx1, cx2 = ROI_CHOICE
        cv2.rectangle(debug_frame, (cx1, cy1), (cx2, cy2), (0, 255, 0), 1)
        cv2.putText(debug_frame, "Choice ROI", (cx1 + 5, cy1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)

        # 4. Ground Motion ROI (Magenta)
        gy1, gy2, gx1, gx2 = ROI_GROUND
        cv2.rectangle(debug_frame, (gx1, gy1), (gx2, gy2), (255, 0, 255), 1)
        cv2.putText(debug_frame, f"Ground Motion: {self.smooth_motion_score:.2f}", (gx1 + 5, gy1 + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 0, 255), 1)

        # 5. Combat HP Scan ROI (Red)
        kby1, kby2, kbx1, kbx2 = ROI_COMBAT
        combat_color = (0, 0, 255) if self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL) else (50, 50, 160)
        cv2.rectangle(debug_frame, (kbx1, kby1), (kbx2, kby2), combat_color, 1)

        # Draw enemy centroid if available
        if self.combat_target_centroid is not None and self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            ecx, ecy = self.combat_target_centroid
            cv2.drawMarker(debug_frame, (ecx, ecy), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 16, 2)
            cv2.putText(
                debug_frame,
                f"TARGET ({int(self.combat_largest_area)}px)",
                (ecx - 40, ecy - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 0, 255),
                1,
            )

        # Top Information Banner
        cv2.rectangle(debug_frame, (0, 0), (TARGET_WIDTH, 36), (20, 20, 20), -1)

        # State color badge
        state_color = (0, 255, 0)
        if self.current_state == STATE_COMBAT_AUTO:
            state_color = (0, 100, 255)
        elif self.current_state == STATE_COMBAT_MANUAL:
            state_color = (0, 0, 255)
        elif self.current_state == STATE_DIALOGUE:
            state_color = (255, 180, 0)

        cv2.putText(
            debug_frame,
            f"STATE: {self.current_state}",
            (15, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            state_color,
            2,
        )

        # Middle telemetry info
        if self.current_state == STATE_COMBAT_AUTO:
            elapsed = time.time() - self.combat_start_time
            queue_len = len(self.combat_queue)
            telemetry_text = f"Combat: {elapsed:.1f}s/45s | Action: {self.active_combat_action_label} (Q:{queue_len})"
            cv2.putText(debug_frame, telemetry_text, (290, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.48, (0, 220, 255), 1)
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
        hotkey_text = "[F12] Global Pause | [S] Save Tuner Config | [Q] Clean Exit"
        cv2.putText(
            debug_frame,
            hotkey_text,
            (TARGET_WIDTH // 2 - 270, TARGET_HEIGHT - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.55,
            (220, 220, 220),
            1,
        )

        # Config Saved notification
        if time.time() < self.save_banner_until:
            cv2.rectangle(debug_frame, (500, 50), (780, 95), (0, 140, 0), -1)
            cv2.putText(
                debug_frame,
                "CONFIG SAVED!",
                (535, 82),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (255, 255, 255),
                2,
            )

        # Stuck Alert Banner
        if self.is_stuck_alert:
            cv2.rectangle(debug_frame, (420, 120), (860, 170), (0, 0, 200), -1)
            cv2.putText(
                debug_frame,
                "OBSTACLE / STUCK DETECTED!",
                (445, 155),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.75,
                (255, 255, 255),
                2,
            )

        # Combat Manual Timeout Banner
        if self.current_state == STATE_COMBAT_MANUAL:
            cv2.rectangle(debug_frame, (320, 110), (960, 165), (0, 0, 180), -1)
            cv2.putText(
                debug_frame,
                "COMBAT TIMEOUT (>45s) - MANUAL TAKEOVER",
                (345, 145),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.65,
                (255, 255, 255),
                2,
            )

    def _draw_overlay_notice(self, frame: np.ndarray, text: str, color: Tuple[int, int, int]):
        """Draw semi-transparent overlay to indicate safe paused state."""
        overlay = frame.copy()
        cv2.rectangle(overlay, (0, 0), (TARGET_WIDTH, TARGET_HEIGHT), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)

        # Center banner
        cv2.rectangle(frame, (240, 310), (1040, 410), (20, 20, 20), -1)
        cv2.rectangle(frame, (240, 310), (1040, 410), color, 3)
        cv2.putText(
            frame,
            text,
            (TARGET_WIDTH // 2 - 280, 370),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.95,
            color,
            2,
        )

    def cleanup(self):
        """Emergency stop inputs, disconnect serial, and close OpenCV windows."""
        print("[CLEANUP] Stopping HID inputs and releasing resources...")
        self.hid.close()
        cv2.destroyAllWindows()
        print("[CLEANUP] Genshin Assistant terminated safely.")


# ==============================================================================
# CLI Entrypoint
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="Genshin Impact CV Automation with ESP32-S3 HID (v2 Auto-Combat)")
    parser.add_argument("--port", type=str, default="COM4", help="Serial COM port of ESP32-S3 (default: COM4)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run screen capture, state machine, and tuner without requiring physical ESP32-S3 connected",
    )
    args = parser.parse_args()

    assistant = GenshinAssistant(port=args.port, dry_run=args.dry_run)
    assistant.run()


if __name__ == "__main__":
    main()
