/*
 * ESP32-S3 Native USB HID Keyboard & Mouse Controller for Genshin Impact CV Assistant (v5)
 * 
 * Hardware:
 *   ESP32-S3 (e.g. ESP32-S3-DevKitC-1 or compatible)
 *   Native USB OTG port (GPIO 19: D-, GPIO 20: D+)
 * 
 * Arduino IDE Settings:
 *   Board:              "ESP32S3 Dev Module" (or your specific S3 board)
 *   USB CDC On Boot:    "Enabled"
 *   USB Mode:           "USB-OTG (TinyUSB)"
 *   USB Firmware MSC:   "Disabled"
 *   Upload Mode:        "UART0 / Hardware CDC" (or "USB-OTG")
 * 
 * Non-Blocking v5 Architecture:
 *   - Zero-delay non-blocking millis() release scheduler (MAX_TAPS = 6)
 *   - Serial.setRxBufferSize(512) for high-rate command packet streaming
 *   - Decoupled hold states: W0 releases 'w' only; LMB_REL releases mouse only
 *   - High-speed micro-stepped mouse movement (clamped [-110, 110], 16-count steps, 900us spacing)
 *   - 12-second hardware safety watchdog
 * 
 * Protocol (Newline-terminated ASCII commands):
 *   PING           -> Replies "PONG\n"
 *   1, 2, 3, 4     -> Character slot select (random hold 48-112 ms, non-blocking)
 *   E              -> Elemental Skill (random hold 48-112 ms, non-blocking)
 *   Q              -> Elemental Burst (random hold 48-112 ms, non-blocking)
 *   R              -> Aiming Mode Toggle for Bow (random hold 45-85 ms, non-blocking)
 *   F              -> Interact / Dialogue / Loot (random hold 48-112 ms, non-blocking)
 *   V              -> Quest path trace (random hold 48-112 ms, non-blocking)
 *   SPACE          -> Jump / Glide / Unfreeze (random hold 48-112 ms, non-blocking)
 *   LMB            -> Left Mouse Click / Normal Attack (random hold 48-112 ms, non-blocking)
 *   LMB_HOLD       -> Press & hold Left Mouse Button (Charged Attack / Aim Shot start)
 *   LMB_REL        -> Release Left Mouse Button (Charged Attack / Aim Shot fire)
 *   RMB            -> Right Mouse Click / Dash / Dodge (random hold 48-112 ms, non-blocking)
 *   W1             -> Hold 'w' key down (continuous forward movement)
 *   W0             -> Release 'w' key only (continuous forward movement pause)
 *   STOP           -> Immediately release all keyboard keys, mouse buttons, and queues
 *   M<dx>          -> High-speed smooth relative mouse move (clamped [-110, 110], <= 16 counts/step)
 *   M<dx>,<dy>     -> High-speed smooth 2-axis mouse move (clamped [-110, 110], <= 16 counts/step)
 */

#include "USB.h"
#include "USBHIDKeyboard.h"
#include "USBHIDMouse.h"

USBHIDKeyboard Keyboard;
USBHIDMouse Mouse;

// Maximum length of incoming serial command line
const size_t CMD_BUF_SIZE = 64;
char cmdBuffer[CMD_BUF_SIZE];
size_t cmdIndex = 0;

// Non-blocking release scheduler table
enum TapType {
  TAP_NONE = 0,
  TAP_KEYBOARD,
  TAP_MOUSE_BUTTON
};

struct ScheduledRelease {
  bool active;
  TapType type;
  uint8_t target;
  unsigned long releaseAt;
};

const int MAX_TAPS = 6;
ScheduledRelease tapQueue[MAX_TAPS];

// Decoupled hold states
bool isWalking = false;
bool isLmbHeld = false;

// Safety watchdog: auto-release all if no command is received for 12 seconds while keys/buttons are held
unsigned long lastCommandTime = 0;
const unsigned long WATCHDOG_TIMEOUT_MS = 12000;

void scheduleKeyTap(char key, unsigned long holdMs) {
  // If this key is already active in queue, extend its release timestamp
  for (int i = 0; i < MAX_TAPS; i++) {
    if (tapQueue[i].active && tapQueue[i].type == TAP_KEYBOARD && tapQueue[i].target == (uint8_t)key) {
      tapQueue[i].releaseAt = millis() + holdMs;
      return;
    }
  }

  // Find an inactive slot
  for (int i = 0; i < MAX_TAPS; i++) {
    if (!tapQueue[i].active) {
      Keyboard.press(key);
      tapQueue[i].active = true;
      tapQueue[i].type = TAP_KEYBOARD;
      tapQueue[i].target = (uint8_t)key;
      tapQueue[i].releaseAt = millis() + holdMs;
      return;
    }
  }

  // If queue is completely full, evict slot 0 immediately to prevent stalling
  if (tapQueue[0].type == TAP_KEYBOARD) {
    Keyboard.release((char)tapQueue[0].target);
  } else if (tapQueue[0].type == TAP_MOUSE_BUTTON) {
    Mouse.release(tapQueue[0].target);
  }
  Keyboard.press(key);
  tapQueue[0].active = true;
  tapQueue[0].type = TAP_KEYBOARD;
  tapQueue[0].target = (uint8_t)key;
  tapQueue[0].releaseAt = millis() + holdMs;
}

void scheduleMouseTap(uint8_t button, unsigned long holdMs) {
  // If this button is already active in queue, extend its release timestamp
  for (int i = 0; i < MAX_TAPS; i++) {
    if (tapQueue[i].active && tapQueue[i].type == TAP_MOUSE_BUTTON && tapQueue[i].target == button) {
      tapQueue[i].releaseAt = millis() + holdMs;
      return;
    }
  }

  // Find an inactive slot
  for (int i = 0; i < MAX_TAPS; i++) {
    if (!tapQueue[i].active) {
      Mouse.press(button);
      tapQueue[i].active = true;
      tapQueue[i].type = TAP_MOUSE_BUTTON;
      tapQueue[i].target = button;
      tapQueue[i].releaseAt = millis() + holdMs;
      return;
    }
  }

  // If queue is completely full, evict slot 0 immediately to prevent stalling
  if (tapQueue[0].type == TAP_KEYBOARD) {
    Keyboard.release((char)tapQueue[0].target);
  } else if (tapQueue[0].type == TAP_MOUSE_BUTTON) {
    Mouse.release(tapQueue[0].target);
  }
  Mouse.press(button);
  tapQueue[0].active = true;
  tapQueue[0].type = TAP_MOUSE_BUTTON;
  tapQueue[0].target = button;
  tapQueue[0].releaseAt = millis() + holdMs;
}

void pollScheduledReleases() {
  unsigned long now = millis();
  for (int i = 0; i < MAX_TAPS; i++) {
    if (tapQueue[i].active && (long)(now - tapQueue[i].releaseAt) >= 0) {
      if (tapQueue[i].type == TAP_KEYBOARD) {
        Keyboard.release((char)tapQueue[i].target);
      } else if (tapQueue[i].type == TAP_MOUSE_BUTTON) {
        Mouse.release(tapQueue[i].target);
      }
      tapQueue[i].active = false;
    }
  }
}

void clearScheduledReleases() {
  for (int i = 0; i < MAX_TAPS; i++) {
    if (tapQueue[i].active) {
      if (tapQueue[i].type == TAP_KEYBOARD) {
        Keyboard.release((char)tapQueue[i].target);
      } else if (tapQueue[i].type == TAP_MOUSE_BUTTON) {
        Mouse.release(tapQueue[i].target);
      }
      tapQueue[i].active = false;
    }
  }
}

// Micro-stepped mouse movement: chunks of max 16 counts spaced 900us apart, clamped to [-110, 110]
void moveMouseSmooth(int totalDx, int totalDy) {
  int remX = constrain(totalDx, -110, 110);
  int remY = constrain(totalDy, -110, 110);

  while (remX != 0 || remY != 0) {
    int stepX = constrain(remX, -16, 16);
    int stepY = constrain(remY, -16, 16);

    Mouse.move((int8_t)stepX, (int8_t)stepY);
    remX -= stepX;
    remY -= stepY;

    if (remX != 0 || remY != 0) {
      delayMicroseconds(900);
    }
  }
}

void setup() {
  // Seed random generator with ESP32-S3 hardware true RNG
  randomSeed(esp_random());

  // Increase Serial RX buffer to 512 bytes for high-frequency packet streaming
  Serial.setRxBufferSize(512);
  Serial.begin(115200);

  Keyboard.begin();
  Mouse.begin();
  USB.begin();

  for (int i = 0; i < MAX_TAPS; i++) {
    tapQueue[i].active = false;
    tapQueue[i].type = TAP_NONE;
    tapQueue[i].target = 0;
    tapQueue[i].releaseAt = 0;
  }

  lastCommandTime = millis();
}

void processCommand(const char* cmd) {
  // Reset safety watchdog on every valid incoming serial command
  lastCommandTime = millis();

  // Handshake ping
  if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
    return;
  }

  // Dialogue / Interaction / Loot key: F
  if (strcmp(cmd, "F") == 0) {
    scheduleKeyTap('f', random(48, 112));
    return;
  }

  // Quest Navigation trace key: V
  if (strcmp(cmd, "V") == 0) {
    scheduleKeyTap('v', random(48, 112));
    return;
  }

  // Elemental Skill: E
  if (strcmp(cmd, "E") == 0) {
    scheduleKeyTap('e', random(48, 112));
    return;
  }

  // Elemental Burst: Q
  if (strcmp(cmd, "Q") == 0) {
    scheduleKeyTap('q', random(48, 112));
    return;
  }

  // Bow Aiming Mode Toggle: R
  if (strcmp(cmd, "R") == 0) {
    scheduleKeyTap('r', random(45, 85));
    return;
  }

  // Character Slot Selection: 1, 2, 3, 4
  if (cmd[0] >= '1' && cmd[0] <= '4' && cmd[1] == '\0') {
    scheduleKeyTap(cmd[0], random(48, 112));
    return;
  }

  // Jump / Glide / Unfreeze: SPACE
  if (strcmp(cmd, "SPACE") == 0) {
    scheduleKeyTap(' ', random(48, 112));
    return;
  }

  // Left Mouse Button Click: LMB (Normal attack)
  if (strcmp(cmd, "LMB") == 0) {
    scheduleMouseTap(MOUSE_LEFT, random(48, 112));
    return;
  }

  // Left Mouse Button Press and Hold: LMB_HOLD (Charged attack start)
  if (strcmp(cmd, "LMB_HOLD") == 0) {
    Mouse.press(MOUSE_LEFT);
    isLmbHeld = true;
    return;
  }

  // Left Mouse Button Release: LMB_REL (Charged attack release)
  if (strcmp(cmd, "LMB_REL") == 0) {
    Mouse.release(MOUSE_LEFT);
    isLmbHeld = false;
    return;
  }

  // Right Mouse Button Click: RMB (Dash / Sprint / Dodge)
  if (strcmp(cmd, "RMB") == 0) {
    scheduleMouseTap(MOUSE_RIGHT, random(48, 112));
    return;
  }

  // Continuous Walk forward: W1
  if (strcmp(cmd, "W1") == 0) {
    Keyboard.press('w');
    isWalking = true;
    return;
  }

  // Pause Walk: W0 (Decoupled: releases 'w' ONLY)
  if (strcmp(cmd, "W0") == 0) {
    Keyboard.release('w');
    isWalking = false;
    return;
  }

  // Emergency Stop: STOP (Releases all inputs and clears schedule)
  if (strcmp(cmd, "STOP") == 0) {
    clearScheduledReleases();
    Keyboard.releaseAll();
    Mouse.release(MOUSE_LEFT);
    Mouse.release(MOUSE_RIGHT);
    isWalking = false;
    isLmbHeld = false;
    return;
  }

  // Relative Mouse Steering: M<dx> or M<dx>,<dy>
  if (cmd[0] == 'M') {
    int dx = 0;
    int dy = 0;
    const char* comma = strchr(cmd + 1, ',');
    if (comma != NULL) {
      dx = atoi(cmd + 1);
      dy = atoi(comma + 1);
    } else {
      dx = atoi(cmd + 1);
      dy = 0;
    }
    moveMouseSmooth(dx, dy);
    return;
  }
}

void loop() {
  // Non-blocking serial command reader
  while (Serial.available() > 0) {
    char c = (char)Serial.read();

    if (c == '\n' || c == '\r') {
      if (cmdIndex > 0) {
        cmdBuffer[cmdIndex] = '\0';
        processCommand(cmdBuffer);
        cmdIndex = 0;
      }
    } else {
      if (cmdIndex < CMD_BUF_SIZE - 1) {
        cmdBuffer[cmdIndex++] = c;
      } else {
        // Buffer overflow protection: reset buffer
        cmdIndex = 0;
      }
    }
  }

  // Non-blocking key/mouse release processor
  pollScheduledReleases();

  // Safety watchdog failsafe: prevent infinite walking or mouse holding if host script crashes
  if ((isWalking || isLmbHeld) && (millis() - lastCommandTime > WATCHDOG_TIMEOUT_MS)) {
    clearScheduledReleases();
    Keyboard.releaseAll();
    Mouse.release(MOUSE_LEFT);
    Mouse.release(MOUSE_RIGHT);
    isWalking = false;
    isLmbHeld = false;
  }
}
