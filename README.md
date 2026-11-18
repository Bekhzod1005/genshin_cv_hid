# Genshin Impact Hardware-Assisted CV Automation (v5 Modular Architecture)

A production-grade, self-contained computer-vision navigation, dialogue, chest-looting, and **starter-roster auto-combat** assistant for **Genshin Impact** running on Windows (1280x720 Borderless or Windowed mode). 

Direct screen capture is handled via Python (`mss` + OpenCV), and all keyboard and mouse inputs are generated physically through an **ESP32-S3** microcontroller emulating a native USB HID composite device (TinyUSB Keyboard + Mouse) with a non-blocking hardware scheduler and high-speed micro-stepped camera rotation.

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
              │ "1"-"4", "E", "Q", "R", "LMB",   │ Micro-Stepped Mouse &
              │ "RMB", "SPACE", "W1", "STOP", ...│ Non-Blocking Keystrokes
              ▼                                  │
 ┌───────────────────────────────────────────────┴─────────────┐
 │            ESP32-S3 MICROCONTROLLER (Native USB OTG)        │
 │                                                             │
 │   [512B RX Buffer] ──► [Zero-Delay Parser] ──► [TinyUSB HID]│
 │          ▲                                           │      │
 │          └────────── [Non-Blocking millis Queue] ────┘      │
 └─────────────────────────────────────────────────────────────┘
```

---

## v5.1 Architecture & Runtime Regression Overhaul

1. **Robust 3D Quest Marker Locking (Hybrid Template + Direct Diamond Detection):**
   - **Deleted Broken Border Contrast Check:** Removed the fragile $V_{core} - V_{border} \ge 25$ outer-border subtraction check that failed in bright daytime Mondstadt overworld conditions where sky, grass, and clouds have $V \ge 210$.
   - **Inner $18 \times 18$ Cropping:** Loaded templates (`assets/quest_gold.png` and `assets/quest_blue.png`) are automatically center-cropped to $18 \times 18$ at runtime, stripping away static outer background corners while keeping on-disk PNGs untouched.
   - **Direct Diamond HSV + Dark Outline Detector:** Runs in parallel with template matching ($\ge 0.50$):
     - **Gold Core:** `H: 17..38, S: 135..255, V: 185..255`, bounding box $8 \le w, h \le 28$, aspect $0.72..1.38$.
     - **Blue Core:** `H: 90..122, S: 60..255, V: 145..255` with cyan center ($V \ge 210$) and sky/water rejection ($< 45\%$ blue in $44 \times 44$ outer ring).
     - **Dark Outline Verification:** Verifies that $\ge 15\%$ of pixels in a $2\text{--}3$ px dilated outline ring have $V < 140$, rejecting flat terrain and foliage.
     - Central stamina arc exclusion zone ($x: 520..760, y: 240..540$) is strictly masked out.
2. **Eliminated Idle Left Rotation & Minimap False Positives:**
   - **Annulus Mask & Pure Gold Minimap Blips:** Removed Blue blips completely from minimap analysis to eliminate water/waypoint/player-arrow false positives. Evaluates only the $18 \le r \le 58$ annulus, rejecting the central player arrow ($r < 18$) and outer UI border ($r > 58$). Only reports `quest_bearing` for isolated Gold minimap diamonds (`H: 19..33, S: 175..255, V: 205..255`, area $10..65$ px, $w, h \le 12$).
   - **4-Phase Non-Endless Lost-Marker Recovery:**
     - **Phase 1 (Wait Initial):** Immediately halts forward walk (`W0`) and stops camera movement (`dx = 0`) for $0.8$s.
     - **Phase 2 (Pulse V):** Pulses `V` once ($5.0$s cooldown) and waits $1.0$s completely stationary (`dx = 0, W0`) for the trail or marker to appear.
     - **Phase 3 (Discrete Step Sweep):** Executes at most 8 discrete step turns (`M32,0` every $0.28$s, pausing between steps).
     - **Phase 4 (Sweep Complete):** Halts camera rotation completely (`dx = 0, W0`) and remains idle until a marker appears or the $5.0$s `V` pulse cooldown expires. Never spins endlessly.
3. **Context-Aware `[F]` Prompt Arbitration (Prevent Random NPC Dialogue):**
   - In `NAVIGATE`, arbitrary overworld `[F]` prompts (NPCs, cooking pots, signposts) no longer trigger loot vacuums or keypresses.
   - Interacts with `[F]` in only two verified situations:
     - **Quest Target Arrival:** 3D marker is locked (or locked within last $1.5$s) near center ($|error\_x| \le 95$ px) AND `[F]` prompt appears $\rightarrow$ single `F` tap ($1.2$s cooldown, not vacuum).
     - **Post-Combat / Sealed Chest Loot:** Only triggers `start_loot_vacuum()` when approaching a confirmed sealed chest in `STATE_CHEST_LOOT` OR within $4.0$s after exiting `STATE_COMBAT_AUTO` (monster drops).
4. **Staged Non-Blocking Obstacle Unstuck State Machine:**
   - Replaced permanent stall lockout with a staged, non-blocking unstuck routine:
     - **Stage 1 (Stall $\ge 0.8$s):** Pulses `SPACE` jump without stopping forward walking.
     - **Stage 2 (Stall $\ge 1.7$s):** Halts walking (`W0`), pivots laterally $\pm 70$ counts, resumes forward walking (`W1`), and jumps (`SPACE`), resetting the stall timer.
5. **Non-Blocking Combat & Amber Aiming Sub-States:**
   - Replaced all blocking `time.sleep()` calls in combat with non-blocking timestamp sub-states (`AIM_SWAP_WAIT`, `AIM_ENTER_R`, `AIM_TRACK_AND_CHARGE`, `AIM_RELEASE`).
   - Actively tracks the enemy's moving HP bar with 2-axis mouse steering (`M<dx>,<dy>`) *while* `LMB_HOLD` charges Amber's shot.
6. **Two-Zone PD Camera Steering & Pivot-in-Place:**
   - Firmware micro-stepped mouse movement widened to `[-110, 110]` with $16$-count steps spaced $900\,\mu$s apart.
   - Python PD controller operates at **28 Hz** ($0.035$s update interval) without pre-PD EMA lag:
     - **Zone 1 (Deadzone $|error\_x| \le 24$ px):** $dx = 0$, hard center lock.
     - **Zone 2 (Fine PD $24 < |error\_x| \le 130$ px):** $Kp = 0.26$, $Kd = 0.09$, clamped to $[-45, 45]$.
     - **Zone 3 (Fast Pivot $|error\_x| > 130$ px):** $Kp = 0.44$, $Kd = 0.10$, clamped to $[-95, 95]$.
     - **Pivot-in-Place:** Automatically pauses forward walking (`W0`) whenever $|error\_x| > 115$ px or when turning toward an off-screen minimap bearing.
10. **Safe OpenCV Window Lifecycle:**
    - Guarded with `cv2.getWindowProperty` and `try...except cv2.error` handlers so clicking the Windows `X` button closes the application safely without crashes.
11. **Fixed HUD Crosshair Line Typo:**
    - Corrected vertical crosshair line coordinates to `(mcx, mcy - 16)` $\rightarrow$ `(mcx, mcy + 16)`.

---

## Modular Project Structure

```
genshin_cv_hid/
├── assets/
│   ├── .gitkeep              # Optional templates (minimap_border.png, choice_icon.png)
│   ├── quest_gold.png        # 28x28 Gold Archon/Story quest diamond template
│   └── quest_blue.png        # 28x28 Blue World Quest diamond template
├── config.json               # Persisted PD gains, thresholds, and timings
├── firmware/
│   └── esp32s3_hid/
│       └── esp32s3_hid.ino   # Non-blocking TinyUSB HID + CDC Serial sketch (v5)
├── app/
│   ├── __init__.py           # Package exports
│   ├── config_manager.py     # Constants, ROIs, defaults, and config I/O
│   ├── controller.py         # ESP32HIDController + PDSteeringController
│   ├── vision.py             # VisionDetector (concentric templates, minimap, chest, prompt)
│   ├── state_machine.py      # AssistantStateMachine (hierarchy, unstuck, non-blocking combat)
│   ├── main.py               # Application entrypoint, capture loop, HUD rendering
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

| Command | Action | Execution | Hold Duration |
|---|---|---|---|
| `PING` | Handshake verification (returns `PONG\n`) | Immediate | Instant |
| `1`, `2`, `3`, `4` | Select party member slot 1 to 4 | Non-Blocking | 48–112 ms (Randomized) |
| `E` | Cast Elemental Skill | Non-Blocking | 48–112 ms (Randomized) |
| `Q` | Cast Elemental Burst | Non-Blocking | 48–112 ms (Randomized) |
| `R` | Toggle Bow Aiming Mode | Non-Blocking | 45–85 ms (Randomized) |
| `LMB` | Normal Attack (Left Mouse Click) | Non-Blocking | 48–112 ms (Randomized) |
| `LMB_HOLD` | Begin Charged Attack (Hold Left Mouse Button) | Immediate | Continuous until `LMB_REL` |
| `LMB_REL` | Finish Charged Attack (Release Left Mouse Button) | Immediate | Instant (clears `isLmbHeld`) |
| `RMB` | Sprint Dash / Dodge (Right Mouse Click) | Non-Blocking | 48–112 ms (Randomized) |
| `SPACE` | Jump / Glide / Unfreeze | Non-Blocking | 48–112 ms (Randomized) |
| `F` | Interact / Dialogue Option / Chest Loot | Non-Blocking | 48–112 ms (Randomized) |
| `V` | Quest Navigation Trail | Non-Blocking | 48–112 ms (Randomized) |
| `W1` | Continuous forward walk | Immediate | Continuous until `W0` or `STOP` |
| `W0` | Pause forward walk (releases `'w'` only) | Immediate | Instant (decoupled from mouse) |
| `STOP` | Emergency stop: releases all keys, mouse buttons & queue | Immediate | Instant |
| `M<dx>` | High-speed mouse move (clamped `[-110, 110]`) | Micro-Stepped | Instant (16-count steps, 900us spacing) |
| `M<dx>,<dy>` | High-speed 2-axis mouse move (clamped `[-110, 110]`) | Micro-Stepped | Instant (16-count steps, 900us spacing) |

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
Runs screen capture, template tracker, and state machine preview with no serial connection required:
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
| **`D`** | Debug Window | **Reset Configuration to Defaults**. Restores hardcoded defaults (`Kp_fine = 0.26`, `Kd = 0.09`, `Kp_fast = 0.44`, `threshold = 0.64`, etc.) and updates `config.json`. |
| **`S`** | Debug Window | **Save Configuration**. Saves current configuration to `config.json`. |
| **`Q`** / **`ESC`** | Debug Window | **Clean Shutdown**. Issues emergency `STOP` command to ESP32-S3, closes serial, and destroys OpenCV windows. |
