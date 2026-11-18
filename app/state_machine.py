#!/usr/bin/env python3
"""
Deterministic State Machine for Genshin Impact Assistant (v5)
Enforces strict hierarchy:
1. DIALOGUE (Top Priority)
2. COMBAT_AUTO / COMBAT_MANUAL (Strict HP Bar Geometry)
3. CHEST_LOOT (Hollow Ring & Vacuum)
4. NAVIGATE (Pure Template Matching + Minimap Turn + Staged Unstuck)
"""

import time
import math
import random
from typing import Optional, Tuple, Dict, Any

import numpy as np

from app.config_manager import (
    STATE_DIALOGUE,
    STATE_COMBAT_AUTO,
    STATE_COMBAT_MANUAL,
    STATE_CHEST_LOOT,
    STATE_NAVIGATE,
    dialogue_advance_delay,
    play_alert_chime,
    save_config,
)
from app.controller import ESP32HIDController, PDSteeringController
from app.vision import VisionDetector


class AssistantStateMachine:
    def __init__(self, hid: ESP32HIDController, detector: VisionDetector, config: Dict[str, Any], logger: Optional[Any] = None):
        self.hid = hid
        self.detector = detector
        self.config = config
        self.logger = logger
        self.steering = PDSteeringController(config)

        # Active State
        self.current_state = STATE_NAVIGATE
        self.previous_minimap_state = True
        self.detected_party_size: int = 1

        # Marker Tracking & Locked Target Info
        self.marker_last_seen_time = time.time()
        self.last_valid_marker_x: Optional[int] = None
        self.active_marker_type: Optional[str] = None
        self.active_marker_centroid: Optional[Tuple[int, int]] = None
        self.active_marker_bbox: Optional[Tuple[int, int, int, int]] = None
        self.active_marker_score: float = 0.0
        self.active_scan_mode: str = "None"
        self.last_template_lock: Optional[Dict[str, Any]] = None

        # Flight Recorder Telemetry & Vision State
        self.last_telem_time: float = 0.0
        self.prev_marker_visible: bool = False
        self.prev_minimap_bearing_visible: bool = False

        # Navigation & Minimap Bearing
        self.active_road_following: bool = False
        self.minimap_quest_bearing: Optional[float] = None
        self.pending_v_pulse_time: Optional[float] = None
        self.scan_sweep_direction: int = 1
        self.last_scan_sweep_time: float = 0.0
        self.next_scan_sweep_interval: float = 0.22
        self.is_scanning_for_marker: bool = False
        self.last_quest_v_pulse_time: float = 0.0
        self.lost_marker_phase: str = "IDLE"  # IDLE, WAIT_INITIAL, WAIT_V_PULSE, STEP_SWEEP, SWEEP_COMPLETE
        self.lost_marker_start_time: float = 0.0
        self.lost_marker_step_count: int = 0
        self.last_step_sweep_time: float = 0.0
        self.last_sprint_time: float = time.time()
        self.next_sprint_interval: float = random.uniform(2.2, 3.8)

        # Camera-Locked Sprint with Hysteresis Drift Corridor
        self.nav_mode: str = "ALIGN"  # "ALIGN" (stationary centering) or "CRUISE_LOCKED" (forward sprint, dx=0)
        self.cruise_source: str = "3D"  # "3D" or "MINIMAP" or "RADAR"
        self.align_pitch_steps: int = 0  # Number of upward pitch steps taken in current ALIGN cycle (max 3)
        self.needs_pitch_reset: bool = True  # Automatic upward camera pitch reset on entering navigation

        # 2D Minimap Radar & Camera Yaw Calibration
        self.counts_per_degree: float = float(self.config.get("counts_per_degree", 0.65))
        self.yaw_calibrated: bool = bool(self.config.get("yaw_calibrated", False))
        self.calibration_phase: str = "IDLE"  # IDLE, START, WAIT_SETTLE
        self.calib_yaw_0: Optional[float] = None
        self.calib_pulse_sent_time: float = 0.0
        self.next_turn_ready_time: float = 0.0
        self.radar_target_bearing_deg: Optional[float] = None
        self.radar_trail_bearing_deg: Optional[float] = None
        self.radar_dist_px: Optional[float] = None
        self.radar_source: str = "NONE"

        # Staged Obstacle Unstuck State Machine (0.8s hop, 1.7s lateral turn + hop)
        self.prev_ground_gray_half: Optional[np.ndarray] = None
        self.smooth_motion_score: float = 10.0
        self.unstuck_still_start_time: float = 0.0
        self.unstuck_stage: int = 0  # 0: idle, 1: pulsed hop, 2: lateral evasion

        # Overworld [F] Prompt & Loot Vacuum
        self.f_prompt_consecutive_frames: int = 0
        self.f_prompt_box: Optional[Tuple[int, int, int, int]] = None
        self.last_overworld_f_time: float = 0.0
        self.overworld_f_cooldown: float = 1.4
        self.show_overworld_f_box_until: float = 0.0
        self.sealed_chest_consecutive_frames: int = 0
        self.active_chest_box: Optional[Tuple[int, int, int, int]] = None

        # Loot Vacuum Sequence
        self.vacuum_active: bool = False
        self.vacuum_phase: str = "IDLE"
        self.vacuum_animation_end_time: float = 0.0
        self.vacuum_burst_start_time: float = 0.0
        self.vacuum_burst_end_time: float = 0.0
        self.next_vacuum_f_time: float = 0.0
        self.vacuum_taps_count: int = 0
        self.vacuum_wiggle_stage: int = 0
        self.vacuum_drain_timeout: float = 0.0

        # Dialogue
        self.next_dialogue_f_time: float = 0.0
        self.has_active_choice: bool = False
        self.choice_box: Optional[Tuple[int, int, int, int]] = None

        # Combat (Strict HP Bar Geometry)
        self.combat_consecutive_frames: int = 0
        self.detected_hp_bar_box: Optional[Tuple[int, int, int, int]] = None
        self.combat_start_time: float = 0.0
        self.combat_clear_start: float = 0.0
        self.combat_step_ready_time: float = 0.0
        self.active_combat_action_label: str = "Idle"
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
        self.combat_ended_time: float = 0.0

        # Non-Blocking Amber Aim State Machine
        self.is_aiming_bow: bool = False
        self.bow_aim_shot_count: int = 0
        self.bow_substate: str = "AIM_IDLE"  # AIM_SWAP_WAIT, AIM_ENTER_R, AIM_TRACK_AND_CHARGE, AIM_RELEASE
        self.bow_substate_ready_time: float = 0.0
        self.bow_charge_start_time: float = 0.0
        self.last_aim_mouse_time: float = 0.0

    def update_config(self, new_config: Dict[str, Any]):
        """Update active configuration parameters dynamically."""
        self.config = new_config
        self.steering.config = new_config

    def _maybe_emit_telemetry(self, now: float, minimap_present: bool, minimap_score: float, f_prompt: bool):
        """Emits a 4 Hz compact telemetry snapshot while focused and unpaused."""
        if not self.logger:
            return
        if now - self.last_telem_time < 0.25:
            return
        self.last_telem_time = now

        if self.active_marker_centroid is not None:
            err_x = self.active_marker_centroid[0] - 640
            marker_str = f"{self.active_marker_type}({self.active_marker_score:.2f}, {self.active_scan_mode}) @{self.active_marker_centroid} err_x={err_x:+d}"
        else:
            marker_str = "None(0.00, NONE) @None err_x=None"

        if self.minimap_quest_bearing is not None:
            bearing_str = f"{math.degrees(self.minimap_quest_bearing):+.1f}deg"
        else:
            bearing_str = "None"

        trail_str = f"{self.radar_trail_bearing_deg:+.1f}deg" if self.radar_trail_bearing_deg is not None else "None"
        cpd = float(self.config.get("counts_per_degree", self.counts_per_degree))

        telem_str = (
            f"state={self.current_state} nav={self.nav_mode} walk={self.hid.is_walking} | "
            f"minimap={minimap_present}({minimap_score:.2f}) | "
            f"marker={marker_str} | "
            f"mini_bearing={bearing_str} | "
            f"trail_bearing={trail_str} | "
            f"counts_per_deg={cpd:.3f} | "
            f"f_prompt={f_prompt} | "
            f"motion={self.smooth_motion_score:.2f}"
        )
        self.logger.log_event("TELEM", telem_str)

    def _set_lost_marker_phase(self, new_phase: str, reason: str = ""):
        """Transitions lost-marker recovery phase and logs event."""
        if self.lost_marker_phase != new_phase:
            old_phase = self.lost_marker_phase
            self.lost_marker_phase = new_phase
            if self.logger:
                msg = f"Lost-marker phase {old_phase} -> {new_phase}"
                if reason:
                    msg += f" ({reason})"
                self.logger.log_event("NAV", msg)

    def trigger_yaw_calibration(self):
        """Requests camera yaw calibration on next navigation step."""
        print("[CALIB] Yaw calibration requested.")
        self.calibration_phase = "START"
        self.yaw_calibrated = False

    def calibrate_yaw(
        self,
        frame: Optional[np.ndarray] = None,
        wait_func=time.sleep,
        next_frame: Optional[np.ndarray] = None,
    ) -> bool:
        """
        Synchronous camera yaw calibration against minimap player arrow.
        1. Ensures walking stopped (W0).
        2. Measures initial yaw_0.
        3. Sends M45,0 pulse and waits 0.18s.
        4. Measures yaw_1 and calculates counts_per_degree.
        """
        self.hid.stop_walk()
        if frame is None:
            return False

        yaw_0 = self.detector.get_minimap_player_yaw(frame)
        if yaw_0 is None:
            print("[CALIB WARN] Could not detect player arrow on minimap for yaw calibration.")
            return False

        self.hid.move_mouse(45, 0)
        wait_func(0.18)

        # Re-check frame after pulse
        f1 = next_frame if next_frame is not None else frame
        yaw_1 = self.detector.get_minimap_player_yaw(f1)
        if yaw_1 is None:
            return False

        delta_deg = (yaw_1 - yaw_0 + 180.0) % 360.0 - 180.0
        abs_delta = abs(delta_deg)
        if abs_delta >= 2.0:
            new_cpd = max(0.30, min(1.80, 45.0 / abs_delta))
            self.counts_per_degree = round(new_cpd, 3)
            self.config["counts_per_degree"] = self.counts_per_degree
            self.config["yaw_calibrated"] = True
            self.yaw_calibrated = True
            save_config(self.config)
            msg = f"Measured delta={delta_deg:.1f} deg -> counts_per_degree={self.counts_per_degree:.3f}"
            print(f"[CALIB] {msg}")
            if self.logger:
                self.logger.log_event("CALIB", msg)
            return True
        return False

    def process_frame(self, frame: np.ndarray, frame_half: np.ndarray, frame_idx: int):
        now = time.time()
        self.detector.update_yolo_frame(frame)
        self.detected_party_size = self.detector.detect_party_size(frame)

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 1: DIALOGUE (Strict Top Priority)
        # ----------------------------------------------------------------------
        minimap_present, minimap_score = self.detector.check_minimap_present(frame)

        if not minimap_present:
            if self.current_state != STATE_DIALOGUE:
                old_state = self.current_state
                self.current_state = STATE_DIALOGUE
                print(f"[STATE] Entered {STATE_DIALOGUE} (Minimap hidden, metric: {minimap_score:.3f})")
                if self.logger:
                    self.logger.log_event("STATE", f"{old_state} -> DIALOGUE (minimap_score={minimap_score:.3f})")
                    if self.nav_mode != "ALIGN":
                        self.logger.log_event("NAV", "Mode reset -> ALIGN (entered dialogue)")
                self.hid.stop_all()
                self.hid.release_lmb()
                self.combat_consecutive_frames = 0
                self.sealed_chest_consecutive_frames = 0
                self.f_prompt_consecutive_frames = 0
                self.is_scanning_for_marker = False
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.nav_mode = "ALIGN"
                self.align_pitch_steps = 0
                self.needs_pitch_reset = True
                self.next_dialogue_f_time = now + dialogue_advance_delay()
                if self.is_aiming_bow:
                    self.hid.press_r()
                    self.is_aiming_bow = False
                    self.bow_substate = "AIM_IDLE"

            # Check for dialogue choice icons
            self.has_active_choice, self.choice_box = self.detector.check_choice_icon(frame)

            if now >= self.next_dialogue_f_time:
                self.hid.press_f()
                # If a choice is active, slightly faster cadence to confirm option
                delay = dialogue_advance_delay() * (0.85 if self.has_active_choice else 1.0)
                self.next_dialogue_f_time = now + delay

            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            self.previous_minimap_state = False
            return

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 2: COMBAT_AUTO / COMBAT_MANUAL (Strict HP Bar Geometry)
        # ----------------------------------------------------------------------
        has_hp_bar, num_bars, largest_area, enemy_centroid, is_elevated, hp_bbox = (
            self.detector.detect_combat_red_hp_bars(frame)
        )
        self.detected_hp_bar_box = hp_bbox

        if has_hp_bar:
            self.combat_consecutive_frames += 1
        else:
            self.combat_consecutive_frames = 0

        in_combat = (
            (self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL) and has_hp_bar)
            or (self.combat_consecutive_frames >= 2)
        )

        if in_combat:
            self.combat_clear_start = 0.0
            self.combat_target_centroid = enemy_centroid
            self.combat_largest_area = largest_area

            if self.current_state not in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
                old_state = self.current_state
                self.current_state = STATE_COMBAT_AUTO
                self.combat_start_time = now
                self.melee_damage_stall_timer = now
                self.last_enemy_hp_pixels = int(largest_area)
                self.is_scanning_for_marker = False
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.nav_mode = "ALIGN"
                self.align_pitch_steps = 0
                self.needs_pitch_reset = True
                self.hid.stop_walk()
                self.hid.stop_all()
                print(f"[STATE] Entered {STATE_COMBAT_AUTO} (Valid HP bars: {num_bars}, aspect ratio >= 5.5, elevated: {is_elevated})")
                if self.logger:
                    self.logger.log_event("STATE", f"{old_state} -> COMBAT_AUTO (num_bars={num_bars}, largest_area={largest_area:.1f}, elevated={is_elevated}, hp_bbox={hp_bbox})")
                    if old_state != STATE_COMBAT_AUTO:
                        self.logger.log_event("NAV", "Mode reset -> ALIGN (entered combat)")

            if self.current_state == STATE_COMBAT_AUTO and (now - self.combat_start_time > 45.0):
                print(f"[STATE] Combat exceeded 45s threshold -> {STATE_COMBAT_MANUAL}")
                self.current_state = STATE_COMBAT_MANUAL
                self.hid.stop_all()
                play_alert_chime(880, 300)
                if self.logger:
                    self.logger.log_event("STATE", f"COMBAT_AUTO -> COMBAT_MANUAL (combat_duration={now - self.combat_start_time:.1f}s > 45.0s)")

            if self.current_state == STATE_COMBAT_AUTO:
                self._execute_auto_combat_step(now, is_elevated, int(largest_area))
            else:
                self.hid.stop_all()

            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            self.previous_minimap_state = True
            return

        elif self.current_state in (STATE_COMBAT_AUTO, STATE_COMBAT_MANUAL):
            # 0 valid HP bars detected -> wait >= 1.5s before exiting combat
            if self.combat_clear_start == 0.0:
                self.combat_clear_start = now

            if now - self.combat_clear_start >= 1.5:
                print("[STATE] Combat ended (0 valid HP bars for 1.5s). Resuming overworld.")
                self.hid.stop_all()
                if self.is_aiming_bow:
                    self.hid.press_r()
                    self.is_aiming_bow = False
                    self.bow_substate = "AIM_IDLE"
                old_state = self.current_state
                self.current_state = STATE_NAVIGATE
                self.nav_mode = "ALIGN"
                self.align_pitch_steps = 0
                self.needs_pitch_reset = True
                self.combat_ended_time = now
                self.pending_v_pulse_time = now + random.uniform(0.4, 0.8)
                if self.logger:
                    self.logger.log_event("STATE", f"{old_state} -> NAVIGATE (0 valid HP bars for 1.5s)")

            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            self.previous_minimap_state = True
            return

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 3: CHEST_LOOT (Active Vacuum OR Confirmed Hollow Ring)
        # ----------------------------------------------------------------------
        if self.vacuum_active:
            if self.current_state != STATE_CHEST_LOOT:
                old_state = self.current_state
                self.current_state = STATE_CHEST_LOOT
                if self.logger:
                    self.logger.log_event("STATE", f"{old_state} -> CHEST_LOOT (vacuum active)")
            self._execute_loot_vacuum_step(now, frame)
            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            self.previous_minimap_state = True
            return

        ring_detected, ring_box, ring_centroid = self.detector.detect_sealed_chest_ring(frame_half)
        if ring_detected and ring_centroid:
            self.sealed_chest_consecutive_frames += 1
        else:
            self.sealed_chest_consecutive_frames = 0

        # Require >= 4 consecutive frames to eliminate false chest detours
        if self.sealed_chest_consecutive_frames >= 4 and ring_centroid:
            if self.current_state != STATE_CHEST_LOOT:
                old_state = self.current_state
                self.current_state = STATE_CHEST_LOOT
                if self.logger:
                    self.logger.log_event("STATE", f"{old_state} -> CHEST_LOOT (sealed chest ring confirmed, ring_box={ring_box}, ring_centroid={ring_centroid})")
            self.nav_mode = "ALIGN"
            self.align_pitch_steps = 0
            self.needs_pitch_reset = True
            self.active_chest_box = ring_box
            cx, cy = ring_centroid
            error_x = cx - 640

            f_detected, _ = self.detector.detect_overworld_f_prompt(frame)
            if f_detected or cy >= 440:
                self.start_loot_vacuum(now, trigger_source="sealed_chest_approach")
            else:
                dx, should_pause = self.steering.compute_steering(error_x, now)
                if dx != 0:
                    self.hid.move_mouse(dx, 0)
                if should_pause:
                    self.hid.stop_walk()
                else:
                    self.hid.start_walk()

            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_detected)
            self.previous_minimap_state = True
            return
        elif self.current_state == STATE_CHEST_LOOT and not self.vacuum_active:
            self.current_state = STATE_NAVIGATE
            self.active_chest_box = None
            if self.logger:
                self.logger.log_event("STATE", "CHEST_LOOT -> NAVIGATE (sealed chest ring lost)")

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 4: NAVIGATE (Pure Template + Minimap Turn + Staged Unstuck)
        # ----------------------------------------------------------------------
        if self.current_state != STATE_NAVIGATE:
            old_state = self.current_state
            self.current_state = STATE_NAVIGATE
            if self.logger:
                self.logger.log_event("STATE", f"{old_state} -> NAVIGATE")
        self._execute_navigate_step(now, frame, frame_half, frame_idx, minimap_present, minimap_score)
        self.previous_minimap_state = True

    # ==========================================================================
    # Multi-Second Loot Vacuum Step
    # ==========================================================================
    def start_loot_vacuum(self, now: float, trigger_source: str = "chest"):
        """Initiates multi-second loot vacuum sequence."""
        self.hid.stop_walk()
        self.hid.press_f()
        self.vacuum_active = True
        self.nav_mode = "ALIGN"
        self.align_pitch_steps = 0
        self.needs_pitch_reset = True
        self.vacuum_phase = "WAIT_ANIMATION"
        self.vacuum_animation_end_time = now + 0.65
        burst_duration = random.uniform(2.2, 2.8)
        self.vacuum_burst_start_time = self.vacuum_animation_end_time
        self.vacuum_burst_end_time = self.vacuum_animation_end_time + burst_duration
        self.next_vacuum_f_time = self.vacuum_animation_end_time
        self.vacuum_taps_count = 1
        self.vacuum_wiggle_stage = 0
        self.vacuum_drain_timeout = self.vacuum_burst_end_time + 4.0
        old_state = self.current_state
        self.current_state = STATE_CHEST_LOOT
        if self.logger:
            self.logger.log_event("STATE", f"{old_state} -> CHEST_LOOT (start_loot_vacuum triggered by {trigger_source})")
        print(f"[LOOT VACUUM] Chest/loot triggered: waiting 0.65s lid animation then {burst_duration:.2f}s vacuum burst.")

    def _execute_loot_vacuum_step(self, now: float, frame: np.ndarray):
        """Drives the multi-second loot vacuum sequence."""
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
                self.nav_mode = "ALIGN"
                self.align_pitch_steps = 0
                self.needs_pitch_reset = True

    # ==========================================================================
    # Non-Blocking Starter Combat & Amber Bow Aiming
    # ==========================================================================
    def _execute_auto_combat_step(self, now: float, is_elevated: bool, red_pixels: int):
        """Executes deliberate, humanized starter combat with NON-BLOCKING Amber Aim state machine."""
        if red_pixels < self.last_enemy_hp_pixels:
            self.melee_damage_stall_timer = now
            self.last_enemy_hp_pixels = red_pixels
        elif now - self.melee_damage_stall_timer > 4.0:
            is_elevated = True

        amber_slot = 2 if self.detected_party_size >= 2 else 1

        # ----------------------------------------------------------------------
        # AMBER BOW AIM-SHOT NON-BLOCKING STATE MACHINE
        # ----------------------------------------------------------------------
        if is_elevated and self.detected_party_size >= 2:
            if not self.is_aiming_bow:
                if self.current_party_slot != amber_slot:
                    if self.bow_substate == "AIM_IDLE":
                        self.hid.select_slot(amber_slot)
                        self.current_party_slot = amber_slot
                        self.last_party_swap_time = now
                        self.bow_substate = "AIM_SWAP_WAIT"
                        self.bow_substate_ready_time = now + 0.40
                        self.active_combat_action_label = f"Swapping to Amber (Slot {amber_slot})"
                        return
                    elif self.bow_substate == "AIM_SWAP_WAIT":
                        if now < self.bow_substate_ready_time:
                            return
                        self.bow_substate = "AIM_ENTER_R"

                self.hid.press_r()
                self.is_aiming_bow = True
                self.bow_aim_shot_count = 0
                self.bow_substate = "AIM_TRACK_AND_CHARGE"
                self.hid.hold_lmb()
                self.bow_charge_start_time = now
                self.active_combat_action_label = "Amber: Aim Mode [R] Charging Shot"
                return

            # While in Aim Mode: Active 2-Axis Tracking & Charging (rate-limited to 28 Hz / 0.035s)
            if self.bow_substate == "AIM_TRACK_AND_CHARGE":
                if self.combat_target_centroid is not None:
                    if now - self.last_aim_mouse_time >= 0.035:
                        ecx, ecy = self.combat_target_centroid
                        aim_err_x = ecx - 640
                        aim_err_y = (ecy + 35) - 360
                        dx = max(-40, min(40, int(aim_err_x * 0.22)))
                        dy = max(-40, min(40, int(aim_err_y * 0.22)))
                        self.hid.move_mouse(dx, dy)
                        self.last_aim_mouse_time = now

                charge_elapsed = now - self.bow_charge_start_time
                self.active_combat_action_label = f"Amber: Charging Shot ({charge_elapsed:.1f}s/1.6s) [{self.bow_aim_shot_count + 1}/3]"

                if charge_elapsed >= 1.6:
                    self.hid.release_lmb()
                    self.bow_aim_shot_count += 1
                    self.bow_substate = "AIM_RELEASE"
                    self.bow_substate_ready_time = now + 0.35
                    self.active_combat_action_label = f"Amber: Shot Released ({self.bow_aim_shot_count}/3)"
                return

            elif self.bow_substate == "AIM_RELEASE":
                if now < self.bow_substate_ready_time:
                    return

                if self.bow_aim_shot_count >= 3:
                    self.hid.press_r()
                    self.is_aiming_bow = False
                    self.bow_substate = "AIM_IDLE"
                    self.melee_damage_stall_timer = now
                    self.active_combat_action_label = "Amber: Exiting Aim Mode [R]"
                else:
                    self.bow_substate = "AIM_TRACK_AND_CHARGE"
                    self.hid.hold_lmb()
                    self.bow_charge_start_time = now
                return

        # If not elevated and aiming bow is active, exit Aim Mode
        if self.is_aiming_bow:
            self.hid.press_r()
            self.is_aiming_bow = False
            self.bow_substate = "AIM_IDLE"
            self.combat_step_ready_time = now + 0.30
            return

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
    # Overworld Navigation (Pure Template + Minimap Turn + Staged Unstuck)
    # ==========================================================================
    def _execute_navigate_step(
        self, now: float, frame: np.ndarray, frame_half: np.ndarray, frame_idx: int,
        minimap_present: bool = True, minimap_score: float = 1.0
    ):
        # 1. Transition V pulse from Dialogue
        if not self.previous_minimap_state:
            self.pending_v_pulse_time = now + random.uniform(0.3, 0.6)
            self.nav_mode = "ALIGN"
            self.align_pitch_steps = 0
            self.needs_pitch_reset = True

        if self.pending_v_pulse_time and now >= self.pending_v_pulse_time:
            self.hid.press_v()
            if self.logger:
                self.logger.log_event("NAV", "Dispatched 'V' pulse (post-dialogue transition)")
            self.pending_v_pulse_time = None

        # 2. Camera Pitch Reset for Mondstadt Elevation
        if self.needs_pitch_reset:
            pitch_counts = int(self.config.get("default_pitch_up_counts", -30))
            if pitch_counts != 0:
                self.hid.move_mouse(0, pitch_counts)
                if self.logger:
                    self.logger.log_event("NAV", f"Dispatched pitch reset upward (dy={pitch_counts})")
            self.needs_pitch_reset = False

        # 3. Closed-Loop Camera Yaw Calibration Check
        if not self.yaw_calibrated and self.calibration_phase == "IDLE":
            self.calibration_phase = "START"

        if self.calibration_phase == "START":
            self.hid.stop_walk()
            yaw_0 = self.detector.get_minimap_player_yaw(frame)
            if yaw_0 is not None:
                self.calib_yaw_0 = yaw_0
                self.hid.move_mouse(45, 0)
                self.calib_pulse_sent_time = now
                self.calibration_phase = "WAIT_SETTLE"
                msg = f"Yaw calibration started: measured yaw_0={yaw_0:.1f} deg, sent M45,0 pulse, settling 0.18s"
                print(f"[CALIB] {msg}")
                if self.logger:
                    self.logger.log_event("CALIB", msg)
            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            return

        elif self.calibration_phase == "WAIT_SETTLE":
            if now - self.calib_pulse_sent_time >= 0.18:
                yaw_1 = self.detector.get_minimap_player_yaw(frame)
                if yaw_1 is not None and self.calib_yaw_0 is not None:
                    delta_deg = (yaw_1 - self.calib_yaw_0 + 180.0) % 360.0 - 180.0
                    abs_delta = abs(delta_deg)
                    if abs_delta >= 2.0:
                        new_cpd = max(0.30, min(1.80, 45.0 / abs_delta))
                        self.counts_per_degree = round(new_cpd, 3)
                        self.config["counts_per_degree"] = self.counts_per_degree
                        self.config["yaw_calibrated"] = True
                        self.yaw_calibrated = True
                        save_config(self.config)
                        msg = f"Measured delta={delta_deg:.1f} deg -> counts_per_degree={self.counts_per_degree:.3f}"
                        print(f"[CALIB] {msg}")
                        if self.logger:
                            self.logger.log_event("CALIB", msg)
                    else:
                        print(f"[CALIB WARN] Measured delta too small ({delta_deg:.1f} deg). Retaining counts_per_degree={self.counts_per_degree:.3f}")
                self.calibration_phase = "IDLE"
            self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=False)
            return

        # 4. Overworld [F] Prompt Detection (Keycap + Dark Pill Strip)
        f_prompt_detected, prompt_box = self.detector.detect_overworld_f_prompt(frame)
        if f_prompt_detected and prompt_box:
            self.f_prompt_consecutive_frames += 1
            self.f_prompt_box = prompt_box
            if self.f_prompt_consecutive_frames >= 2 and (now - self.last_overworld_f_time >= self.overworld_f_cooldown):
                # Situation A: Monster drops within 4.0s of combat end
                if now - self.combat_ended_time <= 4.0:
                    print("[NAV] Monster drops detected post-combat -> starting loot vacuum.")
                    if self.logger:
                        self.logger.log_event("NAV", f"[F] prompt triggered: monster drops post-combat (ended {now - self.combat_ended_time:.2f}s ago <= 4.0s) -> starting loot vacuum")
                    self.show_overworld_f_box_until = now + 1.2
                    self.start_loot_vacuum(now, trigger_source="post_combat_drops")
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=True)
                    return

                # Situation B: Quest Target Arrival
                # 3D Marker locked (or locked within last 3.5s) near center (|error_x| <= 135 px)
                marker_recent = (now - self.marker_last_seen_time <= 3.5)
                marker_centered = (self.last_valid_marker_x is not None and abs(self.last_valid_marker_x - 640) <= 135)

                if marker_recent and marker_centered:
                    print("[NAV] Arrived at quest target with [F] prompt -> single F tap.")
                    if self.logger:
                        self.logger.log_event("NAV", f"[F] prompt triggered: quest target arrival (marker_recent=True [dt={now - self.marker_last_seen_time:.2f}s], marker_centered=True [x={self.last_valid_marker_x}]) -> single F tap")
                    self.hid.press_f()
                    self.hid.stop_walk()
                    self.nav_mode = "ALIGN"
                    self.align_pitch_steps = 0
                    self.needs_pitch_reset = True
                    self.last_overworld_f_time = now
                    self.overworld_f_cooldown = 1.2
                    self.show_overworld_f_box_until = now + 1.2
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=True)
                    return
                else:
                    reasons = []
                    if not marker_recent:
                        reasons.append(f"marker_recent=False (dt={now - self.marker_last_seen_time:.2f}s > 3.5s)")
                    if not marker_centered:
                        err_str = f"err_x={abs(self.last_valid_marker_x - 640)}" if self.last_valid_marker_x is not None else "last_valid_marker_x=None"
                        reasons.append(f"marker_centered=False ({err_str} > 135 px)")
                    if self.logger:
                        self.logger.log_event("NAV", f"[F] prompt ignored (NPC/interactable): {', '.join(reasons)}")
        else:
            self.f_prompt_consecutive_frames = 0

        # 4. Downscaled Ground Motion Differencing & Staged Unstuck (Every 3rd frame)
        if frame_idx % 3 == 0:
            raw_motion, self.prev_ground_gray_half = self.detector.calculate_ground_motion_downscaled(
                frame_half, self.prev_ground_gray_half
            )
            self.smooth_motion_score = 0.75 * self.smooth_motion_score + 0.25 * raw_motion

        # Non-blocking staged unstuck recovery (0.8s hop -> 1.7s lateral turn + hop)
        if self.hid.is_walking and self.smooth_motion_score < 1.05:
            if self.unstuck_still_start_time == 0.0:
                self.unstuck_still_start_time = now
            else:
                stall_duration = now - self.unstuck_still_start_time
                if stall_duration >= 0.8 and self.unstuck_stage == 0:
                    print(f"[UNSTUCK] Stage 1: Stall duration {stall_duration:.1f}s -> SPACE hop")
                    if self.logger:
                        self.logger.log_event("UNSTUCK", f"Stage 1 triggered: ground motion stall {stall_duration:.2f}s >= 0.8s (motion={self.smooth_motion_score:.2f}) -> SPACE hop")
                    self.hid.press_space()
                    self.unstuck_stage = 1
                elif stall_duration >= 1.7 and self.unstuck_stage == 1:
                    print(f"[UNSTUCK] Stage 2: Stall duration {stall_duration:.1f}s -> W0 + lateral turn + W1 + SPACE")
                    lateral_dx = 70 if random.random() < 0.5 else -70
                    if self.logger:
                        self.logger.log_event("UNSTUCK", f"Stage 2 triggered: ground motion stall {stall_duration:.2f}s >= 1.7s -> W0 + lateral turn {lateral_dx} + W1 + SPACE")
                    self.hid.stop_walk()
                    self.hid.move_mouse(lateral_dx, 0)
                    self.hid.start_walk()
                    self.hid.press_space()
                    self.hid.walk_start_time = now
                    self.unstuck_still_start_time = 0.0
                    self.unstuck_stage = 0
        else:
            self.unstuck_still_start_time = 0.0
            self.unstuck_stage = 0

        # 5. 2D Minimap Radar + V-Trail Path Extraction
        radar = self.detector.analyze_minimap_radar(frame)
        target_bearing_deg = radar["target_bearing_deg"]
        obj_dist_px = radar["objective_dist_px"]
        trail_bearing_deg = radar["trail_bearing_deg"]
        self.radar_trail_bearing_deg = trail_bearing_deg
        self.radar_target_bearing_deg = target_bearing_deg
        self.radar_dist_px = obj_dist_px
        self.radar_source = radar["steering_source"]

        if radar["objective_bearing_deg"] is not None:
            self.minimap_quest_bearing = math.radians(radar["objective_bearing_deg"])
        else:
            self.minimap_quest_bearing = None

        # Primary Sensor: 2D Minimap Radar + V-Trail Navigation
        # 3D marker is demoted to terminal approach only (objective_dist_px <= 26 px)
        if target_bearing_deg is not None and (obj_dist_px is None or obj_dist_px > 26.0):
            if self.nav_mode == "CRUISE_LOCKED":
                if abs(target_bearing_deg) > 28.0:
                    print(f"[NAV] Exiting CRUISE_LOCKED -> ALIGN: Target bearing drift (|{target_bearing_deg:.1f}| > 28 deg)")
                    if self.logger:
                        self.logger.log_event("NAV", f"Mode CRUISE_LOCKED -> ALIGN (target_bearing={target_bearing_deg:.1f} deg > 28 deg)")
                    self.nav_mode = "ALIGN"
                    self.align_pitch_steps = 0
                    self.hid.stop_walk()
                else:
                    # Maintain forward walk and periodic sprint bursts
                    if not self.hid.is_walking and not self.vacuum_active:
                        self.hid.start_walk()

                    if self.hid.is_walking:
                        if now - self.last_sprint_time >= self.next_sprint_interval:
                            self.hid.click_rmb()
                            self.last_sprint_time = now
                            self.next_sprint_interval = random.uniform(2.2, 3.8)

                        if now - self.hid.last_command_time >= 4.0:
                            self.hid.send_command("W1")

                    # Lock out all camera panning: dx = 0
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                    return

            if self.nav_mode == "ALIGN":
                if self.hid.is_walking:
                    self.hid.stop_walk()

                if abs(target_bearing_deg) <= 14.0:
                    print(f"[NAV] Aligned with radar target ({target_bearing_deg:.1f} deg <= 14 deg) -> CRUISE_LOCKED")
                    if self.logger:
                        self.logger.log_event("NAV", f"Mode ALIGN -> CRUISE_LOCKED (aligned with radar target {target_bearing_deg:.1f} deg <= 14 deg)")
                    self.nav_mode = "CRUISE_LOCKED"
                    self.cruise_source = "RADAR"
                    if not self.vacuum_active:
                        self.hid.start_walk()
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                    return

                # One-Shot Calibrated Turning
                if now >= self.next_turn_ready_time:
                    cpd = float(self.config.get("counts_per_degree", self.counts_per_degree))
                    raw_dx = int(target_bearing_deg * cpd)
                    dx = max(-95, min(95, raw_dx))
                    if dx != 0:
                        self.hid.move_mouse(dx, 0)
                        self.next_turn_ready_time = now + 0.14
                        if self.logger:
                            self.logger.log_event("NAV", f"One-shot calibrated turn: bearing={target_bearing_deg:.1f} deg -> M{dx},0 (cpd={cpd:.3f}, settle=0.14s)")
                self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                return

        # 6. Terminal Approach: Hybrid 3D Quest Tracking (Template Matching + Direct Diamond HSV & Dark Outline)
        centroid, marker_type, bbox, score, scan_mode = self.detector.track_quest_marker(
            frame, self.config, frame_idx, self.last_template_lock
        )
        self.active_marker_type = marker_type
        self.active_marker_centroid = centroid
        self.active_marker_bbox = bbox
        self.active_marker_score = score
        self.active_scan_mode = scan_mode

        deadzone = int(self.config.get("center_lock_deadzone", 24))
        drift_corridor = int(self.config.get("cruise_drift_corridor", 95))

        if centroid is not None:
            cx, cy = centroid
            raw_error_x = cx - 640
            if not self.prev_marker_visible:
                self.prev_marker_visible = True
                if self.logger:
                    self.logger.log_event("VISION", f"3D quest marker ACQUIRED: type={marker_type}, scan_mode={scan_mode}, score={score:.2f}, pos={centroid}, err_x={raw_error_x:+d}")
            self.marker_last_seen_time = now
            self.last_valid_marker_x = centroid[0]
            self.is_scanning_for_marker = False
            self._set_lost_marker_phase("IDLE", "marker visible")
            self.lost_marker_start_time = 0.0
            self.lost_marker_step_count = 0
            self.last_template_lock = {
                "centroid": centroid,
                "type": marker_type,
                "bbox": bbox,
                "score": score,
            }
        else:
            if self.prev_marker_visible:
                self.prev_marker_visible = False
                if self.logger:
                    self.logger.log_event("VISION", f"3D quest marker LOST (last_seen_x={self.last_valid_marker_x})")
            self.last_template_lock = None
            cx, cy = None, None
            raw_error_x = None

        # ----------------------------------------------------------------------
        # TWO-STATE HYSTERESIS NAVIGATION: CRUISE_LOCKED vs ALIGN
        # ----------------------------------------------------------------------
        if self.nav_mode == "CRUISE_LOCKED":
            should_exit_cruise = False
            exit_reason = ""

            if centroid is not None:
                self.cruise_source = "3D"
                if abs(raw_error_x) > drift_corridor:
                    should_exit_cruise = True
                    exit_reason = f"Drift corridor exceeded (|{raw_error_x}| > {drift_corridor} px)"
            else:
                if self.cruise_source == "3D":
                    if now - self.marker_last_seen_time > 0.65:
                        should_exit_cruise = True
                        exit_reason = f"3D marker occluded ({now - self.marker_last_seen_time:.2f}s > 0.65s)"
                elif self.cruise_source == "MINIMAP":
                    if quest_bearing is None:
                        should_exit_cruise = True
                        exit_reason = "Minimap bearing lost"
                    elif abs(math.degrees(quest_bearing)) > 35.0:
                        should_exit_cruise = True
                        exit_reason = f"Minimap bearing drifted ({abs(math.degrees(quest_bearing)):.1f} deg > 35 deg)"

            if should_exit_cruise:
                print(f"[NAV] Exiting CRUISE_LOCKED -> ALIGN: {exit_reason}")
                if self.logger:
                    self.logger.log_event("NAV", f"Mode CRUISE_LOCKED -> ALIGN (exit_reason: {exit_reason})")
                self.nav_mode = "ALIGN"
                self.align_pitch_steps = 0
                self.hid.stop_walk()
                # Fall through to ALIGN handling below to begin steering immediately
            else:
                # Maintain forward walk and periodic sprint bursts
                if not self.hid.is_walking and not self.vacuum_active:
                    self.hid.start_walk()

                if self.hid.is_walking:
                    if now - self.last_sprint_time >= self.next_sprint_interval:
                        self.hid.click_rmb()
                        self.last_sprint_time = now
                        self.next_sprint_interval = random.uniform(2.2, 3.8)

                    if now - self.hid.last_command_time >= 4.0:
                        self.hid.send_command("W1")

                # LOCK OUT ALL CAMERA PANNING (dx = 0): No mouse steering commands sent
                self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                return

        # ----------------------------------------------------------------------
        # MODE A: ALIGN (Stationary Camera Centering)
        # ----------------------------------------------------------------------
        if self.nav_mode == "ALIGN":
            if self.hid.is_walking:
                self.hid.stop_walk()

            # Case 1: 3D Marker Visible
            if centroid is not None:
                if abs(raw_error_x) <= deadzone:
                    print(f"[NAV] Aligned with 3D marker (|{raw_error_x}| <= {deadzone} px) -> CRUISE_LOCKED")
                    if self.logger:
                        self.logger.log_event("NAV", f"Mode ALIGN -> CRUISE_LOCKED (aligned with 3D marker, |err_x|={abs(raw_error_x)} <= {deadzone} px)")
                    self.nav_mode = "CRUISE_LOCKED"
                    self.cruise_source = "3D"
                    if not self.vacuum_active:
                        self.hid.start_walk()
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                    return

                # ALIGN steers 100% purely from raw_error_x (no minimap or road blending)
                effective_error_x = float(raw_error_x)

                dx, _ = self.steering.compute_steering(effective_error_x, now)
                # Upward pitch step capped at 3 steps per ALIGN cycle, only when cy < 140 and dx != 0
                if cy < 140 and dx != 0 and self.align_pitch_steps < 3:
                    dy = -4
                    self.align_pitch_steps += 1
                else:
                    dy = 0

                if dx != 0 or dy != 0:
                    self.hid.move_mouse(dx, dy)

                if raw_error_x > 20:
                    self.scan_sweep_direction = 1
                elif raw_error_x < -20:
                    self.scan_sweep_direction = -1
                self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                return

            else:
                # 0.45s Grace Buffer Before Falling Back to Minimap Bearing / Lost Recovery
                time_lost = now - self.marker_last_seen_time
                if time_lost < 0.45:
                    # Brief single-frame template flicker: hold heading (dx=0), do not pivot or sweep
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                    return

                # Case 2: 3D Marker off-screen, Minimap Quest Bearing Present
                if quest_bearing is not None:
                    self.is_scanning_for_marker = False
                    self._set_lost_marker_phase("IDLE", "minimap bearing active")
                    self.lost_marker_start_time = 0.0
                    self.lost_marker_step_count = 0

                    # Check 'V' pulse for occluded 3D marker every 5.0s
                    v_delay = float(self.config.get("lost_marker_v_delay", 0.8))
                    v_cooldown = float(self.config.get("quest_v_pulse_cooldown", 5.0))
                    if time_lost > v_delay and (now - self.last_quest_v_pulse_time >= v_cooldown):
                        self.hid.press_v()
                        if self.logger:
                            self.logger.log_event("NAV", f"Dispatched 'V' pulse (occluded marker, lost for {time_lost:.2f}s > {v_delay}s)")
                        self.last_quest_v_pulse_time = now
                        print(f"[NAV] Quest marker occluded ({time_lost:.2f}s > {v_delay}s): dispatched 'V' pulse.")

                    bearing_deg = abs(math.degrees(quest_bearing))
                    if bearing_deg <= 20.0:
                        print(f"[NAV] Aligned with minimap bearing ({bearing_deg:.1f} deg <= 20 deg) -> CRUISE_LOCKED")
                        if self.logger:
                            self.logger.log_event("NAV", f"Mode ALIGN -> CRUISE_LOCKED (aligned with minimap bearing {bearing_deg:.1f} deg <= 20.0 deg)")
                        self.nav_mode = "CRUISE_LOCKED"
                        self.cruise_source = "MINIMAP"
                        if not self.vacuum_active:
                            self.hid.start_walk()
                        self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                        return

                    pivot_dx, _ = self.steering.compute_minimap_pivot(quest_bearing, now)
                    if pivot_dx != 0:
                        self.hid.move_mouse(pivot_dx, 0)
                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
                    return

                # Case 3: Lost Marker 4-Phase Recovery Routine (No Endless Rotation)
                else:
                    if self.lost_marker_phase == "IDLE":
                        self._set_lost_marker_phase("WAIT_INITIAL", "start recovery")
                        self.lost_marker_start_time = now
                        self.lost_marker_step_count = 0

                    if self.lost_marker_phase == "WAIT_INITIAL":
                        if now - self.lost_marker_start_time >= float(self.config.get("lost_marker_v_delay", 0.8)):
                            v_cooldown = float(self.config.get("quest_v_pulse_cooldown", 5.0))
                            if now - self.last_quest_v_pulse_time >= v_cooldown:
                                self.hid.press_v()
                                if self.logger:
                                    self.logger.log_event("NAV", "Dispatched 'V' pulse (lost marker recovery Phase 1)")
                                self.last_quest_v_pulse_time = now
                                print("[NAV] Lost marker: dispatched 'V' pulse.")
                            self._set_lost_marker_phase("WAIT_V_PULSE", "0.8s wait expired")
                            self.lost_marker_start_time = now

                    elif self.lost_marker_phase == "WAIT_V_PULSE":
                        if now - self.lost_marker_start_time >= 1.0:
                            self._set_lost_marker_phase("STEP_SWEEP", "1.0s wait expired")
                            self.lost_marker_step_count = 0
                            self.last_step_sweep_time = now

                    elif self.lost_marker_phase == "STEP_SWEEP":
                        self.is_scanning_for_marker = True
                        if now - self.last_step_sweep_time >= 0.28:
                            if self.lost_marker_step_count < 8:
                                step_dx = 32 * self.scan_sweep_direction
                                self.hid.move_mouse(step_dx, 0)
                                self.lost_marker_step_count += 1
                                self.last_step_sweep_time = now
                            else:
                                print(f"[NAV] Completed 8-step scan sweep ({self.lost_marker_step_count}/8). Stopping rotation.")
                                self.is_scanning_for_marker = False
                                self._set_lost_marker_phase("SWEEP_COMPLETE", "8 steps completed")

                    elif self.lost_marker_phase == "SWEEP_COMPLETE":
                        self.is_scanning_for_marker = False
                        v_cooldown = float(self.config.get("quest_v_pulse_cooldown", 5.0))
                        if now - self.last_quest_v_pulse_time >= v_cooldown:
                            self.hid.press_v()
                            if self.logger:
                                self.logger.log_event("NAV", "Dispatched 'V' pulse (lost marker recovery Phase 4 cooldown expired)")
                            self.last_quest_v_pulse_time = now
                            print("[NAV] V cooldown expired in idle: dispatched 'V' pulse.")
                            self._set_lost_marker_phase("WAIT_V_PULSE", "cooldown expired")
                            self.lost_marker_start_time = now

                    self._maybe_emit_telemetry(now, minimap_present, minimap_score, f_prompt=f_prompt_detected)
