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
)
from app.controller import ESP32HIDController, PDSteeringController
from app.vision import VisionDetector


class AssistantStateMachine:
    def __init__(self, hid: ESP32HIDController, detector: VisionDetector, config: Dict[str, Any]):
        self.hid = hid
        self.detector = detector
        self.config = config
        self.steering = PDSteeringController(config)

        # Active State
        self.current_state = STATE_NAVIGATE
        self.previous_minimap_state = True
        self.detected_party_size: int = 1

        # Marker Tracking & Locked Target Info
        self.marker_last_seen_time = time.time()
        self.active_marker_type: Optional[str] = None
        self.active_marker_centroid: Optional[Tuple[int, int]] = None
        self.active_marker_bbox: Optional[Tuple[int, int, int, int]] = None
        self.active_marker_score: float = 0.0
        self.active_scan_mode: str = "None"
        self.last_template_lock: Optional[Dict[str, Any]] = None

        # Navigation & Minimap Bearing
        self.active_road_following: bool = False
        self.minimap_quest_bearing: Optional[float] = None
        self.pending_v_pulse_time: Optional[float] = None
        self.scan_sweep_direction: int = 1
        self.last_scan_sweep_time: float = 0.0
        self.next_scan_sweep_interval: float = 0.22
        self.is_scanning_for_marker: bool = False
        self.last_quest_v_pulse_time: float = 0.0
        self.last_sprint_time: float = time.time()
        self.next_sprint_interval: float = random.uniform(2.2, 3.8)

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

        # Non-Blocking Amber Aim State Machine
        self.is_aiming_bow: bool = False
        self.bow_aim_shot_count: int = 0
        self.bow_substate: str = "AIM_IDLE"  # AIM_SWAP_WAIT, AIM_ENTER_R, AIM_TRACK_AND_CHARGE, AIM_RELEASE
        self.bow_substate_ready_time: float = 0.0
        self.bow_charge_start_time: float = 0.0

    def update_config(self, new_config: Dict[str, Any]):
        """Update active configuration parameters dynamically."""
        self.config = new_config
        self.steering.config = new_config

    def process_frame(self, frame: np.ndarray, frame_half: np.ndarray, frame_idx: int):
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
                self.combat_consecutive_frames = 0
                self.sealed_chest_consecutive_frames = 0
                self.f_prompt_consecutive_frames = 0
                self.is_scanning_for_marker = False
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
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
                self.current_state = STATE_COMBAT_AUTO
                self.combat_start_time = now
                self.melee_damage_stall_timer = now
                self.last_enemy_hp_pixels = int(largest_area)
                self.is_scanning_for_marker = False
                self.vacuum_active = False
                self.vacuum_phase = "IDLE"
                self.hid.stop_walk()
                self.hid.stop_all()
                print(f"[STATE] Entered {STATE_COMBAT_AUTO} (Valid HP bars: {num_bars}, aspect ratio >= 5.5, elevated: {is_elevated})")

            if self.current_state == STATE_COMBAT_AUTO and (now - self.combat_start_time > 45.0):
                print(f"[STATE] Combat exceeded 45s threshold -> {STATE_COMBAT_MANUAL}")
                self.current_state = STATE_COMBAT_MANUAL
                self.hid.stop_all()
                play_alert_chime(880, 300)

            if self.current_state == STATE_COMBAT_AUTO:
                self._execute_auto_combat_step(now, is_elevated, int(largest_area))
            else:
                self.hid.stop_all()

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
                self.current_state = STATE_NAVIGATE
                self.pending_v_pulse_time = now + random.uniform(0.4, 0.8)

            self.previous_minimap_state = True
            return

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 3: CHEST_LOOT (Active Vacuum OR Confirmed Hollow Ring)
        # ----------------------------------------------------------------------
        if self.vacuum_active:
            self.current_state = STATE_CHEST_LOOT
            self._execute_loot_vacuum_step(now, frame)
            self.previous_minimap_state = True
            return

        ring_detected, ring_box, ring_centroid = self.detector.detect_sealed_chest_ring(frame_half)
        if ring_detected and ring_centroid:
            self.sealed_chest_consecutive_frames += 1
        else:
            self.sealed_chest_consecutive_frames = 0

        # Require >= 4 consecutive frames to eliminate false chest detours
        if self.sealed_chest_consecutive_frames >= 4 and ring_centroid:
            self.current_state = STATE_CHEST_LOOT
            self.active_chest_box = ring_box
            cx, cy = ring_centroid
            error_x = cx - 640

            f_detected, _ = self.detector.detect_overworld_f_prompt(frame)
            if f_detected or cy >= 440:
                self.start_loot_vacuum(now)
            else:
                dx, should_pause = self.steering.compute_steering(error_x, now)
                if dx != 0:
                    self.hid.move_mouse(dx, 0)
                if should_pause:
                    self.hid.stop_walk()
                else:
                    self.hid.start_walk()

            self.previous_minimap_state = True
            return
        elif self.current_state == STATE_CHEST_LOOT and not self.vacuum_active:
            self.current_state = STATE_NAVIGATE
            self.active_chest_box = None

        # ----------------------------------------------------------------------
        # HIERARCHY LEVEL 4: NAVIGATE (Pure Template + Minimap Turn + Staged Unstuck)
        # ----------------------------------------------------------------------
        self.current_state = STATE_NAVIGATE
        self._execute_navigate_step(now, frame, frame_half, frame_idx)
        self.previous_minimap_state = True

    # ==========================================================================
    # Multi-Second Loot Vacuum Step
    # ==========================================================================
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

            # While in Aim Mode: Active 2-Axis Tracking & Charging
            if self.bow_substate == "AIM_TRACK_AND_CHARGE":
                if self.combat_target_centroid is not None:
                    ecx, ecy = self.combat_target_centroid
                    aim_err_x = ecx - 640
                    aim_err_y = (ecy + 35) - 360
                    dx = max(-40, min(40, int(aim_err_x * 0.22)))
                    dy = max(-40, min(40, int(aim_err_y * 0.22)))
                    self.hid.move_mouse(dx, dy)

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
        self, now: float, frame: np.ndarray, frame_half: np.ndarray, frame_idx: int
    ):
        # 1. Transition V pulse from Dialogue
        if not self.previous_minimap_state:
            self.pending_v_pulse_time = now + random.uniform(0.3, 0.6)

        if self.pending_v_pulse_time and now >= self.pending_v_pulse_time:
            self.hid.press_v()
            self.pending_v_pulse_time = None

        # 2. Overworld [F] Prompt Detection (Keycap + Dark Pill Strip -> 3 consecutive frames)
        f_prompt_detected, prompt_box = self.detector.detect_overworld_f_prompt(frame)
        if f_prompt_detected and prompt_box:
            self.f_prompt_consecutive_frames += 1
            self.f_prompt_box = prompt_box
            if self.f_prompt_consecutive_frames >= 3 and (now - self.last_overworld_f_time >= self.overworld_f_cooldown):
                self.show_overworld_f_box_until = now + 1.2
                self.start_loot_vacuum(now)
                return
        else:
            self.f_prompt_consecutive_frames = 0

        # 3. Downscaled Ground Motion Differencing & Staged Unstuck (Every 3rd frame)
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
                    self.hid.press_space()
                    self.unstuck_stage = 1
                elif stall_duration >= 1.7 and self.unstuck_stage == 1:
                    print(f"[UNSTUCK] Stage 2: Stall duration {stall_duration:.1f}s -> W0 + lateral turn + W1 + SPACE")
                    self.hid.stop_walk()
                    lateral_dx = 70 if random.random() < 0.5 else -70
                    self.hid.move_mouse(lateral_dx, 0)
                    self.hid.start_walk()
                    self.hid.press_space()
                    self.hid.walk_start_time = now
                    self.unstuck_still_start_time = 0.0
                    self.unstuck_stage = 0
        else:
            self.unstuck_still_start_time = 0.0
            self.unstuck_stage = 0

        # 4. Minimap Quest Bearing & Road Segmentation
        quest_bearing, road_bearing, has_road, blip_type = self.detector.analyze_minimap_road_and_quest(frame)
        self.active_road_following = has_road
        self.minimap_quest_bearing = quest_bearing

        # 5. Pure Template-Based Quest Tracking
        centroid, marker_type, bbox, score, scan_mode = self.detector.track_quest_marker(
            frame, self.config, frame_idx, self.last_template_lock
        )
        self.active_marker_type = marker_type
        self.active_marker_centroid = centroid
        self.active_marker_bbox = bbox
        self.active_marker_score = score
        self.active_scan_mode = scan_mode

        if centroid is not None:
            self.marker_last_seen_time = now
            self.is_scanning_for_marker = False
            self.last_template_lock = {
                "centroid": centroid,
                "type": marker_type,
                "bbox": bbox,
                "score": score,
            }
            cx, cy = centroid
            raw_error_x = cx - 640

            # Subtle road-following bias if marker is distant/off-center
            if abs(raw_error_x) > 45 and has_road and road_bearing is not None and self.config.get("road_following_enabled", True):
                road_offset_x = road_bearing * (640.0 / (math.pi / 2.5))
                effective_error_x = 0.70 * raw_error_x + 0.30 * road_offset_x
            else:
                effective_error_x = float(raw_error_x)

            # Two-Zone PD Camera Steering
            dx, should_pause_walk = self.steering.compute_steering(effective_error_x, now)
            if dx != 0:
                self.hid.move_mouse(dx, 0)

            if raw_error_x > 20:
                self.scan_sweep_direction = 1
            elif raw_error_x < -20:
                self.scan_sweep_direction = -1

            # Walk arbitration (pause walking during sharp turns |error_x| > 115 px)
            if should_pause_walk:
                if self.hid.is_walking:
                    self.hid.stop_walk()
            else:
                if not self.hid.is_walking and not self.vacuum_active:
                    self.hid.start_walk()

                if self.hid.is_walking:
                    # Occasional sprint dodge
                    if now - self.last_sprint_time >= self.next_sprint_interval:
                        self.hid.click_rmb()
                        self.last_sprint_time = now
                        self.next_sprint_interval = random.uniform(2.2, 3.8)

                    # 4.0s keepalive check to prevent firmware watchdog from tripping
                    if now - self.hid.last_command_time >= 4.0:
                        self.hid.send_command("W1")

        else:
            # 3D Marker off-screen -> Drop local lock
            self.last_template_lock = None

            # ------------------------------------------------------------------
            # INSTANT DIRECTIONAL TURNING VIA MINIMAP QUEST BEARING
            # ------------------------------------------------------------------
            if quest_bearing is not None:
                self.marker_last_seen_time = now
                self.is_scanning_for_marker = False

                # Calculate instant pivot towards off-screen bearing
                pivot_dx, should_pause_walk = self.steering.compute_minimap_pivot(quest_bearing, now)
                if should_pause_walk and self.hid.is_walking:
                    self.hid.stop_walk()

                if pivot_dx != 0:
                    self.hid.move_mouse(pivot_dx, 0)
                return

            # ------------------------------------------------------------------
            # LOST-MARKER RECOVERY: PULSE 'V' FIRST, WAIT 0.9s, THEN SWEEP
            # ------------------------------------------------------------------
            time_lost = now - self.marker_last_seen_time
            v_delay = float(self.config.get("lost_marker_v_delay", 0.75))
            v_cooldown = float(self.config.get("quest_v_pulse_cooldown", 4.5))

            if time_lost > v_delay and (now - self.last_quest_v_pulse_time >= v_cooldown):
                self.hid.press_v()
                self.last_quest_v_pulse_time = now
                print(f"[NAV] Quest marker lost ({time_lost:.2f}s > {v_delay}s): dispatched 'V' pulse.")

            sweep_delay = float(self.config.get("lost_marker_sweep_delay", 1.65))
            if time_lost > sweep_delay:
                if self.hid.is_walking:
                    self.hid.stop_walk()

                self.is_scanning_for_marker = True
                if now - self.last_scan_sweep_time >= self.next_scan_sweep_interval:
                    burst_dx = random.randint(28, 42) * self.scan_sweep_direction
                    self.hid.move_mouse(burst_dx, 0)
                    self.last_scan_sweep_time = now
                    self.next_scan_sweep_interval = random.uniform(0.18, 0.32)
