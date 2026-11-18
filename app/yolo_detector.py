#!/usr/bin/env python3
"""
YOLOv8-Nano ONNX Runtime Wrapper for Genshin Impact UI & Marker Detection.
Runs high-performance inference (< 5ms) on Intel Arc GPU via DirectML / OpenVINO / CPU.
Detects 5 synthetic classes:
  0: quest_gold
  1: quest_blue
  2: f_prompt
  3: enemy_hp
  4: dialogue_choice
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

CLASS_NAMES = [
    "quest_gold",
    "quest_blue",
    "f_prompt",
    "enemy_hp",
    "dialogue_choice",
]

CLASS_COLORS = {
    0: (0, 215, 255),    # Gold (BGR)
    1: (255, 180, 0),    # Cyan/Blue (BGR)
    2: (255, 255, 255),  # White [F] Prompt
    3: (0, 0, 255),      # Red HP Bar
    4: (0, 230, 255),    # Dialogue Choice
}


class YoloUIWrapper:
    """
    Lightweight ONNX Runtime inference wrapper for YOLOv8n UI detection.
    Optimized for Intel Arc GPU with DirectML execution provider.
    """

    def __init__(
        self,
        model_path: Optional[str] = None,
        conf_threshold: float = 0.35,
        iou_threshold: float = 0.45,
        logger: Optional[Any] = None,
    ):
        self.logger = logger
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.model_path = model_path or os.path.join("assets", "genshin_yolov8n.onnx")
        self.session = None
        self.input_name = None
        self.output_name = None
        self.input_shape = (640, 640)
        self.is_ready = False
        self.active_provider = "None"

        self._init_session()

    def _init_session(self):
        """Loads ONNX model session if model file exists."""
        if not os.path.exists(self.model_path):
            return

        try:
            import onnxruntime as ort

            available = ort.get_available_providers()
            preferred = [
                "DmlExecutionProvider",       # Windows DirectML (Intel Arc GPU)
                "OpenVINOExecutionProvider",  # Intel OpenVINO
                "CPUExecutionProvider",       # Fallback CPU
            ]
            selected = [p for p in preferred if p in available]
            if not selected:
                selected = ["CPUExecutionProvider"]

            # Session options for fast inference
            sess_options = ort.SessionOptions()
            sess_options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
            sess_options.log_severity_level = 3  # Errors only

            self.session = ort.InferenceSession(self.model_path, sess_options, providers=selected)
            self.input_name = self.session.get_inputs()[0].name
            self.output_name = self.session.get_outputs()[0].name
            self.active_provider = self.session.get_providers()[0]
            self.is_ready = True

            msg = f"Initialized YOLOv8n ONNX model '{self.model_path}' with provider '{self.active_provider}'"
            print(f"[YOLO] {msg}")
            if self.logger:
                self.logger.log_event("YOLO", msg)

        except Exception as e:
            print(f"[YOLO WARN] Failed to initialize ONNX session: {e}", file=sys.stderr)
            self.is_ready = False

    def infer(self, frame_bgr: np.ndarray) -> List[Dict[str, Any]]:
        """
        Runs YOLOv8-Nano inference on a 1280x720 frame.
        Returns list of detection dictionaries:
          {
            "class_id": int,
            "class_name": str,
            "confidence": float,
            "box": (x, y, w, h),
            "centroid": (cx, cy)
          }
        """
        if not self.is_ready or self.session is None or frame_bgr is None:
            return []

        orig_h, orig_w = frame_bgr.shape[:2]
        target_w, target_h = self.input_shape

        # Preprocessing: Resize/Letterbox to 640x640, BGR -> RGB, [0, 1] float32
        scale_x = orig_w / float(target_w)
        scale_y = orig_h / float(target_h)

        resized = cv2.resize(frame_bgr, (target_w, target_h), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
        blob = np.transpose(rgb, (2, 0, 1)).astype(np.float32) / 255.0
        blob = np.expand_dims(blob, axis=0)

        # Run inference
        try:
            outputs = self.session.run([self.output_name], {self.input_name: blob})
            raw_output = outputs[0]  # shape: (1, 4 + num_classes, 8400) or (1, 8400, 4 + num_classes)
        except Exception as e:
            print(f"[YOLO ERROR] Inference failed: {e}", file=sys.stderr)
            return []

        if raw_output.shape[1] == (4 + len(CLASS_NAMES)):
            # Transpose (1, 9, 8400) -> (1, 8400, 9)
            predictions = np.transpose(raw_output[0], (1, 0))
        else:
            predictions = raw_output[0]

        boxes = []
        confidences = []
        class_ids = []

        # Parse predictions
        for pred in predictions:
            cx, cy, w, h = pred[:4]
            scores = pred[4:]
            class_id = int(np.argmax(scores))
            conf = float(scores[class_id])

            if conf >= self.conf_threshold:
                # Convert from 640x640 to original frame coords
                bx = int((cx - w / 2.0) * scale_x)
                by = int((cy - h / 2.0) * scale_y)
                bw = int(w * scale_x)
                bh = int(h * scale_y)

                # Clamp to frame bounds
                bx = max(0, min(orig_w - 1, bx))
                by = max(0, min(orig_h - 1, by))
                bw = max(1, min(orig_w - bx, bw))
                bh = max(1, min(orig_h - by, bh))

                boxes.append([bx, by, bw, bh])
                confidences.append(conf)
                class_ids.append(class_id)

        if not boxes:
            return []

        # Non-Maximum Suppression (NMS)
        indices = cv2.dnn.NMSBoxes(boxes, confidences, self.conf_threshold, self.iou_threshold)
        detections = []

        if len(indices) > 0:
            for idx in indices.flatten():
                bx, by, bw, bh = boxes[idx]
                cid = class_ids[idx]
                conf = confidences[idx]
                cname = CLASS_NAMES[cid] if cid < len(CLASS_NAMES) else f"class_{cid}"
                centroid = (bx + bw // 2, by + bh // 2)

                detections.append({
                    "class_id": cid,
                    "class_name": cname,
                    "confidence": conf,
                    "box": (bx, by, bw, bh),
                    "centroid": centroid,
                })

        return detections

    def get_quest_marker(self, detections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Filter for highest confidence 3D quest diamond (gold or blue)."""
        candidates = [d for d in detections if d["class_name"] in ("quest_gold", "quest_blue")]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d["confidence"])

    def get_f_prompt(self, detections: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """Filter for highest confidence overworld [F] prompt."""
        candidates = [d for d in detections if d["class_name"] == "f_prompt"]
        if not candidates:
            return None
        return max(candidates, key=lambda d: d["confidence"])

    def get_enemy_hp_bars(self, detections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter for enemy HP bars."""
        return [d for d in detections if d["class_name"] == "enemy_hp"]

    def get_dialogue_choices(self, detections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Filter for dialogue choice bubbles."""
        return [d for d in detections if d["class_name"] == "dialogue_choice"]

    def draw_detections(self, frame: np.ndarray, detections: List[Dict[str, Any]]) -> np.ndarray:
        """Draws bounding boxes, confidence tags, and centroids on a frame."""
        for d in detections:
            bx, by, bw, bh = d["box"]
            cid = d["class_id"]
            cname = d["class_name"]
            conf = d["confidence"]
            color = CLASS_COLORS.get(cid, (0, 255, 0))

            cv2.rectangle(frame, (bx, by), (bx + bw, by + bh), color, 2)
            label = f"{cname} {conf:.2f}"
            t_size, _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
            cv2.rectangle(frame, (bx, by - t_size[1] - 4), (bx + t_size[0] + 4, by), color, -1)
            cv2.putText(
                frame, label, (bx + 2, by - 2),
                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 1, cv2.LINE_AA
            )
            cv2.circle(frame, d["centroid"], 3, color, -1)

        return frame
