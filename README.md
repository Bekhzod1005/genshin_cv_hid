# Genshin Impact Hardware-Assisted CV Automation (v3)

A production-grade, self-contained computer-vision navigation, dialogue, chest-looting, and **starter-roster auto-combat** assistant for **Genshin Impact** running on Windows (1280x720 Borderless or Windowed mode). 

Direct screen capture is handled via Python (`mss` + OpenCV), and all keyboard and mouse inputs are generated physically through an **ESP32-S3** microcontroller emulating a native USB HID composite device (TinyUSB Keyboard + Mouse).

```
 ┌─────────────────────────────────────────────────────────────┐
 │                      WINDOWS HOST PC                        │
 │                                                             │
 │   ┌──────────────────┐               ┌──────────────────┐   │
 │   │  Genshin Impact  │               │   Python OpenCV  │   │
 │   │ (1280x720 Window)│               │  4-State Machine │   │
 │   └────────┬─────────┘               └────────┬─────────┘   │
 │            │ (mss direct capture)             │             │
 │            ▼                                  │             │
 │   ┌──────────────────┐                        │             │
 │   │ Win32 Desktop    │                        │             │
 │   │ Client Geometry  │────────────────────────┘             │
 └────────────┼────────────────────────────────────────────────┘
              │                                  ▲
              │ Virtual Serial (COMx)            │ Physical USB HID
              │ "1"-"4", "E", "Q", "R", "LMB",   │ Keystrokes & Cursor
              │ "RMB", "SPACE", "W1", "STOP", ...│
              ▼                                  │
 ┌───────────────────────────────────────────────┴─────────────┐
 │            ESP32-S3 MICROCONTROLLER (Native USB OTG)        │
 │                                                             │
 │   [CDC Serial Port] ───► [Command Parser] ───► [TinyUSB HID]│
 └─────────────────────────────────────────────────────────────┘
```

---

## Key Features & Safety Architecture

1. **Hardware-Assisted Physical Input:** All keystrokes and mouse movements originate from the ESP32-S3 hardware via native USB HID descriptors with randomized hold durations (48–112 ms). No software synthetic input injection (`SendInput`, `mouse_event`) touches Windows.
2. **Active-Window Focus Lock:** Continuously checks `win32gui.GetForegroundWindow() == hwnd`. If Genshin Impact loses focus, the assistant immediately sends `STOP` and suspends automation to prevent typing or clicking in other applications.
3. **Global `F12` Instant Pause Toggle:** Monitored system-wide via `pynput`. Pressing `F12` at any time immediately disengages inputs, releases all keys/buttons, and clears combat and navigation queues.
4. **Strict State Hierarchy & Arbitration (`DIALOGUE > COMBAT_AUTO > CHEST_LOOT > NAVIGATE`):**
   - **State 1 — Dialogue Mode (`DIALOGUE` - Top Priority):** When the top-left minimap is hidden, the system locks into dialogue mode. Any held combat or movement keys (`W0`, `LMB_REL`) are immediately released. Rapidly advances subtitles and menu choices via natural log-normal cadences (0.38s–0.78s) without triggering false combat or navigation actions.
   - **State 2 — Starter-Roster Auto-Combat (`COMBAT_AUTO` & `COMBAT_MANUAL`):**
     - **Immediate Stop:** Halts walking (`W0`) upon engaging targets to avoid sprinting past enemies.
     - **Dynamic Party-Size Awareness:** Automatically detects active party size (1 to 4 slots) from the right-hand HUD stack, preventing invalid keypresses on new accounts (e.g. only pressing `1` and `2` before unlocking Kaeya/Lisa).
     - **Deliberate Pacing:** 3-hit `LMB` attack combos spaced 0.32s–0.58s with natural pauses or `RMB` dodges; `E` skills (6.5s–8.5s); `Q` bursts (12.0s–15.0s); character rotations paced every 7.0s–10.0s.
     - **Amber Bow Aim-Shot Mode:** Automatically swaps to Amber (slot 2) and enters Aim Mode (`R`) when fighting elevated/tower enemies or when melee damage stalls (> 4.0s). Tracks reticle with 2-axis mouse steering (`M<dx>,<dy>`) and looses charged Pyro shots (`LMB_HOLD` 1.6s).
     - **Combat Lock:** Prevents navigation sweeps and walking until all enemy HP bars are cleared for >= 1.5s.
     - **Manual Fallback:** After 45s of unresolved combat, alerts the player and yields control.
   - **State 3 — Chest Looting & Multi-Second Vacuum (`CHEST_LOOT`):**
     - **High-Confidence Sealed Ring Detection:** Detects only high-saturation glowing crimson (`H: 0..8` / `172..180`, `S: 175..255`, `V: 170..255`) or anemo-teal (`H: 82..98`, `S: 160..255`, `V: 170..255`) circular lock rings in a tighter mid-screen ROI (`x: 300..980, y: 220..560`) with aspect ratio filtering (`0.70..1.35`, area `350..3200 px`). Broad terrain/brown color guessing has been removed to eliminate false positives on Mondstadt dirt paths and rocks.
     - **Multi-Second Loot Vacuum:** Upon opening a chest or triggering an overworld `[F]` prompt, halts movement (`W0`), waits 0.65s for the chest lid opening animation and scattered drops to hit the ground, and enters a 2.2s–2.8s vacuum burst tapping `F` every 0.16s–0.24s (10–14 taps total) while executing a gentle left/right camera wiggle (`M-18,0` $\rightarrow$ `M36,0` $\rightarrow$ `M-18,0`). Continues draining stacked item drop lists until the `[F]` prompt box is cleared.
   - **State 4 — Overworld Navigation (`NAVIGATE`):**
     - **Hybrid Gold & Blue Quest Marker Tracking:** Detects both Gold (Archon / Story quests) and Blue World Quest markers. World Quest markers are validated via their authentic concentric signature: **Dark Charcoal/Navy Border (`V < 145`) $\rightarrow$ Slate-Blue/Periwinkle Outer Ring (`H: 96..122, S: 65..220, V: 120..255`) $\rightarrow$ Glowing Light-Cyan Core (`H: 85..112, S: 40..170, V: 215..255`)**, combined with multi-scale template matching (`assets/quest_gold.png` & concentric `assets/quest_blue.png`) and sky/water rejection ($> 50\%$ blue pixels in surrounding $48 \times 48$ box).
     - **Stamina & Character Exclusion Zone:** Automatically masks out `x: 520..760, y: 240..540` from quest detection so the curved yellow stamina arc next to Lumine never biases the marker centroid.
     - **Hard Center Lock:** When the marker is centered ($|error\_x| \le 28$ px), camera steering is strictly zeroed (`dx = 0`) and minimap road bias is suppressed to eliminate post-lock camera drift. Road-following bias is only applied when the marker is distant/off-center ($|error\_x| > 45$ px).
     - **Lost-Marker Recovery (Pulse 'V' First):** If neither a gold nor blue marker is detected for $> 0.75$s, dispatches `V` (4.5s cooldown) to re-illuminate the navigation trail. Waits 0.9s (total $> 1.65$s); only if the marker remains undetected does it halt walking (`W0`) and begin slow horizontal camera sweeps (`M<dx>`).
     - **Minimap Road-Following:** Segments dirt roads and paths within the circular minimap, calculating path centerline bearing. Biases movement towards roads when within $\pm 55^\circ$ of quest target.
     - **Smoothed Camera Steering:** Camera mouse steering uses Exponential Moving Average (EMA, $\alpha=0.35$) with rate limiting (max 15–20 Hz) to eliminate micro-stutter.
     - **Hard Turn Pausing:** Stops walking (`W0`) during sharp turns ($|error\_x| > 160$ px) to prevent wide drifting.
     - **Obstacle Jumping & Unstuck Logic:** Frame differencing on the player feet region detects terrain obstacles. Automatically triggers `SPACE` jump after 1.8s of stall, followed by lateral evasion if stuck persists.
     - **Overworld [F] Prompt Interaction:** Interacts with Waypoints, Statues, items, and NPCs.
5. **Firmware Hardware TRNG & Extended Watchdog:** ESP32-S3 uses true hardware random numbers (`esp_random()`) and a 15-second watchdog timer, complemented by 4-second Python navigation keepalive pings.
6. **Live HSV & Gain Tuner with Instant Hardcoded Reset:** Real-time OpenCV sliders for `H_min`, `H_max`, `S_min`, `S_max`, `V_min`, `V_max`, and proportional steering gain `Kp_x100`, saved to `config.json` with the `S` key. Includes an instant **`Reset [0=No, 1=Reset]`** trackbar and **`D`** hotkey to restore icon-calibrated defaults immediately.
7. **Capture & Processing FPS Optimization:** Computes optical flow / unstuck differencing and sealed-chest ring masks on a downscaled `0.5x` frame (`640x360`), runs unstuck motion checks only every 3rd frame (`frame_idx % 3 == 0`), and throttles `cv2.imshow` GUI rendering to max 20 FPS while keeping the direct screen capture and HID control loop executing at full hardware speed.

---

## Project Structure

```
genshin_cv_hid/
├── assets/
│   ├── .gitkeep              # Optional templates (minimap_border.png, choice_icon.png)
├── config.json               # Persisted HSV thresholds, EMA alpha, and steering gain
├── firmware/
│   └── esp32s3_hid/
│       └── esp32s3_hid.ino   # Arduino TinyUSB HID + CDC Serial sketch (v3)
├── app/
│   ├── __init__.py
│   ├── main.py               # Main CV capture, auto-combat & live tuner
│   └── test_hid.py           # Standalone serial-to-HID verification utility
├── requirements.txt
└── README.md
```

---

## Hardware Requirements & Wiring

- **Microcontroller:** Any **ESP32-S3** development board (e.g., `ESP32-S3-DevKitC-1`, `Waveshare ESP32-S3`, or `NodeMCU ESP32-S3`).
- **USB Cable:** A standard USB Type-C data cable.
- **Port Connection:** Connect your cable to the port labeled **"USB"** (Native USB OTG connected directly to GPIO 19 `D-` and GPIO 20 `D+`), **NOT** the secondary port labeled "UART" / "COM" (which is bridged via CP2102/CH340).

---

## Arduino IDE Board Configuration & Flashing

1. Open **Arduino IDE** (version 2.0+ recommended).
2. Install the official ESP32 board package by Espressif:
   - Go to **File > Preferences**.
   - Add this URL to *Additional Boards Manager URLs*:
     ```
     https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
     ```
   - Go to **Tools > Board > Boards Manager**, search for `esp32`, and install version `2.0.14` or later (or `3.x`).
3. Open `firmware/esp32s3_hid/esp32s3_hid.ino`.
4. Configure the **Tools** menu settings:
   - **Board:** `ESP32S3 Dev Module`
   - **USB CDC On Boot:** `Enabled`  *(Critical: allows Serial commands over native USB port)*
   - **USB Mode:** `USB-OTG (TinyUSB)` *(Critical: enables native HID Keyboard + Mouse)*
   - **USB Firmware MSC On Boot:** `Disabled`
   - **Upload Mode:** `UART0 / Hardware CDC` (or `USB-OTG` depending on your board revision)
   - **Flash Size:** `4MB` (or matching your board module)
5. Select the COM port corresponding to your ESP32-S3 board.
6. Click **Upload**.
7. Once flashing is complete, disconnect and reconnect the USB cable once so Windows re-enumerates the board as a composite CDC Serial + USB HID Keyboard & Mouse device.

---

## Serial Command Protocol

| Command | Action | Hold Duration |
|---|---|---|
| `PING` | Handshake verification (returns `PONG\n`) | Instant |
| `1`, `2`, `3`, `4` | Select party member slot 1 to 4 | 48–112 ms (Randomized) |
| `E` | Cast Elemental Skill | 48–112 ms (Randomized) |
| `Q` | Cast Elemental Burst | 48–112 ms (Randomized) |
| `R` | Toggle Bow Aiming Mode | 45–85 ms (Randomized) |
| `LMB` | Normal Attack (Left Mouse Click) | 48–112 ms (Randomized) |
| `LMB_HOLD` | Begin Charged Attack (Hold Left Mouse Button) | Continuous until `LMB_REL` |
| `LMB_REL` | Finish Charged Attack (Release Left Mouse Button) | Instant |
| `RMB` | Sprint Dash / Dodge (Right Mouse Click) | 48–112 ms (Randomized) |
| `SPACE` | Jump / Glide / Unfreeze | 48–112 ms (Randomized) |
| `F` | Interact / Dialogue Option / Chest Loot | 48–112 ms (Randomized) |
| `V` | Quest Navigation Trail | 48–112 ms (Randomized) |
| `W1` | Continuous forward walk | Continuous until `W0`/`STOP` |
| `W0` / `STOP` | Emergency stop: releases all keys and mouse buttons | Instant |
| `M<dx>` | Relative horizontal mouse move (`[-50, 50]`) | Instant |
| `M<dx>,<dy>` | 2-axis relative mouse move (`dx, dy in [-50, 50]`) | Instant |

---

## Software Installation & Environment Setup

### 1. Python Environment Setup
Open PowerShell or Command Prompt:

```powershell
# Navigate into the project folder
cd c:\Users\BKhas\OneDrive\Desktop\genshin_cv_hid

# Create a virtual environment (recommended)
python -m venv venv
.\venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Verify Hardware with `app/test_hid.py`
Before launching the full computer vision system, verify your ESP32-S3 serial communication and physical input generation:

```powershell
# Run the test utility on your ESP32-S3 COM port (e.g. COM4)
python app/test_hid.py --port COM4
```

In the interactive menu:
- Option `1`: Sends `PING` to verify bidirectional serial communication (`PONG`).
- Option `2`: Dispatches Dialogue `'F'` and Quest `'V'`.
- Option `3`: Cycles character slots `1 -> 2 -> 3 -> 4`.
- Option `4`: Dispatches combat skills `'E'` and `'Q'`.
- Option `5`: Tests Left Mouse Click (`LMB`) and Right Mouse Click (`RMB`).
- Option `6`: Tests Charged Attack (`LMB_HOLD` for 0.6s, then `LMB_REL`).
- Option `7`: Dispatches Jump (`SPACE`).
- Option `8`: Tests Walk Hold `W1` (1.5s) -> `W0`.
- Option `9`: Tests horizontal camera steering (`M<dx>`).
- Option `r`: Tests Bow Aim Mode toggle (`R`).
- Option `m`: Tests 2-axis relative mouse steering (`M<dx>,<dy>`).
- Option `a`: Runs the full automated verification suite with a 3-second countdown.

---

## Game Configuration (Genshin Impact)

1. **Resolution & Display Mode:**
   - Open Genshin Impact settings: **Graphics > Display Mode**.
   - Select **1280x720 Borderless** or **1280x720 Windowed**.
2. **Keybindings:**
   - Standard default keybindings:
     - `W`: Move Forward
     - `1`, `2`, `3`, `4`: Party Members 1 to 4
     - `E`: Elemental Skill
     - `Q`: Elemental Burst
     - `R`: Aiming Mode
     - `Left Click`: Normal / Charged Attack
     - `Right Click`: Dash / Sprint
     - `Space`: Jump
     - `F`: Interact / Pick Up / Dialogue Choice
     - `V`: Navigate / Highlight Quest Path

---

## Running the Assistant

### Live Hardware Mode
```powershell
python app/main.py --port COM4
```

### Dry-Run Mode (Testing without Hardware)
Runs screen capture, the live HSV tuner, and state machine preview with no serial connection required:
```powershell
python app/main.py --dry-run
```

---

## Controls & Hotkeys

| Hotkey | Context | Action |
|---|---|---|
| **`F12`** | Global (Any Window) | **Instant Pause / Resume Toggle**. Immediately releases all held keys (`STOP`), clears queues, and suspends automation. |
| **`T`** | Debug Window | **Capture Active Diamond Template**. Crops a 28x28 patch around the tracked marker and saves to `assets/quest_gold.png` or `assets/quest_blue.png`. |
| **Mouse Click** | Debug Window | **Crop Template At Cursor**. Left-click on any quest diamond on the live debug window to instantly save a 28x28 reference template. |
| **`D`** | Debug Window | **Reset Tuner to Calibrated Defaults**. Restores hardcoded in-game calibrated HSV thresholds and `Kp` gain, and updates `config.json`. |
| **Reset Trackbar** | Tuner Window | **One-Click Tuner Reset**. Move the `Reset [0=No, 1=Reset]` trackbar to `1` to restore hardcoded defaults. |
| **`S`** | Debug Window | **Save Configuration**. Saves current Tuner HSV thresholds, EMA alpha, and `Kp_x100` gain to `config.json`. |
| **`Q`** / **`ESC`** | Debug Window | **Clean Shutdown**. Issues emergency `STOP` command to ESP32-S3, closes serial, and destroys OpenCV windows. |

---

## Troubleshooting

- **ESP32-S3 Not Detected / No COM Port:**
  Ensure you are plugged into the **native USB** port on the board. In Windows Device Manager, check under *Ports (COM & LPT)*. If marked with a yellow triangle, reinstall the ESP32 USB CDC driver or press the `BOOT` + `RESET` buttons on the board to enter download mode.
- **Combat Timing Out at 45s:**
  If an enemy is shielded or obstructed, the 45s safety cutoff triggers and beeps, allowing manual takeover. Once cleared, the assistant automatically resumes `NAVIGATE`.
- **Road or Quest Tuning:**
  Use the live Tuner window sliders to adjust HSV filter bounds and steering gain in real-time, then press **`S`** to persist to `config.json`.
