#!/usr/bin/env python3
"""
ESP32-S3 Serial-to-HID Hardware Verification Utility
Verifies serial communication, keyboard keypresses, continuous hold, mouse movements,
and starter-roster combat inputs (1-4, E, Q, LMB, LMB_HOLD, LMB_REL, RMB, SPACE).
"""

import sys
import time
import argparse
import serial
import serial.tools.list_ports


def print_banner():
    banner = r"""
===================================================================
    ESP32-S3 Native USB HID Verification Suite (v2 Combat Edition)
===================================================================
[*] Tests serial communication and hardware USB keyboard/mouse events.
[*] Supports Overworld Navigation & Starter-Roster Auto-Combat commands:
    - Slots: 1, 2, 3, 4
    - Skills: E (Skill), Q (Burst)
    - Mouse: LMB (Attack), LMB_HOLD/REL (Charged), RMB (Dash), M<dx>
    - Movement: W1/W0 (Walk), SPACE (Jump), F (Interact), V (Route)
[*] IMPORTANT: Open a blank Notepad window to safely observe typed
    letters and mouse cursor movements.
===================================================================
"""
    print(banner)


def list_available_ports():
    ports = serial.tools.list_ports.comports()
    print("[INFO] Detected Serial / COM Ports:")
    if not ports:
        print("  - (No COM ports found. Check USB connection and driver)")
    for p in ports:
        print(f"  - {p.device}: {p.description} [{p.hwid}]")
    print()


class HIDTester:
    def __init__(self, port="COM4", baud=115200, timeout=1.5):
        self.port = port
        self.baud = baud
        self.timeout = timeout
        self.ser = None

    def connect(self) -> bool:
        print(f"[>] Connecting to ESP32-S3 on port '{self.port}' at {self.baud} baud...")
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=self.timeout)
            # Allow board to settle / CDC reboot
            time.sleep(1.8)
            self.ser.reset_input_buffer()
            self.ser.reset_output_buffer()
            print(f"[+] Serial port {self.port} opened successfully.")
            return True
        except serial.SerialException as e:
            print(f"[-] Serial connection failed: {e}")
            list_available_ports()
            return False

    def send_raw(self, cmd: str) -> None:
        if self.ser and self.ser.is_open:
            self.ser.write(f"{cmd}\n".encode("ascii"))
            self.ser.flush()

    def test_ping(self) -> bool:
        print("[>] Testing PING -> PONG handshake...")
        self.send_raw("PING")
        line = self.ser.readline().decode("ascii", errors="ignore").strip()
        if line == "PONG":
            print("    [PASS] Received 'PONG' from ESP32-S3.")
            return True
        else:
            print(f"    [FAIL] Expected 'PONG', received: '{line}'")
            return False

    def test_single_key(self, key_name: str, cmd: str):
        print(f"[>] Testing '{key_name}' (cmd: {cmd})...")
        self.send_raw(cmd)
        time.sleep(0.25)
        print(f"    [DONE] Keystroke '{key_name}' dispatched.")

    def test_character_slots(self):
        print("[>] Testing Character Slots 1 -> 2 -> 3 -> 4...")
        for slot in ("1", "2", "3", "4"):
            self.send_raw(slot)
            print(f"    Selected Slot {slot}")
            time.sleep(0.3)
        print("    [DONE] Character slot keys tested.")

    def test_combat_skills(self):
        print("[>] Testing Combat Skills: 'E' (Skill) and 'Q' (Burst)...")
        self.send_raw("E")
        time.sleep(0.3)
        self.send_raw("Q")
        time.sleep(0.3)
        print("    [DONE] Skills E and Q dispatched.")

    def test_mouse_clicks(self):
        print("[>] Testing Left Mouse Click (LMB) and Right Mouse Click (RMB)...")
        print("    Sending LMB (Normal Attack)...")
        self.send_raw("LMB")
        time.sleep(0.3)
        print("    Sending RMB (Dash / Dodge)...")
        self.send_raw("RMB")
        time.sleep(0.3)
        print("    [DONE] Mouse clicks dispatched.")

    def test_charged_attack(self):
        print("[>] Testing Charged Attack: LMB_HOLD -> 0.6s hold -> LMB_REL...")
        self.send_raw("LMB_HOLD")
        print("    Holding Left Mouse Button...")
        time.sleep(0.6)
        self.send_raw("LMB_REL")
        print("    Released Left Mouse Button.")
        time.sleep(0.2)
        print("    [DONE] Charged attack sequence completed.")

    def test_jump_space(self):
        print("[>] Testing Jump (SPACE)...")
        self.send_raw("SPACE")
        time.sleep(0.3)
        print("    [DONE] Spacebar dispatched.")

    def test_walk_hold(self):
        print("[>] Testing Walk Hold 'W1' -> 'W0'...")
        print("    Sending 'W1' (press and hold 'w')...")
        self.send_raw("W1")
        print("    Holding for 1.5 seconds (you should see 'w' repeating in Notepad)...")
        time.sleep(1.5)
        print("    Sending 'W0' (release 'w')...")
        self.send_raw("W0")
        time.sleep(0.3)
        print("    [DONE] Walk test complete.")

    def test_mouse_wiggle(self):
        print("[>] Testing Mouse Horizontal Steering (M<dx>)...")
        print("    Moving cursor smoothly Left -> Right -> Center...")
        # Move Left in 5 steps of dx = -15
        for _ in range(5):
            self.send_raw("M-15")
            time.sleep(0.04)
        time.sleep(0.2)
        # Move Right in 10 steps of dx = +15
        for _ in range(10):
            self.send_raw("M15")
            time.sleep(0.04)
        time.sleep(0.2)
        # Return to Center in 5 steps of dx = -15
        for _ in range(5):
            self.send_raw("M-15")
            time.sleep(0.04)
        print("    [DONE] Mouse motion sequence finished.")

    def test_stop(self):
        print("[>] Sending STOP (Keyboard.releaseAll + Mouse.release)...")
        self.send_raw("STOP")
        print("    [DONE] All keys and mouse buttons released.")

    def run_automated_suite(self):
        print("\n--- Starting Full Automated Verification Suite ---")
        print("Switch focus to Notepad now! (Starting in 3 seconds...)")
        for i in range(3, 0, -1):
            print(f"  {i}...")
            time.sleep(1.0)

        # 1. Ping
        if not self.test_ping():
            print("[-] Ping test failed. Aborting automated sequence.")
            return

        time.sleep(0.5)

        # 2. Key F & V
        self.test_single_key("F", "F")
        time.sleep(0.5)
        self.test_single_key("V", "V")
        time.sleep(0.5)

        # 3. Character slots 1-4
        self.test_character_slots()
        time.sleep(0.5)

        # 4. Combat Skills E & Q
        self.test_combat_skills()
        time.sleep(0.5)

        # 5. Jump (Space)
        self.test_jump_space()
        time.sleep(0.5)

        # 6. Mouse clicks (LMB & RMB)
        self.test_mouse_clicks()
        time.sleep(0.5)

        # 7. Charged Attack
        self.test_charged_attack()
        time.sleep(0.5)

        # 8. Walk W1 -> W0
        self.test_walk_hold()
        time.sleep(0.5)

        # 9. Mouse wiggle
        self.test_mouse_wiggle()
        time.sleep(0.5)

        # 10. STOP failsafe
        self.test_stop()

        print("\n[SUCCESS] All hardware test signals dispatched cleanly!")
        print("Verify your Notepad document contains: f, v, 1, 2, 3, 4, e, q, space, and a series of w's.")

    def close(self):
        if self.ser and self.ser.is_open:
            self.test_stop()
            self.ser.close()
            print("[INFO] Serial connection closed.")


def interactive_menu(tester: HIDTester):
    while True:
        print("\n-------------------------------------------")
        print(" Select an HID Test Operation:")
        print("  [1] Send PING (handshake verify)")
        print("  [2] Test Dialogue 'F' & Quest 'V'")
        print("  [3] Test Character Slots 1-4")
        print("  [4] Test Combat Skills 'E' & 'Q'")
        print("  [5] Test Left Click (LMB) & Right Click (RMB)")
        print("  [6] Test Charged Attack (LMB_HOLD -> LMB_REL)")
        print("  [7] Test Jump (SPACE)")
        print("  [8] Test Walk Hold 'W1' (1.5s) -> 'W0'")
        print("  [9] Test Mouse Horizontal Steering (M<dx>)")
        print("  [a] Run Full Automated Test Suite")
        print("  [s] Send STOP (Release All Keys & Buttons)")
        print("  [q] Quit")
        print("-------------------------------------------")

        choice = input("Enter option [1-9, a, s, q]: ").strip().lower()
        if choice == "1":
            tester.test_ping()
        elif choice == "2":
            tester.test_single_key("F", "F")
            time.sleep(0.3)
            tester.test_single_key("V", "V")
        elif choice == "3":
            tester.test_character_slots()
        elif choice == "4":
            tester.test_combat_skills()
        elif choice == "5":
            tester.test_mouse_clicks()
        elif choice == "6":
            tester.test_charged_attack()
        elif choice == "7":
            tester.test_jump_space()
        elif choice == "8":
            tester.test_walk_hold()
        elif choice == "9":
            tester.test_mouse_wiggle()
        elif choice == "a":
            tester.run_automated_suite()
        elif choice == "s":
            tester.test_stop()
        elif choice in ("q", "quit", "exit"):
            break
        else:
            print("[!] Invalid option. Please choose from the menu.")


def main():
    parser = argparse.ArgumentParser(description="ESP32-S3 Serial-to-HID Diagnostic Utility (v2 Combat)")
    parser.add_argument("--port", type=str, default="COM4", help="Serial port for ESP32-S3 (default: COM4)")
    parser.add_argument("--baud", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--auto", action="store_true", help="Run automated test suite immediately and exit")
    args = parser.parse_args()

    print_banner()

    tester = HIDTester(port=args.port, baud=args.baud)
    if not tester.connect():
        sys.exit(1)

    try:
        if args.auto:
            tester.run_automated_suite()
        else:
            interactive_menu(tester)
    except KeyboardInterrupt:
        print("\n[!] User interrupted test.")
    finally:
        tester.close()


if __name__ == "__main__":
    main()
