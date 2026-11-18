#!/usr/bin/env python3
"""
Hardware HID Controller & Two-Zone PD Camera Steering for Genshin Impact (v5)
"""

import time
import math
import random
import threading
from typing import Optional, Tuple, Dict, Any
import serial


class ESP32HIDController:
    """Manages serial protocol communication with the ESP32-S3 TinyUSB HID firmware."""

    def __init__(self, port: str = "COM4", baud: int = 115200, dry_run: bool = False, logger: Optional[Any] = None):
        self.port = port
        self.baud = baud
        self.dry_run = dry_run
        self.logger = logger
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
            time.sleep(1.8)
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

        if self.logger and cmd != "PING":
            extra = ""
            if cmd == "W1":
                extra = " [start walk]"
            elif cmd == "W0":
                extra = " [stop walk]"
            elif cmd == "STOP":
                extra = " [emergency stop all]"
            elif cmd == "LMB_HOLD":
                extra = " [hold lmb]"
            elif cmd == "LMB_REL":
                extra = " [release lmb]"
            elif cmd.startswith("M"):
                extra = " [camera move]"
            self.logger.log_event("HID", f"TX -> {cmd}{extra}")

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
        """Release forward walk ('W0') decoupled from mouse button holds."""
        if self.is_walking:
            self.send_command("W0")
            self.is_walking = False
            self.walk_start_time = 0.0

    def stop_all(self):
        """Emergency stop: release all keyboard keys, mouse buttons, and schedules ('STOP')."""
        self.send_command("STOP")
        self.is_walking = False
        self.is_lmb_held = False
        self.walk_start_time = 0.0

    def move_mouse(self, dx: int, dy: int = 0):
        """Move mouse horizontally and/or vertically (clamped to [-110, 110])."""
        clamped_dx = max(-110, min(110, int(dx)))
        clamped_dy = max(-110, min(110, int(dy)))
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


class PDSteeringController:
    """
    Two-Zone PD Camera Steering & Pivot-in-Place Controller.
    - Operates at 28 Hz (0.035s rate limit) without pre-PD EMA lag.
    - Zone 1: Center Deadzone (|error_x| <= 24 px) -> dx = 0, hard center lock.
    - Zone 2: Fine PD Tracking (24 < |error_x| <= 130 px) -> Kp_fine = 0.26, Kd = 0.09, clamped [-45, 45].
    - Zone 3: Fast Turn & Pivot-in-Place (|error_x| > 130 px) -> Kp_fast = 0.44, Kd = 0.10, clamped [-95, 95].
    - Walking Pause: Immediately stops walking (W0) when |error_x| > 115 px or turning towards minimap bearing.
    """

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.prev_error_x: Optional[float] = None
        self.filtered_derivative: float = 0.0
        self.last_packet_time: float = 0.0

    def compute_steering(self, raw_error_x: float, now: float) -> Tuple[int, bool]:
        """
        Calculate mouse steering delta and whether walking should be paused.
        Returns: (dx, should_pause_walk)
        """
        deadzone = float(self.config.get("center_lock_deadzone", 24))
        pivot_thresh = float(self.config.get("pivot_error_threshold", 115))

        abs_err = abs(raw_error_x)

        # Zone 1: Center Deadzone
        if abs_err <= deadzone:
            self.prev_error_x = 0.0
            self.filtered_derivative = 0.0
            return 0, False

        # Rate limiter: default 28 Hz (0.035s)
        rate_hz = float(self.config.get("steer_rate_hz", 28))
        min_interval = 1.0 / max(10.0, rate_hz)
        if now - self.last_packet_time < min_interval:
            # Maintain walking pause state if error is large
            should_pause = abs_err > pivot_thresh
            return 0, should_pause

        # Derivative calculation with light smoothing (alpha = 0.75 on derivative only)
        if self.prev_error_x is None:
            raw_d = 0.0
        else:
            raw_d = raw_error_x - self.prev_error_x

        self.filtered_derivative = 0.75 * self.filtered_derivative + 0.25 * raw_d
        self.prev_error_x = raw_error_x

        kd = float(self.config.get("Kd", 0.09))
        d_term = kd * self.filtered_derivative

        # Humanized slight noise & overshoot
        noise = random.gauss(0, 1.2)
        overshoot = random.uniform(0.92, 1.08)

        if abs_err <= 130:
            # Zone 2: Fine PD Tracking
            kp = float(self.config.get("Kp_fine", 0.26))
            raw_dx = (kp * raw_error_x * overshoot) + d_term + noise
            dx = max(-45, min(45, int(raw_dx)))
            should_pause_walk = abs_err > pivot_thresh
        else:
            # Zone 3: Fast Turn & Pivot-in-Place
            kp = float(self.config.get("Kp_fast", 0.44))
            raw_dx = (kp * raw_error_x * overshoot) + d_term + noise
            dx = max(-95, min(95, int(raw_dx)))
            should_pause_walk = True

        self.last_packet_time = now
        return dx, should_pause_walk

    def compute_minimap_pivot(self, quest_bearing_rad: float, now: float) -> Tuple[int, bool]:
        """
        When 3D marker is off-screen, instantly steer directly towards the minimap quest bearing.
        Returns: (dx, should_pause_walk)
        """
        rate_hz = float(self.config.get("steer_rate_hz", 28))
        min_interval = 1.0 / max(10.0, rate_hz)
        if now - self.last_packet_time < min_interval:
            return 0, True

        bearing_deg = math.degrees(quest_bearing_rad)
        abs_deg = abs(bearing_deg)

        if abs_deg <= 8.0:
            return 0, False

        # Sharp turn towards off-screen bearing
        speed_magnitude = min(95, max(36, int(abs_deg * 0.95)))
        dx = int(math.copysign(speed_magnitude, bearing_deg))

        should_pause_walk = abs_deg > 25.0
        self.last_packet_time = now
        return dx, should_pause_walk
