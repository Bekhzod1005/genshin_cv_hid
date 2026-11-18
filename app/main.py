#!/usr/bin/env python3
"""
Genshin Impact Hardware-Assisted Computer Vision Automation (v5 Modular Production)
=================================================================================
Architecture:
- Screen Capture: mss (direct desktop / client-rect capture normalized to 1280x720)
- Hardware Input: ESP32-S3 Microcontroller running TinyUSB HID Keyboard & Mouse
  with non-blocking millis() scheduler, 512-byte RX buffer, micro-stepped mouse steering,
  and decoupled hold states.
- Two-Zone PD Camera Steering & Pivot-in-Place:
    - 28 Hz update rate, no pre-PD EMA lag
    - Zone 1: Deadzone (|error_x| <= 24 px) -> dx = 0, hard center lock
    - Zone 2: Fine Tracking (24 < |error_x| <= 130 px) -> Kp=0.26, Kd=0.09
    - Zone 3: Fast Turn & Pivot (|error_x| > 130 px or minimap bearing > 38 deg) -> Kp=0.44, Kd=0.10
    - Pivot-in-Place: W0 pauses walking whenever |error_x| > 115 px or turning towards minimap bearing
- Strict State Hierarchy:
    1. DIALOGUE      : Minimap hidden -> Strict top priority, advances dialogue & multi-choice.
    2. COMBAT_AUTO   : Strict Horizontal HP-Bar Geometry Filtering inside x: 160..1120, y: 120..560:
                       - Crimson red (H: 0..5 / 175..180, S: 185..255, V: 175..255)
                       - Width: 34..140 px, Height: 3..10 px, Aspect Ratio >= 5.5, Extent >= 0.65
                       - Non-blocking Amber bow aim-shot state machine (active 2-axis tracking during charge).
    3. CHEST_LOOT    : Hollow-ring geometry (fill extent 0.12..0.48, 4-frame confirmation) & loot vacuum.
    4. NAVIGATE      : Pure Template-Based Multi-Scale Quest Tracking:
                       - Auto-loads user PNG templates (assets/quest_gold.png & assets/quest_blue.png)
                       - 120x120 local tracking on every frame with immediate full-ROI fallback
                       - Throttled full scan (every 6th frame) when off-screen to preserve >60 FPS
                       - Concentric contrast verification (core V - border V >= 25)
                       - Instant directional turning via minimap quest bearing (Gold & Blue)
                       - Staged obstacle unstuck recovery (0.8s hop -> 1.7s lateral turn + hop)
- Safety & Hotkeys:
    - [F12] : Global Pause / Resume Toggle
    - [T]   : Capture 28x28 Diamond Reference Template (or Left-Click on Debug Window)
    - [D]   : Reset Configuration to Hardcoded Defaults
    - [S]   : Save active settings to config.json
    - [Q]   : Clean Shutdown & Emergency Key Release
    - Safe OpenCV window lifecycle (guards against 'X' close crashes)
    - Active Window Focus Lock (win32gui.GetForegroundWindow() == hwnd)
    - 12-Second Safety Watchdog in Microcontroller Firmware + 4.0s keepalive
"""

import os
import sys

# Ensure project root is in sys.path so 'app.xxx' imports resolve whether run directly or as a package
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import time
import argparse
from typing import Optional, Tuple, Dict, Any

import cv2
import numpy as np
import mss
from pynput import keyboard

from app.config_manager import (
    ASSETS_DIR,
    TARGET_WIDTH,
    TARGET_HEIGHT,
    HALF_WIDTH,
    HALF_HEIGHT,
    ROI_MINIMAP,
    ROI_NAV,
    ROI_CHOICE,
    ROI_COMBAT,
    ROI_INTERACT,
    ROI_PARTY,
    ROI_EXCLUSION_STAMINA,
    ROI_GROUND_HALF,
    MINIMAP_CENTER,
    MINIMAP_RADIUS,
    STATE_DIALOGUE,
    STATE_COMBAT_AUTO,
    STATE_COMBAT_MANUAL,
    STATE_CHEST_LOOT,
    STATE_NAVIGATE,
    HARDCODED_DEFAULTS,
    load_config,
    save_config,
    setup_dpi_awareness,
    HAS_WIN32,
)

if HAS_WIN32:
    import win32gui

from app.controller import ESP32HIDController
from app.vision import VisionDetector
from app.state_machine import AssistantStateMachine


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


class GenshinAssistantApp:
    def __init__(self, port: str = "COM4", dry_run: bool = False, capture_icon_mode: Optional[str] = None):
        self.port = port
        self.dry_run = dry_run
        self.capture_icon_mode = capture_icon_mode

        self.config = load_config()
        self.hid = ESP32HIDController(port=self.port, dry_run=self.dry_run)
        self.detector = VisionDetector()
        self.sm = AssistantStateMachine(self.hid, self.detector, self.config)

        # Global Pause State
        self.global_paused = False
        self.setup_global_hotkeys()

        # UI & Rendering
        self.last_render_time = 0.0
        self.latest_frame: Optional[np.ndarray] = None
        self.save_banner_until = 0.0
        self.reset_banner_until = 0.0
        self.template_banner_until = 0.0
        self.template_banner_text = ""

    def setup_global_hotkeys(self):
        """Bind global F12 key to toggle pause instantly from any window."""
        def on_press(key):
            if key == keyboard.Key.f12:
                self.global_paused = not self.global_paused
                status = "PAUSED" if self.global_paused else "RESUMED"
                print(f"\n[HOTKEY F12] Automation {status} by user.")
                if self.global_paused:
                    self.hid.stop_all()
                    self.sm.combat_consecutive_frames = 0
                    self.sm.sealed_chest_consecutive_frames = 0
                    self.sm.f_prompt_consecutive_frames = 0
                    self.sm.vacuum_active = False
                    self.sm.vacuum_phase = "IDLE"
                    self.sm.is_scanning_for_marker = False
                    if self.sm.is_aiming_bow:
                        self.hid.press_r()
                        self.sm.is_aiming_bow = False
                        self.sm.bow_substate = "AIM_IDLE"

        listener = keyboard.Listener(on_press=on_press)
        listener.daemon = True
        listener.start()
        print("[HOTKEY] Registered global 'F12' instant toggle pause listener.")

    def reset_config_to_defaults(self):
        """Reset configuration back to HARDCODED_DEFAULTS and save to config.json."""
        self.config = HARDCODED_DEFAULTS.copy()
        save_config(self.config)
        self.sm.update_config(self.config)
        self.reset_banner_until = time.time() + 2.5
        print("[CONFIG] Configuration reset to hardcoded defaults.")

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
                center_bgr = patch[14, 14]
                center_hsv = cv2.cvtColor(np.uint8([[center_bgr]]), cv2.COLOR_BGR2HSV)[0][0]
                if 85 <= center_hsv[0] <= 126:
                    target_name = "quest_blue.png"
                    mtype = "BLUE"
                else:
                    target_name = "quest_gold.png"
                    mtype = "GOLD"

                out_path = os.path.join(ASSETS_DIR, target_name)
                try:
                    cv2.imwrite(out_path, patch)
                    self.detector.reload_templates()
                    self.template_banner_text = f"SAVED {mtype} TEMPLATE ({target_name}) FROM CLICK!"
                    self.template_banner_until = time.time() + 2.5
                    print(f"[TEMPLATE] Saved 28x28 {mtype} template from click at ({cx}, {cy}) to {out_path}")
                except Exception as e:
                    print(f"[TEMPLATE ERROR] Could not save template: {e}")

    def capture_active_diamond_template(self):
        """Crop 28x28 patch around currently tracked diamond and save to assets (triggered by 'T')."""
        if self.latest_frame is not None and self.sm.active_marker_centroid is not None:
            cx, cy = self.sm.active_marker_centroid
            py1 = max(0, cy - 14)
            py2 = min(TARGET_HEIGHT, cy + 14)
            px1 = max(0, cx - 14)
            px2 = min(TARGET_WIDTH, cx + 14)
            patch = self.latest_frame[py1:py2, px1:px2]

            if patch.shape[0] == 28 and patch.shape[1] == 28:
                mtype = self.sm.active_marker_type or "GOLD"
                target_name = "quest_gold.png" if mtype == "GOLD" else "quest_blue.png"
                out_path = os.path.join(ASSETS_DIR, target_name)
                try:
                    cv2.imwrite(out_path, patch)
                    self.detector.reload_templates()
                    self.template_banner_text = f"SAVED {mtype} TEMPLATE ({target_name})!"
                    self.template_banner_until = time.time() + 2.5
                    print(f"[TEMPLATE] Hotkey 'T' saved {mtype} template at ({cx}, {cy}) to {out_path}")
                except Exception as e:
                    print(f"[TEMPLATE ERROR] Could not save template: {e}")
        else:
            print("[TEMPLATE] No candidate quest diamond tracked right now. Left-click on the Debug window to crop!")

    def run(self):
        setup_dpi_awareness()

        cv2.namedWindow("Genshin Assistant Debug", cv2.WINDOW_AUTOSIZE)
        cv2.setMouseCallback("Genshin Assistant Debug", self._on_debug_mouse_click)

        fps_counter = 0
        fps_start_time = time.time()
        current_fps = 0.0
        frame_idx = 0

        print("\n" + "=" * 70)
        print(" Genshin Impact CV Assistant Running (v5 Modular Production)")
        print(" Hierarchy: DIALOGUE > COMBAT_AUTO > CHEST_LOOT > NAVIGATE")
        print(" Steering : Two-Zone PD (28 Hz) + Off-Screen Minimap Pivot-in-Place")
        print(" Combat   : Strict HP Bar Geometry + Non-Blocking Amber Aim Tracker")
        print(" Quests   : Pure Template Tracking + Concentric Contrast Verification")
        print(" Unstuck  : Staged Non-Blocking Recovery (0.8s hop, 1.7s turn+hop)")
        print(" Hotkeys:")
        print("   [F12] : Instant Global Pause / Resume Toggle")
        print("   [T]   : Crop & Save 28x28 Diamond Template (or Left-Click Screen)")
        print("   [D]   : Reset Configuration to Hardcoded Defaults")
        print("   [S]   : Save active settings to config.json")
        print("   [Q]   : Clean Shutdown & Emergency Key Release")
        print("=" * 70 + "\n")

        mss_factory = getattr(mss, "MSS", mss.mss)
        with mss_factory() as sct:
            try:
                while True:
                    frame_idx += 1
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
                    if not is_focused or self.global_paused:
                        self.hid.stop_all()
                        self.sm.combat_consecutive_frames = 0
                        self.sm.sealed_chest_consecutive_frames = 0
                        self.sm.f_prompt_consecutive_frames = 0
                        self.sm.vacuum_active = False
                        self.sm.vacuum_phase = "IDLE"
                        if self.sm.is_aiming_bow:
                            self.hid.press_r()
                            self.sm.is_aiming_bow = False
                            self.sm.bow_substate = "AIM_IDLE"
                    else:
                        # 4. Execute State Machine
                        self.sm.process_frame(frame, frame_half, frame_idx)

                    # 5. Calculate Internal Loop Processing FPS
                    fps_counter += 1
                    if now - fps_start_time >= 1.0:
                        current_fps = fps_counter / (now - fps_start_time)
                        fps_counter = 0
                        fps_start_time = now

                    # 6. Throttle Debug Window Rendering to max 20 FPS (~50ms)
                    if now - self.last_render_time >= 0.05:
                        self.last_render_time = now

                        # Safe Window Lifecycle Check: detect if user clicked the 'X' button
                        try:
                            prop = cv2.getWindowProperty("Genshin Assistant Debug", cv2.WND_PROP_VISIBLE)
                            if prop < 1:
                                print("\n[INFO] Debug window closed by user ('X' button clicked). Exiting...")
                                break
                        except cv2.error:
                            print("\n[INFO] Debug window destroyed. Exiting...")
                            break

                        debug_frame = frame.copy()
                        if not is_focused:
                            self._draw_overlay_notice(debug_frame, "PAUSED (Window Unfocused)", (0, 0, 255))
                        elif self.global_paused:
                            self._draw_overlay_notice(debug_frame, "PAUSED (User F12 Toggle)", (0, 165, 255))
                        else:
                            self._draw_hud(debug_frame, current_fps, is_focused)

                        try:
                            cv2.imshow("Genshin Assistant Debug", debug_frame)
                        except cv2.error as e:
                            print(f"[WARN] cv2.imshow error: {e}")
                            break

                        # Handle Hotkeys safely
                        try:
                            key = cv2.waitKey(1) & 0xFF
                        except cv2.error:
                            break

                        if key in (ord("q"), ord("Q"), 27):
                            print("\n[INFO] Clean exit requested by user.")
                            break
                        elif key in (ord("s"), ord("S")):
                            if save_config(self.config):
                                self.save_banner_until = time.time() + 2.0
                        elif key in (ord("d"), ord("D")):
                            self.reset_config_to_defaults()
                        elif key in (ord("t"), ord("T")):
                            self.capture_active_diamond_template()

            finally:
                self.cleanup()

    def _draw_hud(self, debug_frame: np.ndarray, fps: float, is_focused: bool):
        """Draw bounding boxes, telemetry stats, reticles, and hotkey banners on debug frame."""
        # 1. Minimap Circle ROI (Cyan)
        cv2.circle(debug_frame, MINIMAP_CENTER, MINIMAP_RADIUS, (255, 255, 0), 1)
        road_tag = "Road: ON" if self.sm.active_road_following else "Road: OFF"
        cv2.putText(
            debug_frame,
            road_tag,
            (MINIMAP_CENTER[0] - 28, MINIMAP_CENTER[1] + 82),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.38,
            (255, 255, 0),
            1,
        )

        # 2. Navigation ROI (Yellow)
        ny1, ny2, nx1, nx2 = ROI_NAV
        cv2.rectangle(debug_frame, (nx1, ny1), (nx2, ny2), (0, 255, 255), 1)

        # 3. Stamina / Character Exclusion Zone (Gray dashed outline)
        ey1, ey2, ex1, ex2 = ROI_EXCLUSION_STAMINA
        cv2.rectangle(debug_frame, (ex1, ey1), (ex2, ey2), (90, 90, 90), 1)
        cv2.putText(
            debug_frame,
            "Stamina Exclusion Zone",
            (ex1 + 6, ey1 + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.40,
            (120, 120, 120),
            1,
        )

        # 4. Choice ROI (Green)
        cy1, cy2, cx1, cx2 = ROI_CHOICE
        cv2.rectangle(debug_frame, (cx1, cy1), (cx2, cy2), (0, 255, 0), 1)
        if self.sm.has_active_choice and self.sm.choice_box:
            cbx, cby, cbw, cbh = self.sm.choice_box
            cv2.rectangle(debug_frame, (cbx, cby), (cbx + cbw, cby + cbh), (0, 255, 0), 2)
            cv2.putText(debug_frame, "DIALOGUE CHOICE", (cbx, cby - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 0), 1)

        # 5. Ground Motion ROI (Magenta in 1280x720)
        gy1, gy2, gx1, gx2 = (
            ROI_GROUND_HALF[0] * 2,
            ROI_GROUND_HALF[1] * 2,
            ROI_GROUND_HALF[2] * 2,
            ROI_GROUND_HALF[3] * 2,
        )
        cv2.rectangle(debug_frame, (gx1, gy1), (gx2, gy2), (255, 0, 255), 1)
        cv2.putText(
            debug_frame,
            f"Motion: {self.sm.smooth_motion_score:.2f}",
            (gx1 + 5, gy1 + 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (255, 0, 255),
            1,
        )

        # 6. Combat HP Scan ROI (Red - Strict Geometry Bounds)
        kby1, kby2, kbx1, kbx2 = ROI_COMBAT
        combat_color = (0, 0, 255) if self.sm.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL) else (60, 60, 160)
        cv2.rectangle(debug_frame, (kbx1, kby1), (kbx2, kby2), combat_color, 1)

        # 7. Interact Prompt ROI (Teal)
        iy1, iy2, ix1, ix2 = ROI_INTERACT
        cv2.rectangle(debug_frame, (ix1, iy1), (ix2, iy2), (180, 180, 0), 1)

        # 8. Party Portrait Stack ROI
        py1, py2, px1, px2 = ROI_PARTY
        cv2.rectangle(debug_frame, (px1, py1), (px2, py2), (140, 100, 220), 1)
        cv2.putText(
            debug_frame,
            f"Party: {self.sm.detected_party_size} Slots",
            (px1 - 25, py1 - 6),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.42,
            (140, 100, 220),
            1,
        )

        # 9. Draw Active Quest Reticle (Gold or Blue) & Crosshair (Bug 9 Fix!)
        if self.sm.active_marker_centroid is not None and self.sm.current_state == STATE_NAVIGATE:
            mcx, mcy = self.sm.active_marker_centroid
            raw_err = mcx - 640
            deadzone = int(self.config.get("center_lock_deadzone", 24))
            is_locked = abs(raw_err) <= deadzone

            if self.sm.active_marker_type == "BLUE":
                reticle_color = (255, 200, 0)
                type_tag = "BLUE"
            else:
                reticle_color = (0, 215, 255)
                type_tag = "GOLD"

            reticle_label = f"TMPL [{type_tag}] {self.sm.active_marker_score:.2f} | {self.sm.active_scan_mode}"

            if is_locked:
                reticle_label += " [LOCKED]"
                cv2.rectangle(debug_frame, (mcx - 18, mcy - 18), (mcx + 18, mcy + 18), (0, 255, 0), 2)
            else:
                cv2.circle(debug_frame, (mcx, mcy), 10, reticle_color, 2)

            # Horizontal crosshair line
            cv2.line(debug_frame, (mcx - 16, mcy), (mcx + 16, mcy), reticle_color, 2)
            # Vertical crosshair line (Fixed Bug 9 Typo!)
            cv2.line(debug_frame, (mcx, mcy - 16), (mcx, mcy + 16), reticle_color, 2)
            cv2.putText(debug_frame, reticle_label, (mcx - 65, mcy - 22), cv2.FONT_HERSHEY_SIMPLEX, 0.46, reticle_color, 2)

        # Draw Detected Enemy HP Bar Box (if present)
        if self.sm.detected_hp_bar_box:
            hbx, hby, hbw, hbh = self.sm.detected_hp_bar_box
            cv2.rectangle(debug_frame, (hbx, hby), (hbx + hbw, hby + hbh), (0, 0, 255), 2)
            cv2.putText(debug_frame, f"HP BAR ({hbw}x{hbh})", (hbx, hby - 5), cv2.FONT_HERSHEY_SIMPLEX, 0.40, (0, 0, 255), 1)

        # Draw Overworld [F] Prompt Box (Cyan)
        if (time.time() < self.sm.show_overworld_f_box_until or self.sm.f_prompt_consecutive_frames >= 2) and self.sm.f_prompt_box:
            bx, by, bw, bh = self.sm.f_prompt_box
            cv2.rectangle(debug_frame, (bx, by), (bx + bw, by + bh), (255, 255, 0), 2)
            cv2.putText(debug_frame, "OVERWORLD [F]", (bx - 10, by - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 0), 2)

        # Draw Sealed Chest Ring Box if confirmed
        if self.sm.active_chest_box:
            bx, by, bw, bh = self.sm.active_chest_box
            cv2.rectangle(debug_frame, (bx, by), (bx + bw, by + bh), (0, 165, 255), 2)
            cv2.putText(debug_frame, "SEALED CHEST RING", (bx, by - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 165, 255), 2)

        # Draw enemy centroid if in combat
        if self.sm.combat_target_centroid is not None and self.sm.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            ecx, ecy = self.sm.combat_target_centroid
            cv2.drawMarker(debug_frame, (ecx, ecy), (0, 0, 255), cv2.MARKER_TILTED_CROSS, 16, 2)
            elevated_tag = " [ELEVATED]" if ecy < 290 else ""
            cv2.putText(
                debug_frame,
                f"TARGET ({int(self.sm.combat_largest_area)}px){elevated_tag}",
                (ecx - 40, ecy - 12),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.45,
                (0, 0, 255),
                1,
            )

        # Top Information Banner
        cv2.rectangle(debug_frame, (0, 0), (TARGET_WIDTH, 36), (20, 20, 20), -1)

        state_color = (0, 255, 0)
        if self.sm.current_state == STATE_COMBAT_AUTO:
            state_color = (0, 100, 255)
        elif self.sm.current_state == STATE_COMBAT_MANUAL:
            state_color = (0, 0, 255)
        elif self.sm.current_state == STATE_DIALOGUE:
            state_color = (255, 180, 0)
        elif self.sm.current_state == STATE_CHEST_LOOT:
            state_color = (0, 165, 255)

        cv2.putText(
            debug_frame,
            f"STATE: {self.sm.current_state}",
            (15, 24),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.62,
            state_color,
            2,
        )

        # Telemetry info
        if self.sm.current_state == STATE_COMBAT_AUTO:
            elapsed = time.time() - self.sm.combat_start_time
            aim_tag = " [AIM-SHOT]" if self.sm.is_aiming_bow else ""
            telemetry_text = f"Combat: {elapsed:.1f}s/45s | Slot {self.sm.current_party_slot}/{self.sm.detected_party_size} | {self.sm.active_combat_action_label}{aim_tag}"
            cv2.putText(debug_frame, telemetry_text, (270, 24), cv2.FONT_HERSHEY_SIMPLEX, 0.46, (0, 220, 255), 1)
        elif self.sm.current_state == STATE_CHEST_LOOT:
            if self.sm.vacuum_active:
                vac_text = f"VACUUM: {self.sm.vacuum_phase} | Taps: {self.sm.vacuum_taps_count}"
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
        hotkey_text = "[F12] Pause | [T] Capture Template | [D] Reset Defaults | [S] Save | [Q] Exit"
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

        # Reset Defaults notification
        if time.time() < self.reset_banner_until:
            cv2.rectangle(debug_frame, (430, 50), (850, 95), (0, 100, 220), -1)
            cv2.putText(debug_frame, "CONFIG RESET TO DEFAULTS!", (450, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 255), 2)

        # Template Captured notification
        if time.time() < self.template_banner_until:
            cv2.rectangle(debug_frame, (380, 50), (900, 95), (140, 80, 0), -1)
            cv2.putText(debug_frame, self.template_banner_text, (400, 82), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)

        # Off-Screen Quest Marker Scan Banner
        if self.sm.is_scanning_for_marker and self.sm.current_state == STATE_NAVIGATE:
            cv2.rectangle(debug_frame, (430, 80), (850, 118), (0, 120, 255), -1)
            cv2.putText(debug_frame, "SCANNING FOR QUEST MARKER...", (450, 107), cv2.FONT_HERSHEY_SIMPLEX, 0.60, (255, 255, 255), 2)

        # Combat Manual Timeout Banner
        if self.sm.current_state == STATE_COMBAT_MANUAL:
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
        if self.sm.is_aiming_bow:
            self.hid.press_r()
            self.sm.is_aiming_bow = False
            self.sm.bow_substate = "AIM_IDLE"
        self.hid.close()
        try:
            cv2.destroyAllWindows()
        except Exception:
            pass
        print("[CLEANUP] Genshin Assistant terminated safely.")


def main():
    parser = argparse.ArgumentParser(description="Genshin Impact CV Automation with ESP32-S3 HID (v5 Modular Production)")
    parser.add_argument("--port", type=str, default="COM4", help="Serial COM port of ESP32-S3 (default: COM4)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run screen capture, state machine, and template tracker without requiring physical ESP32-S3 connected",
    )
    parser.add_argument(
        "--capture-icon",
        type=str,
        choices=["gold", "blue"],
        default=None,
        help="Run in diamond icon capture mode: specify 'gold' or 'blue'",
    )
    args = parser.parse_args()

    app = GenshinAssistantApp(port=args.port, dry_run=args.dry_run, capture_icon_mode=args.capture_icon)
    app.run()


if __name__ == "__main__":
    main()
