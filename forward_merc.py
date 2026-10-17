import os
import signal
import sys
import time
import selectors
from evdev import InputDevice, ecodes

import glob

# Let systemd's SIGTERM reach the safety-cleanup finally: without a handler the
# process is killed silently and held keys stay stuck on the host
def _sigterm(*_):
    raise SystemExit(0)
signal.signal(signal.SIGTERM, _sigterm)

# Turn off host lock keys (Num/Caps/Scroll) at script boot
AUTO_CLEAR_LOCKS = True
# Print every host LED report to the console (for debugging)
DEBUG_LEDS = False

# Size of one host LED output report in bytes. Set to 2 if the hidg0 gadget
# descriptor uses a report ID (the LED state is always the LAST byte).
LED_REPORT_SIZE = 1
# If no host LED report confirms a local NumLock toggle within this many
# seconds, re-probe the host's lock state (double-toggle + read).
NUMLOCK_PROBE_TIMEOUT = 0.3

# Match the Merc's streams by name, not by fixed event numbers.
# The primary keyboard stream is REQUIRED: a consumer-only discovery would
# leave a zombie service that forwards nothing but volume keys.
PRIMARY_NAME = 'Ideazon Zboard Gaming Keyboard'
WANTED_NAMES = [
    PRIMARY_NAME,
    'Ideazon Zboard Gaming Keyboard Consumer Control',
]
devices = []
found_primary = False

print("1. Hooking into Merc hardware streams...")
for path in sorted(glob.glob('/dev/input/event*')):
    try:
        dev = InputDevice(path)
    except Exception as e:
        print(f"FAILED to initialize stream {path}: {e}")
        continue
    if any(dev.name == wanted for wanted in WANTED_NAMES):
        devices.append(dev)
        if dev.name == PRIMARY_NAME:
            found_primary = True
        print(f"-> Linked stream: {dev.name} ({path})")

if not found_primary:
    print(f"Primary stream '{PRIMARY_NAME}' not found (consumer-only streams are "
          "useless). Refusing to start. Exiting.")
    sys.exit(1)

print("2. Opening USB link to Windows...")
# The ExecStartPre gadget bring-up can race us: retry instead of letting
# systemd churn a full service restart (and a USB re-enumeration) every 5 s.
try:
    hid_out = None
    for attempt in range(1, 11):
        try:
            hid_out = open('/dev/hidg0', 'wb+')  # read-write: writes key reports, reads host LED reports
            break
        except OSError as e:
            if attempt == 10:
                raise
            print(f"hidg0 not ready yet (attempt {attempt}/10): {e}; retrying in 0.5 s...")
            time.sleep(0.5)
    print("3. USB pipeline opened cleanly.")
except Exception as e:
    print(f"FAILED to open USB pipeline: {e}")
    sys.exit(1)

state = [0] * 8
ZERO6 = [0] * 6
pressed_keys = {}   # insertion-ordered: hid_code -> True (deterministic 6KRO)
key_sources = {}    # hid_code -> set of holders: ('phys', dev_path, code) and/or ('hot', None, code)
                    # (dev_path is the event node path string — InputDevice objects are unhashable)
phys_mods = 0       # modifier bits held by physical keys
hot_mods = 0        # modifier bits held by the virtual hotkey layer
hotkey_codes = {}   # evdev code -> modifier bits, for hotkeys currently held
phys_codes = {}     # evdev code -> hid_code, for keys pressed via the physical map
num_lock_active = False
led_buf = bytearray()  # partial LED report bytes, parsed in LED_REPORT_SIZE chunks
numlock_probe_deadline = None  # monotonic deadline: re-probe if no LED report confirms a toggle
numlock_retries = 0          # consecutive failed re-probes (capped, see watchdog)
usb_dead = False
pending_events = []  # (code, val, dev) tuples drained while probing LEDs, replayed in the main loop


# Clear any keys the host is still seeing from a previous hard exit
try:
    hid_out.write(bytes(8))
    hid_out.flush()
except OSError:
    usb_dead = True


def rebuild_report():
    """Rebuild the 8-byte keyboard packet from current key/modifier state."""
    state[0] = phys_mods | hot_mods
    state[2:8] = ZERO6
    for i, k in enumerate(list(pressed_keys)[:6]):  # first 6 in press order
        state[2+i] = k


def release_device_keys(dev):
    """Stream died: drop every key held via this physical device, push a fresh report."""
    global usb_dead
    dev_path = getattr(dev, 'path', None)
    for hid_code, srcs in list(key_sources.items()):
        for s in list(srcs):
            if s[0] == 'phys' and s[1] == dev_path:
                srcs.discard(s)
        if not srcs:
            key_sources.pop(hid_code, None)
            pressed_keys.pop(hid_code, None)
    rebuild_report()
    if not usb_dead:
        try:
            hid_out.write(bytes(state))
            hid_out.flush()
        except OSError:
            usb_dead = True


def key_down(hid_code, source):
    if hid_code not in pressed_keys:
        pressed_keys[hid_code] = True
    key_sources.setdefault(hid_code, set()).add(source)


def key_up(hid_code, source):
    srcs = key_sources.get(hid_code)
    if srcs:
        srcs.discard(source)
        if not srcs:
            key_sources.pop(hid_code, None)
            pressed_keys.pop(hid_code, None)

# Virtual hotkey layer: evdev code -> (modifier bits, [hid keys])
HOTKEYS = {
    ecodes.KEY_F13: (0x08 | 0x02, [0x16]),  # I:   Win+Shift+S  (Snipping Tool)
    ecodes.KEY_F14: (0x01 | 0x02, [0x29]),  # II:  Ctrl+Shift+Esc (Task Manager)
    ecodes.KEY_F15: (0x08, [0x2b]),         # III: Win+Tab (Task View)

    # Media / function remaps (always active; original functions replaced):
    ecodes.KEY_F10: (0x08 | 0x04, [0x0e]),  # F10  -> Win+Alt+K
    ecodes.KEY_VOLUMEUP: (0x00, [0x41]),    # VolUp -> F8
    ecodes.KEY_VOLUMEDOWN: (0x00, [0x42]),  # VolDn -> F9
    ecodes.KEY_MUTE: (0x00, [0x40]),        # Mute  -> F7
    ecodes.KEY_PREVIOUSSONG: (0x01, [0x05]),# Prev  -> Ctrl+B
    ecodes.KEY_PLAYPAUSE: (0x00, [0x2c]),   # Play/Pause -> Space
    ecodes.KEY_STOPCD: (0x01, [0x16]),      # Stop  -> Ctrl+S
    ecodes.KEY_NEXTSONG: (0x01, [0x09]),    # Next  -> Ctrl+F
}

# Numpad-off hotkeys (active only when num_lock_active is False).
# NOTE: these evdev codes are deliberately dual-use — when NumLock is ON the
# same physical keys map to numpad keys via NUMPAD_ENABLED_MAP instead.
HOTKEYS_NUMPAD_OFF = {
    ecodes.KEY_HELP: (0x02, [0x50]),            # Shift+Left  (select char left)
    ecodes.KEY_UNDO: (0x02, [0x4f]),            # Shift+Right (select char right)
    ecodes.KEY_CUT: (0x01, [0x1b]),             # Ctrl+X
    ecodes.KEY_COPY: (0x01, [0x06]),            # Ctrl+C
    ecodes.KEY_PASTE: (0x01, [0x19]),           # Ctrl+V
    ecodes.KEY_KPMINUS: (0x04, [0x2c, 0x0e]),   # Alt+Space+N (minimise window)
    ecodes.KEY_FIND: (0x01, [0x09]),            # Ctrl+F
    ecodes.KEY_SCROLLLOCK: (0x08, [0x51]),      # Win+Down (restore window)
    ecodes.KEY_PAUSE: (0x08, [0x52]),           # Win+Up (maximise window)
    ecodes.KEY_STOP: (0x04, [0x3d]),            # Alt+F4 (close window)
    ecodes.KEY_FILE: (0x08, [0x08]),            # Win+E (File Manager)
}

# GENERAL BASE MAP
MAP = {
    ecodes.KEY_A: 0x04, ecodes.KEY_B: 0x05, ecodes.KEY_C: 0x06, ecodes.KEY_D: 0x07,
    ecodes.KEY_E: 0x08, ecodes.KEY_F: 0x09, ecodes.KEY_G: 0x0a, ecodes.KEY_H: 0x0b,
    ecodes.KEY_I: 0x0c, ecodes.KEY_J: 0x0d, ecodes.KEY_K: 0x0e, ecodes.KEY_L: 0x0f,
    ecodes.KEY_M: 0x10, ecodes.KEY_N: 0x11, ecodes.KEY_O: 0x12, ecodes.KEY_P: 0x13,
    ecodes.KEY_Q: 0x14, ecodes.KEY_R: 0x15, ecodes.KEY_S: 0x16, ecodes.KEY_T: 0x17,
    ecodes.KEY_U: 0x18, ecodes.KEY_V: 0x19, ecodes.KEY_W: 0x1a, ecodes.KEY_X: 0x1b,
    ecodes.KEY_Y: 0x1c, ecodes.KEY_Z: 0x1d,
    ecodes.KEY_1: 0x1e, ecodes.KEY_2: 0x1f, ecodes.KEY_3: 0x20, ecodes.KEY_4: 0x21,
    ecodes.KEY_5: 0x22, ecodes.KEY_6: 0x23, ecodes.KEY_7: 0x24, ecodes.KEY_8: 0x25,
    ecodes.KEY_9: 0x26, ecodes.KEY_0: 0x27, ecodes.KEY_EQUAL: 0x2e, ecodes.KEY_MINUS: 0x2d,
    
    ecodes.KEY_ENTER: 0x28, ecodes.KEY_ESC: 0x29, ecodes.KEY_BACKSPACE: 0x2a,
    ecodes.KEY_TAB: 0x2b, ecodes.KEY_SPACE: 0x2c, ecodes.KEY_CAPSLOCK: 0x39,
    ecodes.KEY_LEFT: 0x50, ecodes.KEY_RIGHT: 0x4f, ecodes.KEY_UP: 0x52, ecodes.KEY_DOWN: 0x51,
    ecodes.KEY_KPENTER: 0x58,
    
    ecodes.KEY_GRAVE: 0x35, ecodes.KEY_LEFTBRACE: 0x2f, ecodes.KEY_RIGHTBRACE: 0x30,
    ecodes.KEY_SEMICOLON: 0x33, ecodes.KEY_APOSTROPHE: 0x34, ecodes.KEY_BACKSLASH: 0x31,
    ecodes.KEY_102ND: 0x64, ecodes.KEY_COMMA: 0x36, ecodes.KEY_DOT: 0x37, ecodes.KEY_SLASH: 0x38,

    ecodes.KEY_F1: 0x3a, ecodes.KEY_F2: 0x3b, ecodes.KEY_F3: 0x3c, ecodes.KEY_F4: 0x3d,
    ecodes.KEY_F5: 0x3e, ecodes.KEY_F6: 0x3f, ecodes.KEY_F7: 0x40, ecodes.KEY_F8: 0x41,
    ecodes.KEY_F9: 0x42, ecodes.KEY_F11: 0x44, ecodes.KEY_F12: 0x45,
    # KEY_F10 and the media keys are NOT here: remapped in HOTKEYS above

    ecodes.KEY_NUMLOCK: 0x53,

}

NUMPAD_ENABLED_MAP = {
    ecodes.KEY_SCROLLLOCK: 0x47, ecodes.KEY_PAUSE: 0x48, ecodes.KEY_STOP: 0x38,
    ecodes.KEY_HELP: 0x27, ecodes.KEY_DELETE: 0x1e, ecodes.KEY_CUT: 0x1f,
    ecodes.KEY_COPY: 0x20, ecodes.KEY_INSERT: 0x21, ecodes.KEY_END: 0x22,
    ecodes.KEY_PAGEDOWN: 0x23, ecodes.KEY_FIND: 0x24, ecodes.KEY_HOME: 0x25,
    ecodes.KEY_PAGEUP: 0x26, ecodes.KEY_UNDO: 0x37, ecodes.KEY_KPMINUS: 0x2d,
    # KEY_PASTE is numpad '+': must be HID 0x57 (Keypad +, VK_ADD -> '+').
    # 0x2e is the main-row =/+ key (VK_OEM_PLUS) which types '=' unshifted.
    ecodes.KEY_PASTE: 0x57, ecodes.KEY_FILE: 0x55,
}

NUMPAD_DISABLED_MAP = {
    ecodes.KEY_HOME: 0x4a, ecodes.KEY_PAGEUP: 0x4b, ecodes.KEY_PAGEDOWN: 0x4e,
    ecodes.KEY_END: 0x4d, ecodes.KEY_INSERT: 0x49, ecodes.KEY_DELETE: 0x4c,
}

MODIFIERS = {
    ecodes.KEY_LEFTCTRL: 0x01,  ecodes.KEY_LEFTSHIFT: 0x02,
    ecodes.KEY_LEFTALT: 0x04,   ecodes.KEY_LEFTMETA: 0x08,
    ecodes.KEY_RIGHTCTRL: 0x10, ecodes.KEY_RIGHTSHIFT: 0x20,
    ecodes.KEY_RIGHTALT: 0x40,  ecodes.KEY_RIGHTMETA: 0x80
}

selector = selectors.DefaultSelector()
for dev in devices:
    selector.register(dev, selectors.EVENT_READ)
selector.register(hid_out, selectors.EVENT_READ)  # host LED output reports


def process_event(code, val, dev=None):
    """Handle one EV_KEY press/release and push a fresh HID report to the host."""
    global hot_mods, phys_mods, num_lock_active, numlock_probe_deadline, numlock_retries, usb_dead

    if code == ecodes.KEY_NUMLOCK and val == 1 and 0x53 not in pressed_keys:
        # Toggle immediately for instant response; a host LED
        # report follows and re-syncs to the definitive state.
        # Real press edge only: the fall-through below registers 0x53 in
        # pressed_keys, so an autorepeat repeat (val==1 while still held)
        # must NOT re-flip local state while the host toggles only once.
        # (standalone if: the key must still fall through to the
        #  map below so the press is forwarded to the host)
        num_lock_active = not num_lock_active
        numlock_retries = 0  # fresh press: fresh watchdog probe budget
        numlock_probe_deadline = time.monotonic() + NUMLOCK_PROBE_TIMEOUT

    # Virtual hotkey layer (custom keys I / II / III, plus numpad-off cluster).
    # Tracked separately from physical state so a hotkey release
    # never clears a physically-held modifier or key.
    if code in hotkey_codes or (val == 1 and (code in HOTKEYS or (not num_lock_active and code in HOTKEYS_NUMPAD_OFF))):
        # code in hotkey_codes: a held hotkey stays on the hotkey
        # layer for repeat/release even if NumLock flipped mid-hold
        active_hotkeys = HOTKEYS if code in HOTKEYS else HOTKEYS_NUMPAD_OFF
        mods, keys = active_hotkeys[code]
        if val == 1:
            hotkey_codes[code] = mods
            hot_mods |= mods
            for k in keys:
                key_down(k, ('hot', None, code))
        elif val == 0:
            hotkey_codes.pop(code, None)
            hot_mods = 0  # recompute from remaining held hotkeys
            for m in hotkey_codes.values():
                hot_mods |= m
            for k in keys:
                key_up(k, ('hot', None, code))

    elif code in MODIFIERS:
        if val == 1: phys_mods |= MODIFIERS[code]
        elif val == 0: phys_mods &= ~MODIFIERS[code]
    else:
        if val == 0 and code in phys_codes:
            # release returns to the layer that pressed it, even
            # if NumLock flipped while the key was held
            hid_code = phys_codes.pop(code)
            key_up(hid_code, ('phys', dev.path, code))
        else:
            hid_code = MAP.get(code)
            if hid_code is None:
                hid_code = (NUMPAD_ENABLED_MAP if num_lock_active else NUMPAD_DISABLED_MAP).get(code)

            if hid_code is not None:
                if val == 1:
                    phys_codes[code] = hid_code
                    key_down(hid_code, ('phys', dev.path, code))
                elif val == 0:
                    key_up(hid_code, ('phys', dev.path, code))

    # Safely rebuild the standard key packet rollover
    rebuild_report()

    if not usb_dead:
        try:
            hid_out.write(bytes(state))
            hid_out.flush()
        except OSError as e:
            print(f"USB pipeline error: {e} (further write errors suppressed)")
            usb_dead = True

# --- Startup: grab the host's initial LED report, optionally clear active locks ---
def send_report(report):
    global usb_dead
    try:
        hid_out.write(bytes(report))
        hid_out.flush()
    except OSError:
        usb_dead = True  # stop the main loop retrying a dead pipeline

def toggle_key_off(hid_key):
    """Send a press+release of hid_key without disturbing other held keys.

    Frames are built from the current source state: every key in
    pressed_keys (except hid_key itself) plus hid_key in the first free
    slot. A physically-held key is never stomped. Only when all six slots
    are taken does the last-resort path drop the first held key from the
    two frames (and rebuild_report() restores it straight after).
    """
    mods = phys_mods | hot_mods
    others = [k for k in pressed_keys if k != hid_key]
    if len(others) < 6:
        press_keys = others + [hid_key]
        if hid_key in pressed_keys:
            # e.g. watchdog firing while NumLock itself is still held:
            # the release frame must keep holding it
            release_keys = list(pressed_keys)
        else:
            release_keys = others
    else:
        # Board is full: no room for the toggle key. Last resort — drop the
        # first held key from these two frames only.
        press_keys = others[:5] + [hid_key]
        release_keys = others[:5]
    send_report([mods] + press_keys + [0] * (6 - len(press_keys)))  # press
    time.sleep(0.1)  # let the host poll the press before the release
    send_report([mods] + release_keys + [0] * (6 - len(release_keys)))  # release
    if len(others) >= 6:
        rebuild_report()  # restore the temporarily-dropped key

def parse_led_reports():
    """Drain complete LED reports from led_buf. Returns (last_led_byte or None, report_count).

    The count lets the startup path sanity-check LED_REPORT_SIZE: if it
    misconfigures the size, one physical LED report is chopped into several
    fragments and the parsed bytes flaps between nonsense values.
    """
    led, count = None, 0
    while len(led_buf) >= LED_REPORT_SIZE:
        report = bytes(led_buf[:LED_REPORT_SIZE])
        del led_buf[:LED_REPORT_SIZE]
        led = report[-1]  # last byte = LED state (skips a report ID if present)
        count += 1
    return led, count

def read_led(timeout):
    """Read the latest host LED report within timeout seconds. Returns (byte, got).

    Keyboard events arriving during the wait are buffered in pending_events
    (not dropped) and replayed by the main loop.
    """
    led, got = 0, False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for key, _ in selector.select(timeout=0.1):
            if key.fileobj is not hid_out:
                try:
                    for ev in key.fileobj.read():
                        if ev.type == ecodes.EV_KEY:
                            pending_events.append((ev.code, ev.value, key.fileobj))
                except OSError:
                    pass
                continue
            chunk = os.read(hid_out.fileno(), 64)
            if chunk:
                led_buf.extend(chunk)
                parsed, count = parse_led_reports()
                if count and LED_REPORT_SIZE == 1:
                    # A host LED report is a single 1- or 2-byte frame; many
                    # fragments in one window means LED_REPORT_SIZE is slicing
                    # frames the wrong way (NumLock flapping will follow).
                    if count > 2:
                        print(f"WARNING: {count} LED fragments in one read window "
                              f"(LED_REPORT_SIZE={LED_REPORT_SIZE}). If the NumLock "
                              "state below flaps or numpad mode is inverted, set "
                              "LED_REPORT_SIZE=2 for a report-ID descriptor.")
                if parsed is not None:
                    led, got = parsed, True  # last report wins
    return led, got

initial_led, got_led = read_led(1.0)
if not got_led:
    # Host only reports LEDs on state change: toggle NumLock twice (net state
    # unchanged) to force it to send its current lock state.
    print("No initial LED report; probing host lock state...")
    toggle_key_off(0x53)
    toggle_key_off(0x53)
    time.sleep(0.3)  # let both reports arrive; the buffer keeps the last one
    initial_led, got_led = read_led(1.0)

if got_led:
    num_lock_active = bool(initial_led & 0x01)
    print(f"Host LED report: 0x{initial_led:02x} (Num={'ON' if initial_led & 0x01 else 'off'}, "
          f"Caps={'ON' if initial_led & 0x02 else 'off'}, Scroll={'ON' if initial_led & 0x04 else 'off'})")
    if AUTO_CLEAR_LOCKS:
        # NumLock is deliberately NOT cleared: numpad mode follows the LED state
        # (num_lock_active was already set from initial_led above)
        for bit, hid_key, name in ((0x02, 0x39, 'CapsLock'), (0x04, 0x47, 'ScrollLock')):
            if initial_led & bit:
                print(f"Auto-clearing {name}...")
                toggle_key_off(hid_key)
        time.sleep(0.1)  # let the host process the toggles and reply with fresh LED reports

# Unconditional drain, AFTER adoption and auto-clear: anything still in
# led_buf arrived after the read window closed, so it is NEWER than the byte
# adopted above (including the host's reply to the auto-clear toggles).
# Without this, leftover frames would be applied late by the main loop as a
# stale "host" lock change.
drained, drained_count = parse_led_reports()
if drained_count:
    num_lock_active = bool(drained & 0x01)
    print(f"Startup drain: adopting late LED report 0x{drained:02x} "
          f"(Num={'ON' if drained & 0x01 else 'off'})")

print("4. Safe macro architecture live...")

try:
    while True:
        # NumLock watchdog: if a local toggle was never confirmed by a host LED
        # report (press lost, or report dropped), re-probe. The double-toggle is
        # net-zero on the host, so the report afterwards reflects true state.
        # Skipped entirely when the USB pipeline is dead (probing would only
        # spam failing writes and could flip host state on a half-dead link).
        if (numlock_probe_deadline is not None and not usb_dead
                and time.monotonic() >= numlock_probe_deadline):
            numlock_probe_deadline = None
            print("NumLock toggle unconfirmed by host; re-probing lock state...")
            toggle_key_off(0x53)
            toggle_key_off(0x53)
            led, got = read_led(0.5)
            if got:
                num_lock_active = bool(led & 0x01)
                numlock_retries = 0
                print(f"Re-probe: host NumLock={'ON' if num_lock_active else 'off'}")
            else:
                # Cap the re-probe: each attempt physically toggles the host
                # NumLock twice, so a dead LED path must not retry forever.
                # Backoff x2 (capped at 4 s); after 5 failures give up until
                # the next physical NumLock press (re-arms in process_event).
                numlock_retries += 1
                if numlock_retries >= 5:
                    print("NumLock host sync unavailable after 5 re-probes; "
                          "trusting local state until the next NumLock press.")
                else:
                    backoff = min(NUMLOCK_PROBE_TIMEOUT * (2 ** numlock_retries), 4.0)
                    numlock_probe_deadline = time.monotonic() + backoff

        # Replay keyboard events buffered while probing LEDs (read_led)
        while pending_events:
            process_event(*pending_events.pop(0))

        events = selector.select(timeout=0.01)
        for key, _ in events:
            device = key.fileobj

            # Host LED output report: the definitive lock state
            if device is hid_out:
                try:
                    chunk = os.read(hid_out.fileno(), 64)
                except OSError:
                    continue
                if chunk:
                    led_buf.extend(chunk)
                    while True:
                        led, _ = parse_led_reports()
                        if led is None:
                            break
                        host_num = bool(led & 0x01)
                        if numlock_probe_deadline is not None:
                            numlock_probe_deadline = None  # host confirmed its lock state
                        if DEBUG_LEDS:
                            print(f"LED report: 0x{led:02x} (host NumLock={'ON' if host_num else 'off'})")
                        if host_num != num_lock_active:
                            print(f"LED sync: adopting host state -> numpad {'ENABLED' if host_num else 'disabled'}")
                        num_lock_active = host_num
                continue

            try:
                events_batch = device.read()
            except OSError as e:
                print(f"Stream {device.path} dropped: {e}")
                release_device_keys(device)
                selector.unregister(device)
                continue
            for event in events_batch:
                if event.type == ecodes.EV_KEY:
                    process_event(event.code, event.value, device)

finally:
    print("\n[Safety Cleanup] Releasing held state strings...")
    try:
        hid_out.write(bytes([0]*8))
        hid_out.flush()
    except OSError:
        pass
    try:
        selector.unregister(hid_out)
        for dev in devices:
            selector.unregister(dev)
        selector.close()
    except Exception:
        pass
    try:
        hid_out.close()
    except OSError:
        pass
