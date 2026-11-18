/*
 * ESP32-S3 Native USB HID Keyboard & Mouse Controller for Genshin Impact CV Assistant
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
 * Protocol (Newline-terminated ASCII commands):
 *   PING           -> Replies "PONG\n"
 *   1, 2, 3, 4     -> Character slot select (random hold 48-112 ms)
 *   E              -> Elemental Skill (random hold 48-112 ms)
 *   Q              -> Elemental Burst (random hold 48-112 ms)
 *   F              -> Interact / Dialogue (random hold 48-112 ms)
 *   V              -> Quest path trace (random hold 48-112 ms)
 *   SPACE          -> Jump / Glide / Unfreeze (random hold 48-112 ms)
 *   LMB            -> Left Mouse Click / Normal Attack (random hold 48-112 ms)
 *   LMB_HOLD       -> Press & hold Left Mouse Button (Charged Attack start)
 *   LMB_REL        -> Release Left Mouse Button (Charged Attack finish)
 *   RMB            -> Right Mouse Click / Dash / Dodge (random hold 48-112 ms)
 *   W1             -> Hold 'w' key down (continuous forward movement)
 *   W0 / STOP      -> Immediately release all keyboard keys and mouse buttons
 *   M<dx>          -> Relative horizontal mouse move (clamped [-50, 50])
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

// Safety watchdog: auto-release keys if no command is received for 15 seconds while keys/buttons are held
unsigned long lastCommandTime = 0;
bool isKeyHeld = false;
const unsigned long WATCHDOG_TIMEOUT_MS = 15000;

void setup() {
  // Seed random generator with ESP32-S3 hardware true RNG
  randomSeed(esp_random());

  // Initialize native USB and HID peripherals
  Serial.begin(115200);
  Keyboard.begin();
  Mouse.begin();
  USB.begin();

  lastCommandTime = millis();
}

// Helper to press and release a keyboard key with humanized randomized hold
void tapKey(char key) {
  Keyboard.press(key);
  delay(random(48, 112));
  Keyboard.release(key);
}

void processCommand(const char* cmd) {
  // Reset safety watchdog on every valid incoming serial command
  lastCommandTime = millis();

  // Handshake ping
  if (strcmp(cmd, "PING") == 0) {
    Serial.println("PONG");
    return;
  }

  // Dialogue / Interaction key: F
  if (strcmp(cmd, "F") == 0) {
    tapKey('f');
    return;
  }

  // Quest Navigation trace key: V
  if (strcmp(cmd, "V") == 0) {
    tapKey('v');
    return;
  }

  // Elemental Skill: E
  if (strcmp(cmd, "E") == 0) {
    tapKey('e');
    return;
  }

  // Elemental Burst: Q
  if (strcmp(cmd, "Q") == 0) {
    tapKey('q');
    return;
  }

  // Character Slot Selection: 1, 2, 3, 4
  if (cmd[0] >= '1' && cmd[0] <= '4' && cmd[1] == '\0') {
    tapKey(cmd[0]);
    return;
  }

  // Jump / Glide / Unfreeze: SPACE
  if (strcmp(cmd, "SPACE") == 0) {
    tapKey(' ');
    return;
  }

  // Left Mouse Button Click: LMB (Normal attack)
  if (strcmp(cmd, "LMB") == 0) {
    Mouse.press(MOUSE_LEFT);
    delay(random(48, 112));
    Mouse.release(MOUSE_LEFT);
    return;
  }

  // Left Mouse Button Press and Hold: LMB_HOLD (Charged attack start)
  if (strcmp(cmd, "LMB_HOLD") == 0) {
    Mouse.press(MOUSE_LEFT);
    isKeyHeld = true;
    return;
  }

  // Left Mouse Button Release: LMB_REL (Charged attack release)
  if (strcmp(cmd, "LMB_REL") == 0) {
    Mouse.release(MOUSE_LEFT);
    return;
  }

  // Right Mouse Button Click: RMB (Dash / Sprint / Dodge)
  if (strcmp(cmd, "RMB") == 0) {
    Mouse.press(MOUSE_RIGHT);
    delay(random(48, 112));
    Mouse.release(MOUSE_RIGHT);
    return;
  }

  // Continuous Walk forward: W1
  if (strcmp(cmd, "W1") == 0) {
    Keyboard.press('w');
    isKeyHeld = true;
    return;
  }

  // Stop Walk / Emergency Stop: W0, STOP
  if (strcmp(cmd, "W0") == 0 || strcmp(cmd, "STOP") == 0) {
    Keyboard.releaseAll();
    Mouse.release(MOUSE_LEFT);
    Mouse.release(MOUSE_RIGHT);
    isKeyHeld = false;
    return;
  }

  // Relative Mouse Horizontal Steering: M<dx> (e.g., M12, M-8)
  if (cmd[0] == 'M') {
    int dx = atoi(cmd + 1);
    // Clamp to safe range [-50, 50]
    dx = constrain(dx, -50, 50);
    Mouse.move((int8_t)dx, 0);
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

  // Safety watchdog failsafe: prevent infinite walking or mouse holding if host script crashes
  if (isKeyHeld && (millis() - lastCommandTime > WATCHDOG_TIMEOUT_MS)) {
    Keyboard.releaseAll();
    Mouse.release(MOUSE_LEFT);
    Mouse.release(MOUSE_RIGHT);
    isKeyHeld = false;
  }
}
