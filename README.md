# Genshin Impact Hardware-Assisted CV Automation (v2 Auto-Combat)

A production-grade, self-contained computer-vision navigation, dialogue, and **starter-roster auto-combat** assistant for **Genshin Impact** running on Windows (1280x720 Borderless or Windowed mode). 

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
              │ "1"-"4", "E", "Q", "LMB",        │ Keystrokes & Cursor
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
3. **Global `F12` Instant Pause Toggle:** Monitored system-wide via `pynput`. Pressing `F12` at any time immediately disengages inputs, releases all keys/buttons, and clears the combat queue.
4. **4-State Computer Vision Machine:**
   - **State 1 — Starter-Roster Auto-Combat (`COMBAT_AUTO`):** Detects enemy red HP bars in `y: 36..360`. Automatically closes distance if target cluster is distant (`area < 280` px), aligns camera toward enemy centroid, and dynamically generates non-blocking stochastic attack cycles:
     - Slot 1 (Traveler/Main DPS): `E` skill, 40% probability `Q` burst, 2–5 `LMB` attacks or 20% charged attack (`0.6s` `LMB_HOLD`).
     - Shuffled slots `[2, 3, 4]`: swaps character, casts `E` (85%), casts `Q` (35%), and chains 1–4 `LMB` attacks (pressing unlocked/empty slots on AR 1–10 accounts is safely ignored by the game).
     - Repositioning: 15% chance per cycle to trigger a dash (`RMB`) or jump (`SPACE`).
   - **State 2 — Combat Manual Fallback (`COMBAT_MANUAL`):** If combat runs continuously for `> 45.0` seconds without clearing (e.g. shielded Abyss Mage, scripted boss phase, or cliff occlusion), the assistant immediately halts inputs (`STOP`), sounds an 880 Hz alert chime, and yields control for manual takeover until cleared or resumed via `F12`.
   - **State 3 — Dialogue Mode (`DIALOGUE`):** Detects minimap disappearance (cutscenes / conversations), and actively advances both dialogue subtitle lines and choice boxes at a natural log-normal cadence (0.38s–0.78s + 12% probability of a 0.95s–1.45s human pause). The sluggish 2.0s cooldown has been removed for smooth story progression.
   - **State 4 — Overworld Navigation (`NAVIGATE`):** Pulses `'V'` to display quest paths, calculates gold quest marker centroid error relative to screen center, and applies humanized camera steering and variable sprint cadence (`RMB` every 2.1–3.8s).
     - **Hard Turn Pausing:** If `abs(error_x) > 160` px, pauses forward walking (`W0`) while turning hard (`M<dx>` clamped `[-45, 45]`) until centered, then resumes forward walking.
     - **Off-Screen Marker Sweep:** When the marker is lost for > 1.2s, stops walking and sweeps the camera horizontally (`22` to `36` px bursts every `0.18s–0.32s`) until the marker re-enters frame.
     - **Overworld [F] Prompt Interaction:** Monitors `y: 300..490, x: 740..940` for crisp high-contrast near-square `[F]` keycaps (Statues of the Seven, Teleport Waypoints, chests, quest items, NPCs). When confirmed over >= 2 consecutive frames, pauses walking, waits 0.15s–0.28s, and interacts with `F` (1.4s–2.1s cooldown).
5. **Humanized Steering & Timing:**
   - **22px Center Deadzone:** Tightened to eliminate camera micro-jitter while enabling crisp turns on small offsets.
   - **Gaussian Noise & Overshoot:** Injects `random.gauss(0, 2.0)` and `random.uniform(0.85, 1.15)` gain variance with default `Kp = 0.14` (tuner range 0.01–0.40).
   - **Throttled Updates:** Steering is updated at randomized 0.12s–0.28s intervals rather than mechanical frame-by-frame loops.
   - **Log-Normal Delays:** All action intervals utilize `human_delay()` with an occasional 10% micro-pause (0.85s–1.55s).
6. **Obstacle & Stuck Detection:** Performs continuous frame-differencing on the ground region beneath the player character. If `W1` has been held for >2.5s with near-zero visual displacement, the assistant releases keys, flags an obstacle alert, and sounds a warning tone.
7. **Live HSV & Gain Tuner:** Real-time OpenCV sliders for `H_min`, `H_max`, `S_min`, `S_max`, `V_min`, `V_max`, and proportional steering gain `Kp_x100`, with instant `config.json` persistence when pressing `S`.

---

## Project Structure

```
genshin_cv_hid/
├── assets/
│   ├── .gitkeep              # Optional templates (minimap_border.png, choice_icon.png)
├── config.json               # Persisted HSV thresholds and steering gain
├── firmware/
│   └── esp32s3_hid/
│       └── esp32s3_hid.ino   # Arduino TinyUSB HID + CDC Serial sketch (v2)
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
| `LMB` | Normal Attack (Left Mouse Click) | 48–112 ms (Randomized) |
| `LMB_HOLD` | Begin Charged Attack (Hold Left Mouse Button) | Continuous until `LMB_REL` |
| `LMB_REL` | Finish Charged Attack (Release Left Mouse Button) | Instant |
| `RMB` | Sprint Dash / Dodge (Right Mouse Click) | 48–112 ms (Randomized) |
| `SPACE` | Jump / Glide / Unfreeze | 48–112 ms (Randomized) |
| `F` | Interact / Dialogue Option | 48–112 ms (Randomized) |
| `V` | Quest Navigation Trail | 48–112 ms (Randomized) |
| `W1` | Continuous forward walk | Continuous until `W0`/`STOP` |
| `W0` / `STOP` | Emergency stop: releases all keys and mouse buttons | Instant |
| `M<dx>` | Relative horizontal mouse move (`[-50, 50]`) | Instant |

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

1. Open **Notepad** side-by-side with your terminal.
2. In the interactive menu:
   - Option `1`: Sends `PING` to verify bidirectional serial communication (`PONG`).
   - Option `2`: Dispatches Dialogue `'F'` and Quest `'V'`.
   - Option `3`: Cycles character slots `1 -> 2 -> 3 -> 4`.
   - Option `4`: Dispatches combat skills `'E'` and `'Q'`.
   - Option `5`: Tests Left Mouse Click (`LMB`) and Right Mouse Click (`RMB`).
   - Option `6`: Tests Charged Attack (`LMB_HOLD` for 0.6s, then `LMB_REL`).
   - Option `7`: Dispatches Jump (`SPACE`).
   - Option `8`: Tests Walk Hold `W1` (1.5s) -> `W0`.
   - Option `9`: Tests horizontal camera steering (`M<dx>`).
   - Option `a`: Runs the full automated verification suite with a 3-second countdown.

---

## Game Configuration (Genshin Impact)

1. **Resolution & Display Mode:**
   - Open Genshin Impact settings: **Graphics > Display Mode**.
   - Select **1280x720 Borderless** or **1280x720 Windowed**.
2. **Keybindings:**
   - Confirm standard default keybindings:
     - `W`: Move Forward
     - `1`, `2`, `3`, `4`: Party Members 1 to 4
     - `E`: Elemental Skill
     - `Q`: Elemental Burst
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
| **`F12`** | Global (Any Window) | **Instant Pause / Resume Toggle**. Immediately releases all held keys (`STOP`), clears combat queue, and suspends automation. |
| **`S`** | Debug Window | **Save Configuration**. Saves current Tuner HSV thresholds and `Kp_x100` gain to `config.json`. |
| **`Q`** / **`ESC`** | Debug Window | **Clean Shutdown**. Issues emergency `STOP` command to ESP32-S3, closes serial, and destroys OpenCV windows. |

---

## Troubleshooting

- **ESP32-S3 Not Detected / No COM Port:**
  Ensure you are plugged into the **native USB** port on the board. In Windows Device Manager, check under *Ports (COM & LPT)*. If marked with a yellow triangle, reinstall the ESP32 USB CDC driver or press the `BOOT` + `RESET` buttons on the board to enter download mode.
- **Combat Timing Out at 45s:**
  If an enemy is behind a shield or wall that blocks normal starter attacks, the 45s safety cutoff will trigger and beep, pausing the bot so you can take over manually. Once defeated or cleared, the bot automatically transitions back to `NAVIGATE`.
- **Character Missing Quest Marker Turns:**
  Use the live Tuner window sliders to adjust `H_min` / `H_max`, `S_min` / `S_max`, and `V_min` / `V_max` until the gold quest diamond shows up cleanly in the mask preview, then press **`S`** to save.
