#!/usr/bin/env python3
"""
Computer Vision Feature Detectors for Genshin Impact Assistant (v5)
Includes:
- Minimap UI & Gold/Blue Quest Bearing extraction
- Strict Horizontal HP Bar Geometry Combat Detector
- Hollow-Ring Sealed Chest Detector
- Keycap + Dark Pill Strip Overworld [F] Prompt Detector
- Pure Multi-Scale Template Quest Tracking with Concentric Contrast Verification
"""

import os
import math
from typing import Optional, Tuple, Dict, Any, List
import cv2
import numpy as np

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
    ROI_SEALED_CHEST_HALF,
    ROI_GROUND_HALF,
    MINIMAP_CENTER,
    MINIMAP_RADIUS,
    MINIMAP_TEMPLATE_PATH,
    CHOICE_TEMPLATE_PATH,
    QUEST_GOLD_TEMPLATE_PATH,
    QUEST_BLUE_TEMPLATE_PATH,
)


class VisionDetector:
    def __init__(self):
        self.minimap_template = None
        self.choice_template = None
        self.quest_gold_template = None
        self.quest_gold_template_gray = None
        self.quest_blue_template = None
        self.quest_blue_template_gray = None
        self.reload_templates()

        # Precompute circular minimap mask for road & quest extraction
        self.minimap_circle_mask = np.zeros((TARGET_HEIGHT, TARGET_WIDTH), dtype=np.uint8)
        cv2.circle(self.minimap_circle_mask, MINIMAP_CENTER, MINIMAP_RADIUS, 255, -1)

    def reload_templates(self):
        """Load user PNG templates directly from disk without overwriting them."""
        if os.path.exists(MINIMAP_TEMPLATE_PATH):
            tmpl = cv2.imread(MINIMAP_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.minimap_template = tmpl

        if os.path.exists(CHOICE_TEMPLATE_PATH):
            tmpl = cv2.imread(CHOICE_TEMPLATE_PATH, cv2.IMREAD_GRAYSCALE)
            if tmpl is not None:
                self.choice_template = tmpl

        if os.path.exists(QUEST_GOLD_TEMPLATE_PATH):
            tmpl = cv2.imread(QUEST_GOLD_TEMPLATE_PATH, cv2.IMREAD_COLOR)
            if tmpl is not None:
                self.quest_gold_template = tmpl
                self.quest_gold_template_gray = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
                print(f"[VISION] Loaded Gold Quest template ({tmpl.shape[1]}x{tmpl.shape[0]}) from {QUEST_GOLD_TEMPLATE_PATH}")

        if os.path.exists(QUEST_BLUE_TEMPLATE_PATH):
            tmpl = cv2.imread(QUEST_BLUE_TEMPLATE_PATH, cv2.IMREAD_COLOR)
            if tmpl is not None:
                self.quest_blue_template = tmpl
                self.quest_blue_template_gray = cv2.cvtColor(tmpl, cv2.COLOR_BGR2GRAY)
                print(f"[VISION] Loaded Blue Quest template ({tmpl.shape[1]}x{tmpl.shape[0]}) from {QUEST_BLUE_TEMPLATE_PATH}")

    def check_minimap_present(self, frame_bgr: np.ndarray) -> Tuple[bool, float]:
        """State 1 trigger: Detect presence of minimap UI in top-left corner."""
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
        return (edge_density > 0.032), edge_density

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

    def analyze_minimap_road_and_quest(
        self, frame_bgr: np.ndarray
    ) -> Tuple[Optional[float], Optional[float], bool, Optional[str]]:
        """
        Extract quest bearing (Gold AND Blue blips) and segment paths inside circular minimap.
        Returns: (quest_bearing_rad, road_bearing_rad, has_road, blip_type)
        """
        cx, cy = MINIMAP_CENTER
        r = MINIMAP_RADIUS

        mini_roi = frame_bgr[cy - r : cy + r, cx - r : cx + r]
        if mini_roi.size == 0:
            return None, None, False, None

        hsv_mini = cv2.cvtColor(mini_roi, cv2.COLOR_BGR2HSV)

        # 1. Gold Quest Blip: H: 18..38, S: 130..255, V: 175..255
        gold_mask = cv2.inRange(hsv_mini, np.array([18, 130, 175]), np.array([38, 255, 255]))

        # 2. Blue Quest Blip: H: 92..124, S: 85..255, V: 165..255
        blue_mask = cv2.inRange(hsv_mini, np.array([92, 85, 165]), np.array([124, 255, 255]))

        local_circle = np.zeros((2 * r, 2 * r), dtype=np.uint8)
        cv2.circle(local_circle, (r, r), r - 3, 255, -1)

        gold_in_mini = cv2.bitwise_and(gold_mask, local_circle)
        blue_in_mini = cv2.bitwise_and(blue_mask, local_circle)

        quest_bearing = None
        blip_type = None

        # Check Gold blips first
        q_contours, _ = cv2.findContours(gold_in_mini, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for qc in q_contours:
            if 8 <= cv2.contourArea(qc) <= 450:
                M = cv2.moments(qc)
                if M["m00"] > 0:
                    q_local_x = M["m10"] / M["m00"]
                    q_local_y = M["m01"] / M["m00"]
                    dx = q_local_x - r
                    dy = q_local_y - r
                    quest_bearing = math.atan2(dx, -dy)
                    blip_type = "GOLD"
                    break

        # Check Blue blips if no Gold blip found
        if quest_bearing is None:
            b_contours, _ = cv2.findContours(blue_in_mini, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for bc in b_contours:
                if 8 <= cv2.contourArea(bc) <= 450:
                    M = cv2.moments(bc)
                    if M["m00"] > 0:
                        q_local_x = M["m10"] / M["m00"]
                        q_local_y = M["m01"] / M["m00"]
                        dx = q_local_x - r
                        dy = q_local_y - r
                        quest_bearing = math.atan2(dx, -dy)
                        blip_type = "BLUE"
                        break

        # Road segmentation
        road_mask = cv2.inRange(hsv_mini, np.array([12, 15, 155]), np.array([32, 95, 245]))
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

        return quest_bearing, road_bearing, has_road, blip_type

    def detect_combat_red_hp_bars(
        self, frame_bgr: np.ndarray
    ) -> Tuple[bool, int, float, Optional[Tuple[int, int]], bool, Optional[Tuple[int, int, int, int]]]:
        """
        Combat trigger: Strict horizontal HP-bar geometry filtering inside x: 160..1120, y: 120..560.
        Excludes the top sky strip (y < 120) and bottom HUD (y > 560).
        """
        y1, y2, x1, x2 = ROI_COMBAT
        roi = frame_bgr[y1:y2, x1:x2]
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

        mask1 = cv2.inRange(hsv, np.array([0, 185, 175]), np.array([5, 255, 255]))
        mask2 = cv2.inRange(hsv, np.array([175, 185, 175]), np.array([180, 255, 255]))
        red_mask = cv2.bitwise_or(mask1, mask2)

        bar_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (4, 2))
        filtered = cv2.morphologyEx(red_mask, cv2.MORPH_OPEN, bar_kernel)

        contours, _ = cv2.findContours(filtered, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        valid_bars = []

        for c in contours:
            area = cv2.contourArea(c)
            bx, by, bw, bh = cv2.boundingRect(c)

            # Strict horizontal thin-bar geometry
            if 34 <= bw <= 140 and 3 <= bh <= 10:
                aspect_ratio = float(bw) / float(bh)
                if aspect_ratio >= 5.5:
                    extent = float(area) / float(bw * bh)
                    if extent >= 0.65:
                        cx = x1 + bx + bw // 2
                        cy = y1 + by + bh // 2
                        valid_bars.append({
                            "bbox": (x1 + bx, y1 + by, bw, bh),
                            "centroid": (cx, cy),
                            "area": area,
                            "width": bw,
                            "height": bh,
                            "is_elevated": (cy < 290),
                        })

        if not valid_bars:
            return False, 0, 0.0, None, False, None

        valid_bars.sort(key=lambda b: (b["width"], b["area"]), reverse=True)
        best = valid_bars[0]
        return True, len(valid_bars), float(best["area"]), best["centroid"], best["is_elevated"], best["bbox"]

    def detect_sealed_chest_ring(
        self, frame_half: np.ndarray
    ) -> Tuple[bool, Optional[Tuple[int, int, int, int]], Optional[Tuple[int, int]]]:
        """
        Detect glowing red or anemo-teal sealed-chest lock rings.
        Hollow-Ring Geometry: Reject solid flowers, barrels, and slime blobs.
        Fill extent must be between 0.12 and 0.48 (confirming a hollow ring).
        Inside x: 360..920, y: 240..540 (half: x: 180..460, y: 120..270).
        """
        y1, y2, x1, x2 = ROI_SEALED_CHEST_HALF
        roi_half = frame_half[y1:y2, x1:x2]
        if roi_half.size == 0:
            return False, None, None

        hsv_half = cv2.cvtColor(roi_half, cv2.COLOR_BGR2HSV)

        # High saturation red: S >= 195, V >= 180
        red_ring1 = cv2.inRange(hsv_half, np.array([0, 195, 180]), np.array([8, 255, 255]))
        red_ring2 = cv2.inRange(hsv_half, np.array([172, 195, 180]), np.array([180, 255, 255]))
        red_ring = cv2.bitwise_or(red_ring1, red_ring2)

        # High saturation anemo-teal: S >= 180, V >= 180
        teal_ring = cv2.inRange(hsv_half, np.array([82, 180, 180]), np.array([98, 255, 255]))
        ring_mask = cv2.bitwise_or(red_ring, teal_ring)

        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        cleaned = cv2.morphologyEx(ring_mask, cv2.MORPH_OPEN, kernel)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel)

        # Use RETR_CCOMP to identify 2-level hierarchy (outer + hole)
        contours, hierarchy = cv2.findContours(cleaned, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_SIMPLE)
        if contours is None or len(contours) == 0:
            return False, None, None

        for idx, c in enumerate(contours):
            bx, by, bw, bh = cv2.boundingRect(c)
            if bh > 0 and bw > 0:
                aspect_ratio = float(bw) / float(bh)
                if 0.70 <= aspect_ratio <= 1.35 and 16 <= bw <= 90 and 16 <= bh <= 90:
                    patch = cleaned[by : by + bh, bx : bx + bw]
                    pixel_count = cv2.countNonZero(patch)
                    if 50 <= pixel_count <= 2400:
                        extent = float(pixel_count) / float(bw * bh)
                        # Check for inner hole or hollow annulus extent
                        has_hole = (hierarchy is not None and hierarchy[0][idx][2] != -1)
                        is_hollow_extent = (0.12 <= extent <= 0.48)

                        if is_hollow_extent or has_hole:
                            cx_half = x1 + bx + bw // 2
                            cy_half = y1 + by + bh // 2
                            cx_full = cx_half * 2
                            cy_full = cy_half * 2
                            bbox_full = ((x1 + bx) * 2, (y1 + by) * 2, bw * 2, bh * 2)
                            return True, bbox_full, (cx_full, cy_full)

        return False, None, None

    def detect_overworld_f_prompt(
        self, frame_bgr: np.ndarray
    ) -> Tuple[bool, Optional[Tuple[int, int, int, int]]]:
        """
        Harden Overworld [F] Prompt Detection against sunlit rocks:
        Inside ROI_INTERACT = (300, 490, 735, 960), simultaneously verify:
        1. Square bright keycap (18 <= bw <= 36, 18 <= bh <= 36, aspect 0.80..1.25, mean V > 200) on left side (x: 735..830)
        2. Dark translucent pill strip immediately to its right (x+bw+8..x+bw+95, mean V < 95) with bright white label text (V > 210 count >= 18).
        """
        y1, y2, x1, x2 = ROI_INTERACT
        roi = frame_bgr[y1:y2, x1:x2]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        val_roi = hsv_roi[:, :, 2]

        # 1. Detect bright keycap candidate
        _, bright_mask = cv2.threshold(gray, 195, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(bright_mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        for c in contours:
            bx, by, bw, bh = cv2.boundingRect(c)
            # Must be inside left column (x: 735..830) -> in roi: x: 0..95
            if bx <= 95 and 18 <= bw <= 36 and 18 <= bh <= 36:
                aspect = float(bw) / float(bh)
                if 0.80 <= aspect <= 1.25:
                    keycap_patch_v = val_roi[by : by + bh, bx : bx + bw]
                    if keycap_patch_v.size > 0 and float(np.mean(keycap_patch_v)) > 200.0:
                        # 2. Check dark pill strip immediately to the right
                        pill_x1 = bx + bw + 8
                        pill_x2 = min(roi.shape[1], bx + bw + 95)
                        pill_y1 = max(0, by - 4)
                        pill_y2 = min(roi.shape[0], by + bh + 4)

                        if pill_x2 - pill_x1 >= 30 and pill_y2 - pill_y1 >= 14:
                            pill_v = val_roi[pill_y1:pill_y2, pill_x1:pill_x2]
                            mean_pill_v = float(np.mean(pill_v))

                            # Dark translucent background (mean V < 95)
                            if mean_pill_v < 95.0:
                                # Count bright white text pixels (V > 210)
                                white_text_count = int(np.count_nonzero(pill_v > 210))
                                if white_text_count >= 18:
                                    return True, (x1 + bx, y1 + by, bw, bh)

        return False, None

    def _verify_template_hue(self, patch_bgr: np.ndarray, expected_type: str) -> bool:
        """
        Verify target hue AND concentric contrast:
        - Inner core (35% center) must have >= 28% matching hue pixels.
        - Gold: H: 16..40, S: 115..255, V: 165..255
        - Blue: H: 92..124, S: 55..255, V: 135..255
        - Concentric Contrast: Mean V(inner core) - Mean V(outer border ring) >= 25.
        """
        if patch_bgr.size == 0 or patch_bgr.shape[0] < 12 or patch_bgr.shape[1] < 12:
            return False

        ph, pw = patch_bgr.shape[:2]
        hsv = cv2.cvtColor(patch_bgr, cv2.COLOR_BGR2HSV)
        val = hsv[:, :, 2]

        # Define 35% inner core
        cw = max(3, int(pw * 0.35))
        ch = max(3, int(ph * 0.35))
        cx = pw // 2
        cy = ph // 2
        core_y1, core_y2 = cy - ch // 2, cy + ch // 2
        core_x1, core_x2 = cx - cw // 2, cx + cw // 2

        core_hsv = hsv[core_y1:core_y2, core_x1:core_x2]
        core_area = core_hsv.shape[0] * core_hsv.shape[1]
        if core_area == 0:
            return False

        if expected_type == "GOLD":
            mask = cv2.inRange(core_hsv, np.array([16, 115, 165]), np.array([40, 255, 255]))
        elif expected_type == "BLUE":
            mask = cv2.inRange(core_hsv, np.array([92, 55, 135]), np.array([124, 255, 255]))
        else:
            return False

        match_ratio = float(cv2.countNonZero(mask)) / float(core_area)
        if match_ratio < 0.28:
            return False

        # Concentric contrast verification:
        # Inner core mean V vs outer border ring mean V
        v_core_mean = float(np.mean(val[core_y1:core_y2, core_x1:core_x2]))

        # Border mask: everything outside inner 55%
        border_mask = np.ones((ph, pw), dtype=bool)
        bw55 = max(4, int(pw * 0.55))
        bh55 = max(4, int(ph * 0.55))
        border_mask[cy - bh55 // 2 : cy + bh55 // 2, cx - bw55 // 2 : cx + bw55 // 2] = False

        border_v_pixels = val[border_mask]
        if border_v_pixels.size == 0:
            return False

        v_border_mean = float(np.mean(border_v_pixels))
        contrast_diff = v_core_mean - v_border_mean

        # Must have at least 25 units contrast above outer border
        return contrast_diff >= 25.0

    def _is_sky_or_water_background(self, frame_bgr: np.ndarray, cx: int, cy: int) -> bool:
        """Sky & Water Rejection: Inspect surrounding 48x48 box. Reject if > 50% blue."""
        sy1 = max(0, cy - 24)
        sy2 = min(TARGET_HEIGHT, cy + 24)
        sx1 = max(0, cx - 24)
        sx2 = min(TARGET_WIDTH, cx + 24)
        surround = frame_bgr[sy1:sy2, sx1:sx2]
        if surround.size == 0:
            return True

        hsv_surround = cv2.cvtColor(surround, cv2.COLOR_BGR2HSV)
        sky_blue_mask = cv2.inRange(hsv_surround, np.array([85, 55, 110]), np.array([125, 255, 255]))
        blue_pixel_count = cv2.countNonZero(sky_blue_mask)
        total_pixels = surround.shape[0] * surround.shape[1]
        blue_ratio = float(blue_pixel_count) / float(total_pixels)
        return blue_ratio > 0.50

    def track_quest_marker(
        self,
        frame_bgr: np.ndarray,
        config: Dict[str, Any],
        frame_idx: int,
        last_lock: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Tuple[int, int]], Optional[str], Optional[Tuple[int, int, int, int]], float, str]:
        """
        Pure Template-Based Multi-Scale Quest Tracking (v5):
        - Local 120x120 window search every frame if locked; immediate full fallback if missed.
        - Throttled full-screen scan (every 6th frame) when off-screen to preserve >60 FPS.
        - Concentric contrast verification and stamina exclusion zone masking.
        Returns: (centroid, marker_type, bbox, score, scan_mode_label)
        """
        y1, y2, x1, x2 = ROI_NAV
        roi = frame_bgr[y1:y2, x1:x2]
        gray_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        gray_full = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)

        match_thresh = float(config.get("template_match_threshold", 0.64))
        blue_enabled = bool(config.get("blue_marker_enabled", True))

        # Central Stamina Exclusion Zone relative to ROI_NAV
        ey1 = max(0, 240 - y1)
        ey2 = min(roi.shape[0], 540 - y1)
        ex1 = max(0, 520 - x1)
        ex2 = min(roi.shape[1], 760 - x1)

        # ----------------------------------------------------------------------
        # FAST 120x120 LOCAL SEARCH IF CURRENTLY LOCKED
        # ----------------------------------------------------------------------
        if last_lock is not None and last_lock.get("centroid") is not None:
            last_cx, last_cy = last_lock["centroid"]
            last_type = last_lock.get("type", "GOLD")
            tmpl_gray = self.quest_gold_template_gray if last_type == "GOLD" else self.quest_blue_template_gray

            if tmpl_gray is not None:
                tw, th = tmpl_gray.shape[1], tmpl_gray.shape[0]
                half_win = 60
                ly1 = max(100, last_cy - half_win)
                ly2 = min(540, last_cy + half_win)
                lx1 = max(190, last_cx - half_win)
                lx2 = min(1090, last_cx + half_win)

                if (ly2 - ly1) > th and (lx2 - lx1) > tw:
                    local_gray = gray_full[ly1:ly2, lx1:lx2]
                    res = cv2.matchTemplate(local_gray, tmpl_gray, cv2.TM_CCOEFF_NORMED)
                    _, max_val, _, max_loc = cv2.minMaxLoc(res)

                    if max_val >= match_thresh:
                        tx, ty = max_loc
                        tcx = lx1 + tx + tw // 2
                        tcy = ly1 + ty + th // 2
                        if not (520 <= tcx <= 760 and 240 <= tcy <= 540):
                            patch = frame_bgr[ly1 + ty : ly1 + ty + th, lx1 + tx : lx1 + tx + tw]
                            if self._verify_template_hue(patch, last_type):
                                if last_type == "GOLD" or not self._is_sky_or_water_background(frame_bgr, tcx, tcy):
                                    return (tcx, tcy), last_type, (lx1 + tx, ly1 + ty, tw, th), float(max_val), "Track: 120x120"

            # If local tracking missed on this frame, immediately fall through to full scan!
            should_run_full_scan = True
        else:
            # When unlocked, throttle full scan to every 6th frame to keep FPS high
            should_run_full_scan = (frame_idx % 6 == 0)

        if not should_run_full_scan:
            return None, None, None, 0.0, "None"

        # ----------------------------------------------------------------------
        # FULL-ROI MULTI-SCALE SCAN (scales: 0.85, 1.0, 1.15)
        # ----------------------------------------------------------------------
        template_candidates: List[Dict[str, Any]] = []

        # 1. Gold Template Scan
        if self.quest_gold_template_gray is not None:
            for scale in (0.85, 1.0, 1.15):
                tw = int(self.quest_gold_template_gray.shape[1] * scale)
                th = int(self.quest_gold_template_gray.shape[0] * scale)
                if tw < 10 or th < 10 or tw >= roi.shape[1] or th >= roi.shape[0]:
                    continue
                scaled_tmpl = cv2.resize(self.quest_gold_template_gray, (tw, th))
                res = cv2.matchTemplate(gray_roi, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
                res[ey1:ey2, ex1:ex2] = -1.0
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= match_thresh:
                    tx, ty = max_loc
                    tcx = x1 + tx + tw // 2
                    tcy = y1 + ty + th // 2
                    patch = roi[ty : ty + th, tx : tx + tw]
                    if self._verify_template_hue(patch, "GOLD"):
                        template_candidates.append({
                            "type": "GOLD",
                            "score": float(max_val),
                            "centroid": (tcx, tcy),
                            "bbox": (x1 + tx, y1 + ty, tw, th),
                            "scan_mode": "Scan: Full",
                        })
                        break

        # 2. Blue Template Scan
        if blue_enabled and self.quest_blue_template_gray is not None:
            for scale in (0.85, 1.0, 1.15):
                tw = int(self.quest_blue_template_gray.shape[1] * scale)
                th = int(self.quest_blue_template_gray.shape[0] * scale)
                if tw < 10 or th < 10 or tw >= roi.shape[1] or th >= roi.shape[0]:
                    continue
                scaled_tmpl = cv2.resize(self.quest_blue_template_gray, (tw, th))
                res = cv2.matchTemplate(gray_roi, scaled_tmpl, cv2.TM_CCOEFF_NORMED)
                res[ey1:ey2, ex1:ex2] = -1.0
                _, max_val, _, max_loc = cv2.minMaxLoc(res)
                if max_val >= match_thresh:
                    tx, ty = max_loc
                    tcx = x1 + tx + tw // 2
                    tcy = y1 + ty + th // 2
                    patch = roi[ty : ty + th, tx : tx + tw]
                    if self._verify_template_hue(patch, "BLUE") and not self._is_sky_or_water_background(frame_bgr, tcx, tcy):
                        template_candidates.append({
                            "type": "BLUE",
                            "score": float(max_val),
                            "centroid": (tcx, tcy),
                            "bbox": (x1 + tx, y1 + ty, tw, th),
                            "scan_mode": "Scan: Full",
                        })
                        break

        if template_candidates:
            template_candidates.sort(key=lambda item: (1 if item["type"] == "GOLD" else 0, item["score"]), reverse=True)
            best = template_candidates[0]
            return best["centroid"], best["type"], best["bbox"], best["score"], best["scan_mode"]

        return None, None, None, 0.0, "None"

    def detect_party_size(self, frame_bgr: np.ndarray) -> int:
        """Detect active unlocked party size (1 to 4) from the right-hand HUD stack."""
        slot_y_ranges = [
            (180, 245),  # Slot 1
            (255, 320),  # Slot 2
            (330, 395),  # Slot 3
            (405, 470),  # Slot 4
        ]
        active_count = 1

        for slot_idx in range(1, 4):
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

    def calculate_ground_motion_downscaled(
        self, frame_half: np.ndarray, prev_ground_gray_half: Optional[np.ndarray]
    ) -> Tuple[float, np.ndarray]:
        """Frame-differencing motion score on the lower-center ground region on 0.5x frame."""
        y1, y2, x1, x2 = ROI_GROUND_HALF
        roi = frame_half[y1:y2, x1:x2]
        curr_gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)

        if prev_ground_gray_half is None:
            return 10.0, curr_gray

        diff = cv2.absdiff(curr_gray, prev_ground_gray_half)
        motion_score = float(np.mean(diff))
        return motion_score, curr_gray
