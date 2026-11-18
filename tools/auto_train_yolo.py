#!/usr/bin/env python3
"""
Automated Synthetic Dataset Generator & YOLOv8-Nano Trainer for Genshin Impact CV Automation.
Runs 100% executable from the terminal with zero manual bounding-box labeling.
1. Synthesizes 1,500 labeled 640x640 images across 5 classes:
   0: quest_gold
   1: quest_blue
   2: f_prompt
   3: enemy_hp
   4: dialogue_choice
2. Trains YOLOv8n with Ultralytics (XPU/DirectML/CPU).
3. Exports assets/genshin_yolov8n.onnx for low-latency Intel Arc GPU inference.
"""

import argparse
import os
import random
import shutil
import sys
import time
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# Class definitions
CLASS_NAMES = [
    "quest_gold",
    "quest_blue",
    "f_prompt",
    "enemy_hp",
    "dialogue_choice",
]


# ==============================================================================
# 1. Programmatic Sprite Generation & Asset Loading
# ==============================================================================
def load_or_generate_sprites(assets_dir: str = "assets") -> Dict[str, np.ndarray]:
    """
    Loads templates from assets/ or programmatically creates high-fidelity RGBA sprites.
    Returns dict mapping class_name to RGBA image array.
    """
    sprites = {}

    # 1. Quest Gold
    gold_path = os.path.join(assets_dir, "quest_gold.png")
    if os.path.exists(gold_path):
        bgr = cv2.imread(gold_path, cv2.IMREAD_COLOR)
        # Add alpha channel based on brightness/color
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([16, 120, 150]), np.array([36, 255, 255]))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=1)
        rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = mask
        sprites["quest_gold"] = rgba
    else:
        # Programmatic gold diamond (28x28)
        s = 28
        rgba = np.zeros((s, s, 4), dtype=np.uint8)
        pts = np.array([[s // 2, 2], [s - 3, s // 2], [s // 2, s - 3], [2, s // 2]], dtype=np.int32)
        cv2.fillPoly(rgba, [pts], (18, 18, 18, 255))
        inner_pts = np.array([[s // 2, 5], [s - 6, s // 2], [s // 2, s - 6], [5, s // 2]], dtype=np.int32)
        cv2.fillPoly(rgba, [inner_pts], (0, 215, 255, 255))
        sprites["quest_gold"] = rgba

    # 2. Quest Blue
    blue_path = os.path.join(assets_dir, "quest_blue.png")
    if os.path.exists(blue_path):
        bgr = cv2.imread(blue_path, cv2.IMREAD_COLOR)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        mask = cv2.inRange(hsv, np.array([90, 50, 110]), np.array([125, 255, 255]))
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        mask = cv2.dilate(mask, kernel, iterations=1)
        rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        rgba[:, :, 3] = mask
        sprites["quest_blue"] = rgba
    else:
        # Programmatic blue diamond (28x28)
        s = 28
        rgba = np.zeros((s, s, 4), dtype=np.uint8)
        pts = np.array([[s // 2, 2], [s - 3, s // 2], [s // 2, s - 3], [2, s // 2]], dtype=np.int32)
        cv2.fillPoly(rgba, [pts], (18, 18, 18, 255))
        inner_pts = np.array([[s // 2, 5], [s - 6, s // 2], [s // 2, s - 6], [5, s // 2]], dtype=np.int32)
        cv2.fillPoly(rgba, [inner_pts], (255, 190, 0, 255))
        sprites["quest_blue"] = rgba

    # 3. [F] Prompt Keycap + Dark Pill Strip (96x30)
    w_f, h_f = 96, 30
    rgba_f = np.zeros((h_f, w_f, 4), dtype=np.uint8)
    # Dark rounded pill background
    cv2.rectangle(rgba_f, (12, 3), (w_f - 4, h_f - 4), (28, 30, 36, 215), -1)
    cv2.circle(rgba_f, (12, h_f // 2), (h_f - 8) // 2, (28, 30, 36, 215), -1)
    cv2.circle(rgba_f, (w_f - 4, h_f // 2), (h_f - 8) // 2, (28, 30, 36, 215), -1)
    # Keycap square
    cv2.rectangle(rgba_f, (4, 4), (26, 26), (235, 235, 235, 255), -1)
    cv2.rectangle(rgba_f, (4, 4), (26, 26), (150, 150, 150, 255), 1)
    # 'F' text
    cv2.putText(rgba_f, "F", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (20, 20, 20, 255), 2, cv2.LINE_AA)
    # Prompt text ('Talk' or 'Open')
    cv2.putText(rgba_f, "Talk", (34, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.44, (245, 245, 245, 240), 1, cv2.LINE_AA)
    sprites["f_prompt"] = rgba_f

    # 4. Enemy HP Bar (Crimson horizontal health bar, 72x8)
    w_hp, h_hp = 72, 8
    rgba_hp = np.zeros((h_hp, w_hp, 4), dtype=np.uint8)
    # Dark border
    cv2.rectangle(rgba_hp, (0, 0), (w_hp - 1, h_hp - 1), (15, 15, 15, 255), -1)
    # Crimson red bar fill
    cv2.rectangle(rgba_hp, (1, 1), (w_hp - 2, h_hp - 2), (38, 38, 225, 255), -1)
    # Inner gradient / highlight
    cv2.line(rgba_hp, (1, 2), (w_hp - 2, 2), (70, 70, 255, 255), 1)
    sprites["enemy_hp"] = rgba_hp

    # 5. Dialogue Choice Bubble (64x24)
    choice_path = os.path.join(assets_dir, "choice_icon.png")
    if os.path.exists(choice_path):
        bgr = cv2.imread(choice_path, cv2.IMREAD_COLOR)
        rgba = cv2.cvtColor(bgr, cv2.COLOR_BGR2BGRA)
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        rgba[:, :, 3] = np.where(gray > 30, 255, 0).astype(np.uint8)
        sprites["dialogue_choice"] = rgba
    else:
        w_c, h_c = 64, 24
        rgba_c = np.zeros((h_c, w_c, 4), dtype=np.uint8)
        cv2.rectangle(rgba_c, (2, 2), (w_c - 3, h_c - 3), (32, 34, 40, 215), -1)
        # Small dialogue arrow icon
        pts_c = np.array([[8, 6], [16, 12], [8, 18]], dtype=np.int32)
        cv2.fillPoly(rgba_c, [pts_c], (0, 220, 255, 255))
        cv2.line(rgba_c, (22, 12), (w_c - 8, 12), (230, 230, 230, 240), 2)
        sprites["dialogue_choice"] = rgba_c

    return sprites


# ==============================================================================
# 2. Synthetic Game Backgrounds Generator
# ==============================================================================
def get_or_generate_backgrounds(bg_dir: str = "dataset/backgrounds", count: int = 25) -> List[np.ndarray]:
    """
    Returns list of 1280x720 background images. If bg_dir is empty or has few images,
    synthesizes realistic overworld color/texture scenes.
    """
    os.makedirs(bg_dir, exist_ok=True)
    images = []

    # Check for existing backgrounds
    if os.path.exists(bg_dir):
        for fname in os.listdir(bg_dir):
            if fname.lower().endswith((".png", ".jpg", ".jpeg")):
                img_path = os.path.join(bg_dir, fname)
                bgr = cv2.imread(img_path)
                if bgr is not None:
                    if bgr.shape[:2] != (720, 1280):
                        bgr = cv2.resize(bgr, (1280, 720))
                    images.append(bgr)

    if len(images) >= 10:
        return images

    # Synthesize realistic backgrounds matching Mondstadt palettes
    palettes = [
        # Mondstadt Grasslands daytime (grass greens + sky blue)
        ((45, 95, 35), (180, 140, 60)),
        # Sunset horizon (warm amber sky + dark terrain)
        ((25, 45, 110), (50, 120, 220)),
        # Stone cliff / ruins (slate gray + earthy brown)
        ((70, 75, 80), (45, 55, 65)),
        # Twilight / Night overworld (deep navy blue + dark foliage)
        ((35, 25, 15), (75, 45, 25)),
        # Windrise lake (clear blue water + riverbank)
        ((110, 85, 30), (145, 115, 60)),
    ]

    needed = max(15, count - len(images))
    for i in range(needed):
        c_bottom, c_top = random.choice(palettes)
        canvas = np.zeros((720, 1280, 3), dtype=np.uint8)

        # Vertical gradient
        for y in range(720):
            alpha = y / 720.0
            color = [
                int(c_top[ch] * (1.0 - alpha) + c_bottom[ch] * alpha)
                for ch in range(3)
            ]
            canvas[y, :] = color

        # Add multi-scale random terrain texture
        noise = np.random.normal(0, 14, (720, 1280, 3)).astype(np.int16)
        blended = np.clip(canvas.astype(np.int16) + noise, 0, 255).astype(np.uint8)
        blurred = cv2.GaussianBlur(blended, (9, 9), 0)

        # Save to bg_dir for persistence
        save_path = os.path.join(bg_dir, f"synth_bg_{i:03d}.png")
        cv2.imwrite(save_path, blurred)
        images.append(blurred)

    return images


# ==============================================================================
# 3. Synthetic Compositor & Dataset Generator
# ==============================================================================
def generate_synthetic_dataset(
    output_dir: str = "dataset",
    num_images: int = 1500,
    train_ratio: float = 0.80,
    assets_dir: str = "assets",
) -> str:
    """
    Generates 1,500 640x640 labeled images and writes YOLO data.yaml.
    Returns path to data.yaml.
    """
    sprites = load_or_generate_sprites(assets_dir)
    backgrounds = get_or_generate_backgrounds(os.path.join(output_dir, "backgrounds"))

    # Setup directories
    train_img_dir = os.path.join(output_dir, "images", "train")
    val_img_dir = os.path.join(output_dir, "images", "val")
    train_lbl_dir = os.path.join(output_dir, "labels", "train")
    val_lbl_dir = os.path.join(output_dir, "labels", "val")

    for d in [train_img_dir, val_img_dir, train_lbl_dir, val_lbl_dir]:
        os.makedirs(d, exist_ok=True)

    num_train = int(num_images * train_ratio)
    print(f"[DATASET] Generating {num_images} synthetic samples ({num_train} train, {num_images - num_train} val)...")

    # ROI placement guidelines per class (in 640x640 canvas)
    roi_bounds = {
        "quest_gold": (70, 540, 80, 480),       # middle screen
        "quest_blue": (70, 540, 80, 480),       # middle screen
        "f_prompt": (280, 480, 220, 420),       # center-right interaction
        "enemy_hp": (140, 480, 70, 290),        # upper-mid combat zone
        "dialogue_choice": (300, 510, 180, 440), # center-right dialogue
    }

    t0 = time.time()
    for idx in range(num_images):
        is_train = idx < num_train
        img_dir = train_img_dir if is_train else val_img_dir
        lbl_dir = train_lbl_dir if is_train else val_lbl_dir
        fname_base = f"sample_{idx:05d}"

        # Crop random 640x640 patch from a random 1280x720 background
        bg = random.choice(backgrounds)
        x_off = random.randint(0, 1280 - 640)
        y_off = random.randint(0, 720 - 640)
        canvas = bg[y_off : y_off + 640, x_off : x_off + 640].copy()

        # Random global lighting jitter
        gamma = random.uniform(0.85, 1.18)
        inv_gamma = 1.0 / gamma
        table = np.array([((i / 255.0) ** inv_gamma) * 255 for i in np.arange(0, 256)]).astype("uint8")
        canvas = cv2.LUT(canvas, table)

        # Choose 1 to 4 instances to composite
        num_instances = random.randint(1, 4)
        labels = []

        for _ in range(num_instances):
            class_name = random.choice(CLASS_NAMES)
            class_id = CLASS_NAMES.index(class_name)
            sprite = sprites[class_name]

            # Random scaling (0.70x to 1.35x)
            scale = random.uniform(0.70, 1.35)
            sw = max(6, int(sprite.shape[1] * scale))
            sh = max(6, int(sprite.shape[0] * scale))
            resized_sprite = cv2.resize(sprite, (sw, sh), interpolation=cv2.INTER_LINEAR)

            # Random brightness/contrast jitter on sprite
            b_mult = random.uniform(0.80, 1.25)
            sprite_bgr = resized_sprite[:, :, :3].astype(np.float32) * b_mult
            sprite_bgr = np.clip(sprite_bgr, 0, 255).astype(np.uint8)
            alpha = resized_sprite[:, :, 3].astype(np.float32) / 255.0

            # Slight motion blur (25% chance)
            if random.random() < 0.25:
                ksize = random.choice([3, 5])
                sprite_bgr = cv2.GaussianBlur(sprite_bgr, (ksize, ksize), 0)

            # Target position
            min_x, max_x, min_y, max_y = roi_bounds[class_name]
            px = random.randint(min_x, max(min_x + 1, max_x - sw))
            py = random.randint(min_y, max(min_y + 1, max_y - sh))

            # Alpha blend onto canvas
            canvas_roi = canvas[py : py + sh, px : px + sw]
            for c in range(3):
                canvas_roi[:, :, c] = (
                    alpha * sprite_bgr[:, :, c] + (1.0 - alpha) * canvas_roi[:, :, c]
                ).astype(np.uint8)

            # Calculate normalized YOLO bbox: (cx, cy, w, h)
            cx_norm = (px + sw / 2.0) / 640.0
            cy_norm = (py + sh / 2.0) / 640.0
            w_norm = sw / 640.0
            h_norm = sh / 640.0

            labels.append(f"{class_id} {cx_norm:.6f} {cy_norm:.6f} {w_norm:.6f} {h_norm:.6f}")

        # Save image and label
        img_path = os.path.join(img_dir, f"{fname_base}.jpg")
        lbl_path = os.path.join(lbl_dir, f"{fname_base}.txt")

        cv2.imwrite(img_path, canvas, [cv2.IMWRITE_JPEG_QUALITY, 94])
        with open(lbl_path, "w", encoding="utf-8") as f:
            f.write("\n".join(labels) + "\n")

        if (idx + 1) % 250 == 0 or (idx + 1) == num_images:
            elapsed = time.time() - t0
            print(f"[DATASET] Generated {idx + 1}/{num_images} images ({elapsed:.1f}s)...")

    # Write data.yaml
    data_yaml_path = os.path.abspath(os.path.join(output_dir, "data.yaml"))
    abs_out_dir = os.path.abspath(output_dir).replace("\\", "/")
    yaml_content = f"""path: {abs_out_dir}
train: images/train
val: images/val
names:
  0: quest_gold
  1: quest_blue
  2: f_prompt
  3: enemy_hp
  4: dialogue_choice
"""
    with open(data_yaml_path, "w", encoding="utf-8") as f:
        f.write(yaml_content)

    print(f"[DATASET] Dataset generation complete! Configuration saved to {data_yaml_path}")
    return data_yaml_path


# ==============================================================================
# 4. YOLOv8 Training & ONNX Export (Intel Arc GPU / DirectML / CPU)
# ==============================================================================
def train_and_export_yolo(
    data_yaml: str,
    epochs: int = 35,
    imgsz: int = 640,
    batch_size: int = 16,
    output_onnx: str = "assets/genshin_yolov8n.onnx",
) -> str:
    """
    Trains YOLOv8n with Ultralytics and exports assets/genshin_yolov8n.onnx.
    Auto-detects Intel Arc XPU, CUDA, or CPU device.
    """
    try:
        from ultralytics import YOLO
        import torch
    except ImportError:
        print("[TRAIN ERROR] ultralytics and torch must be installed to run training.", file=sys.stderr)
        sys.exit(1)

    # Detect compute device
    device = "cpu"
    if hasattr(torch, "xpu") and torch.xpu.is_available():
        device = "xpu"
        print("[TRAIN] Using Intel PyTorch XPU (Intel Arc GPU)!")
    elif torch.cuda.is_available():
        device = "0"
        print("[TRAIN] Using CUDA GPU!")
    else:
        print("[TRAIN] Using CPU for training.")

    print(f"[TRAIN] Loading base model 'yolov8n.pt' for {epochs} epochs (imgsz={imgsz}, batch={batch_size})...")
    model = YOLO("yolov8n.pt")

    # Train
    model.train(
        data=data_yaml,
        epochs=epochs,
        imgsz=imgsz,
        batch=batch_size,
        device=device,
        workers=2,
        val=True,
        plots=False,
    )

    print(f"[TRAIN] Training complete! Exporting to ONNX format (opset=12, simplify=True)...")
    exported_path = model.export(format="onnx", imgsz=imgsz, opset=12, simplify=True)

    # Move/copy exported model to output_onnx
    os.makedirs(os.path.dirname(output_onnx) or ".", exist_ok=True)
    shutil.copy2(exported_path, output_onnx)
    print(f"[TRAIN] ONNX model successfully exported and saved to {output_onnx}!")

    return output_onnx


# ==============================================================================
# CLI Entry Point
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(
        description="Automated Synthetic Dataset Generator & YOLOv8-Nano ONNX Trainer"
    )
    parser.add_argument("--num-images", type=int, default=1500, help="Number of synthetic images (default: 1500)")
    parser.add_argument("--epochs", type=int, default=35, help="Number of training epochs (default: 35)")
    parser.add_argument("--batch-size", type=int, default=16, help="Batch size (default: 16)")
    parser.add_argument("--generate-only", action="store_true", help="Generate dataset without training")
    parser.add_argument("--train-only", action="store_true", help="Train on existing dataset without regenerating")
    parser.add_argument("--output-onnx", type=str, default="assets/genshin_yolov8n.onnx", help="Output ONNX path")

    args = parser.parse_args()

    data_yaml = os.path.abspath("dataset/data.yaml")

    if not args.train_only:
        data_yaml = generate_synthetic_dataset(output_dir="dataset", num_images=args.num_images)

    if not args.generate_only:
        train_and_export_yolo(
            data_yaml=data_yaml,
            epochs=args.epochs,
            batch_size=args.batch_size,
            output_onnx=args.output_onnx,
        )


if __name__ == "__main__":
    main()
