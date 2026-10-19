import ctypes
import math
import os
import queue
import threading
import time
import traceback
import urllib.request
from ctypes import POINTER, cast, wintypes
import tkinter as tk

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

try:
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Arivu.Control")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(HERE, "hand_landmarker.task")
LOG_PATH = os.path.join(HERE, "gesture_control.log")
ICON_PATH = os.path.join(HERE, "logoico.ico")
MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/hand_landmarker/"
             "hand_landmarker/float16/1/hand_landmarker.task")

ACTIVATE_DELAY = 1.0


def log(msg):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


BG, CARD, LINE, TEXT, MUTED = "#12161C", "#1B212A", "#2B3340", "#EAF0F8", "#8D99AB"
ACCENT, DARK = "#6C8CFF", "#0B0F14"
COLORS = {
    "vol_up": "#3DDC84", "vol_down": "#FF9F43",
    "bright_up": "#FFD43B", "bright_down": "#4DA3FF",
    "media_pause": "#2DD4BF", "media_play": "#F472B6",
    "lock": "#FF5C5C", "none": "#5B6678", "paused": "#9B7BFF",
    "blocked": "#FF5C5C",
}
BANNER = {
    "vol_up": "Volume up", "vol_down": "Volume down",
    "bright_up": "Brightness up", "bright_down": "Brightness down",
    "media_pause": "Hold to pause music", "media_play": "Hold to play music",
    "lock": "Hold to lock screen", "none": "Ready",
}
CONTROL_GROUP = {"vol_up": "volume", "vol_down": "volume",
                 "bright_up": "brightness", "bright_down": "brightness"}
ARM_TEXT = {"volume": "Hold to adjust volume...", "brightness": "Hold to adjust brightness..."}

H_OPEN, H_FIST = (1, 1, 1, 1, 1), (0, 0, 0, 0, 0)
H_PEACE, H_POINT, H_LOCK = (0, 1, 1, 0, 0), (0, 1, 0, 0, 0), (0, 1, 0, 0, 1)
ROWS = [
    ("vol_up", [H_OPEN], "Open hand", "Volume up"),
    ("vol_down", [H_FIST], "Fist", "Volume down"),
    ("bright_up", [H_OPEN, H_OPEN], "Two hands open", "Brightness up"),
    ("bright_down", [H_FIST, H_FIST], "Two hands fist", "Brightness down"),
    ("media_pause", [H_PEACE], "Peace sign · hold", "Pause music"),
    ("media_play", [H_POINT], "Point · hold", "Play music"),
    ("lock", [H_LOCK], "Index + pinky · 2s", "Lock screen"),
]

HOLD_TIME = {"media_pause": 0.7, "media_play": 0.7, "lock": 2.0}
HOLD_TEXT = {
    "media_pause": "Pausing... lower your hand to cancel",
    "media_play": "Playing... lower your hand to cancel",
    "lock": "Locking... lower your hand to cancel",
}


MANY_HANDS_TEXT = "More than two hands - using the closest"


def hex_to_bgr(h):
    return int(h[5:7], 16), int(h[3:5], 16), int(h[1:3], 16)


BGR = {k: hex_to_bgr(v) for k, v in COLORS.items()}
FONT = cv2.FONT_HERSHEY_SIMPLEX


class VolumeControl:
    def __init__(self):
        self.ep = None
        try:
            import comtypes
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            try:
                comtypes.CoInitialize()
            except Exception:
                pass
            device = AudioUtilities.GetSpeakers()
            try:
                self.ep = device.EndpointVolume
            except AttributeError:
                iface = device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
                self.ep = cast(iface, POINTER(IAudioEndpointVolume))
        except Exception:
            log("Volume control unavailable:\n" + traceback.format_exc())

    @property
    def ok(self):
        return self.ep is not None

    def get(self):
        try:
            return int(round(self.ep.GetMasterVolumeLevelScalar() * 100))
        except Exception:
            return None

    def set(self, percent):
        try:
            self.ep.SetMasterVolumeLevelScalar(max(0, min(100, percent)) / 100.0, None)
        except Exception:
            pass


class MediaControl:
    VK_MEDIA_PLAY_PAUSE = 0xB3

    def is_playing(self):
        try:
            from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
            meters = []
            for sess in AudioUtilities.GetAllSessions():
                try:
                    meters.append(sess._ctl.QueryInterface(IAudioMeterInformation))
                except Exception:
                    continue
            if not meters:
                return False
            for _ in range(6):
                for m in meters:
                    try:
                        if m.GetPeakValue() > 0.002:
                            return True
                    except Exception:
                        continue
                time.sleep(0.05)
            return False
        except Exception:
            log("Audio detection failed:\n" + traceback.format_exc())
            return None

    def press(self):
        try:
            u = ctypes.windll.user32
            u.keybd_event(self.VK_MEDIA_PLAY_PAUSE, 0, 1, 0)
            u.keybd_event(self.VK_MEDIA_PLAY_PAUSE, 0, 3, 0)
        except Exception:
            log("Media key failed:\n" + traceback.format_exc())

    def pause(self):
        playing = self.is_playing()
        if playing is False:
            return "Nothing is playing"
        self.press()
        return "Paused"

    def play(self):
        playing = self.is_playing()
        if playing is True:
            return "Already playing"
        self.press()
        return "Playing"


class BrightnessControl(threading.Thread):

    def __init__(self):
        super().__init__(daemon=True)
        self.value = None
        self.available = False
        self._target = None
        self._wake = threading.Event()
        self._ready = threading.Event()
        self.start()
        self._ready.wait(8)

    def run(self):
        sbc = None
        try:
            try:
                import pythoncom
                pythoncom.CoInitialize()
            except Exception:
                pass
            import screen_brightness_control as sbc
            v = sbc.get_brightness()
            self.value = int(v[0] if isinstance(v, list) else v)
            self.available = True
        except Exception:
            log("Brightness control unavailable:\n" + traceback.format_exc())
        self._ready.set()
        while self.available:
            self._wake.wait()
            self._wake.clear()
            try:
                sbc.set_brightness(self._target)
            except Exception:
                log("Brightness change failed:\n" + traceback.format_exc())

    def nudge(self, delta):
        if not self.available:
            return
        self.value = max(0, min(100, self.value + delta))
        self._target = self.value
        self._wake.set()


def lock_screen():
    try:
        ctypes.windll.user32.LockWorkStation()
    except Exception:
        log("Lock failed:\n" + traceback.format_exc())


class PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


def battery_info():
    try:
        st = PowerStatus()
        if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(st)):
            return None
        if st.BatteryFlag == 128 or st.BatteryLifePercent > 100:
            return None
        return int(st.BatteryLifePercent), st.ACLineStatus == 1
    except Exception:
        return None


def work_area(root):
    try:
        rect = wintypes.RECT()
        ctypes.windll.user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
        return rect.left, rect.top, rect.right, rect.bottom
    except Exception:
        return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def show_in_taskbar(root):
    try:
        user32 = ctypes.windll.user32
        root.update_idletasks()
        hwnd = user32.GetParent(root.winfo_id())
        style = user32.GetWindowLongW(hwnd, -20)
        style = (style & ~0x00000080) | 0x00040000
        user32.SetWindowLongW(hwnd, -20, style)
        root.withdraw()
        root.after(30, root.deiconify)
    except Exception:
        log("Taskbar button failed:\n" + traceback.format_exc())


def round_window(win, radius):
    state = {"size": None}

    def apply(event=None):
        try:
            if event is not None and event.widget is not win:
                return
            w, h = win.winfo_width(), win.winfo_height()
            if w < 20 or h < 20 or state["size"] == (w, h):
                return
            state["size"] = (w, h)
            hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
            rgn = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, w + 1, h + 1, radius * 2, radius * 2)
            ctypes.windll.user32.SetWindowRgn(hwnd, rgn, True)
        except Exception:
            log("Rounded corners failed:\n" + traceback.format_exc())

    win.bind("<Configure>", apply, add="+")
    win.bind("<Map>", apply, add="+")
    win.after(60, apply)


def start_hotkeys(q):
    def run():
        user32 = ctypes.windll.user32
        mods = 0x0002 | 0x0004 | 0x4000
        failed = []
        for hid, key in ((1, "L"), (2, "H"), (3, "P")):
            if not user32.RegisterHotKey(None, hid, mods, ord(key)):
                failed.append("Ctrl+Shift+" + key)
        if failed:
            q.put(("failed", failed))
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == 0x0312:
                q.put(int(msg.wParam))
    threading.Thread(target=run, daemon=True).start()


def dist(a, b):
    return math.hypot(a.x - b.x, a.y - b.y)


def extended_fingers(lm):
    return [dist(lm[0], lm[tip]) > dist(lm[0], lm[pip]) * 1.1
            for tip, pip in ((8, 6), (12, 10), (16, 14), (20, 18))]


def classify_hand(lm):
    index, middle, ring, pinky = extended_fingers(lm)
    count = [index, middle, ring, pinky].count(True)
    if count == 4:
        return "open"
    if count == 0:
        return "fist"
    if index and pinky and not middle and not ring:
        return "lock"
    if index and middle and not ring and not pinky:
        return "peace"
    if index and not middle and not ring and not pinky:
        return "point"
    return "other"


def hand_size(lm, fw, fh):
    return math.hypot((lm[0].x - lm[9].x) * fw, (lm[0].y - lm[9].y) * fh)


def decide_action(labels):
    if len(labels) == 2:
        if labels.count("open") == 2:
            return "bright_up"
        if labels.count("fist") == 2:
            return "bright_down"
        return "none"
    if len(labels) == 1:
        return {"open": "vol_up", "fist": "vol_down", "lock": "lock",
                "peace": "media_pause", "point": "media_play"}.get(labels[0], "none")
    return "none"


HAND_LINES = [
    (0, 1), (1, 2), (2, 3), (3, 4), (0, 5), (5, 6), (6, 7), (7, 8),
    (5, 9), (9, 10), (10, 11), (11, 12), (9, 13), (13, 14), (14, 15), (15, 16),
    (13, 17), (17, 18), (18, 19), (19, 20), (0, 17),
]


def to_frame(img):
    h, w = img.shape[:2]
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    return w, h, (b"P6 %d %d 255 " % (w, h)) + rgb.tobytes()


def pill(img, msg, x, y, color, f, scale=0.6, center=False):
    s, thick, pad = scale * f, max(1, int(2 * f)), int(8 * f)
    (tw, th), _ = cv2.getTextSize(msg, FONT, s, thick)
    bw, bh = tw + 2 * pad, th + 2 * pad
    if center:
        x = (img.shape[1] - bw) // 2
    overlay = img.copy()
    cv2.rectangle(overlay, (x, y), (x + bw, y + bh), color, -1)
    cv2.addWeighted(overlay, 0.8, img, 0.2, 0, img)
    lum = 0.114 * color[0] + 0.587 * color[1] + 0.299 * color[2]
    fg = (20, 20, 20) if lum > 150 else (255, 255, 255)
    cv2.putText(img, msg, (x + pad, y + pad + th), FONT, s, fg, thick, cv2.LINE_AA)


def draw_progress(img, progress, color, text, f):
    h, w = img.shape[:2]
    bx0, bx1, by = int(18 * f), w - int(18 * f), h - int(30 * f)
    cv2.rectangle(img, (bx0, by), (bx1, by + int(12 * f)), (60, 60, 60), -1)
    cv2.rectangle(img, (bx0, by), (bx0 + int((bx1 - bx0) * progress), by + int(12 * f)), color, -1)
    pill(img, text, 0, by - int(40 * f), color, f, 0.5, center=True)


def draw_corners(img, f):
    h, w = img.shape[:2]
    m, length, t = int(10 * f), int(24 * f), max(1, int(2 * f))
    for x, y, dx, dy in ((m, m, 1, 1), (w - m, m, -1, 1), (m, h - m, 1, -1), (w - m, h - m, -1, -1)):
        cv2.line(img, (x, y), (x + dx * length, y), (210, 210, 210), t)
        cv2.line(img, (x, y), (x, y + dy * length), (210, 210, 210), t)


def draw_hand(img, lm, label, ox, oy, rw, rh, f, dim=False):
    line_c, dot_c, text_c = ((120, 120, 120), (140, 140, 140), (160, 160, 160)) if dim else (
        (255, 255, 255), (0, 140, 255), (0, 255, 255))
    pts = [(ox + int(p.x * rw), oy + int(p.y * rh)) for p in lm]
    for a, b in HAND_LINES:
        cv2.line(img, pts[a], pts[b], line_c, max(1, int(2 * f)), cv2.LINE_AA)
    for p in pts:
        cv2.circle(img, p, max(2, int(4 * f)), dot_c, -1, cv2.LINE_AA)
    cv2.putText(img, label, (pts[0][0] - int(18 * f), pts[0][1] + int(30 * f)),
                FONT, 0.6 * f, text_c, max(1, int(2 * f)), cv2.LINE_AA)


class Shared:
    def __init__(self):
        self.frame = None
        self.cam_size = (440, 330)
        self.action = "none"
        self.status = "Starting..."
        self.detail = ""
        self.progress = None
        self.progress_color = COLORS["none"]
        self.remaining = None
        self.camera_blocked = False
        self.notice = ""
        self.hint = ""
        self.volume = None
        self.brightness = None
        self.paused = False
        self.visible = True
        self.suspend = False
        self.stop = False


class Worker(threading.Thread):
    STABLE_FRAMES = 4
    HOLD_GRACE = 0.25
    MOVE_LIMIT = 0.05
    ARM_GRACE = 0.25
    ENGAGE_GRACE = 1.0
    BLACK_LEVEL = 3.0
    BLACK_SECONDS = 3.0
    MIN_HAND_SIZE = 0.12

    def __init__(self, shared):
        super().__init__(daemon=True)
        self.s = shared

    def message(self, text):
        w, h = self.s.cam_size
        img = np.zeros((h, w, 3), np.uint8)
        f = w / 440.0
        (tw, th), _ = cv2.getTextSize(text, FONT, 0.6 * f, 2)
        cv2.putText(img, text, ((w - tw) // 2, (h + th) // 2), FONT, 0.6 * f,
                    (230, 230, 230), max(1, int(2 * f)), cv2.LINE_AA)
        self.s.frame = to_frame(img)
        self.s.status = text

    def run(self):
        try:
            self.loop()
        except Exception:
            log(traceback.format_exc())
            self.message("Something went wrong - see gesture_control.log")

    def loop(self):
        s = self.s
        self.message("Starting...")
        volume = VolumeControl()
        media = MediaControl()
        bright = BrightnessControl()
        s.volume = volume.get() if volume.ok else None
        s.brightness = bright.value

        if not os.path.exists(MODEL_PATH):
            self.message("Downloading hand model (first run)...")
            urllib.request.urlretrieve(MODEL_URL, MODEL_PATH)

        options = vision.HandLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=MODEL_PATH),
            running_mode=vision.RunningMode.VIDEO,
            num_hands=4,
            min_hand_detection_confidence=0.6,
            min_hand_presence_confidence=0.6,
            min_tracking_confidence=0.5,
        )

        cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
        if not cap.isOpened():
            cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            self.message("Camera not found")
            return
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

        t0 = time.monotonic()
        last_ts = -1
        last_raw, streak = "none", 0
        last_vol = last_bri = last_sync = 0.0
        hold_action, hold_start, hold_lost = None, 0.0, 0.0
        hold_anchor = None
        latched = None
        cooldown_until = 0.0
        flash_text, flash_until = "", 0.0
        engaged, engaged_seen = None, 0.0
        arm, arm_start, arm_seen = None, 0.0, 0.0
        dark_since, blocked = None, False
        fails = 0

        with vision.HandLandmarker.create_from_options(options) as model:
            while not s.stop:
                ok, raw = cap.read()
                if not ok:
                    fails += 1
                    if fails > 30:
                        self.message("Camera stopped responding")
                    time.sleep(0.05)
                    continue
                fails = 0
                now = time.monotonic()

                if float(raw[::8, ::8].mean()) < self.BLACK_LEVEL:
                    if dark_since is None:
                        dark_since = now
                    elif now - dark_since > self.BLACK_SECONDS:
                        blocked = True
                else:
                    dark_since, blocked = None, False
                s.camera_blocked = blocked

                w, h = s.cam_size
                f = w / 440.0
                flipped = cv2.flip(raw, 1)
                fh, fw = flipped.shape[:2]
                k = min(w / fw, h / fh)
                rw, rh = int(fw * k), int(fh * k)
                ox, oy = (w - rw) // 2, (h - rh) // 2
                canvas = np.zeros((h, w, 3), np.uint8)
                canvas[oy:oy + rh, ox:ox + rw] = cv2.resize(flipped, (rw, rh))

                labels, hint, action = [], "", "none"
                wrist, cut_off = None, False
                arm_text = None
                prog, prog_left = None, None
                extra = False

                if s.paused or blocked:
                    canvas = (canvas * 0.35).astype(np.uint8)
                    last_raw, streak, hold_action, latched = "none", 0, None, None
                    engaged = arm = None
                else:
                    rgb = cv2.cvtColor(flipped, cv2.COLOR_BGR2RGB)
                    image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                    ts = max(int((now - t0) * 1000), last_ts + 1)
                    last_ts = ts
                    result = model.detect_for_video(image, ts)

                    all_hands = list(result.hand_landmarks)
                    hands = [m for m in all_hands if hand_size(m, fw, fh) >= self.MIN_HAND_SIZE * fh]
                    far_hands = [m for m in all_hands if all(m is not n for n in hands)]
                    if far_hands and not hands:
                        hint = "Hand too far - move closer"
                    if len(hands) > 2:
                        extra = True
                        ranked = sorted(hands, key=lambda m: hand_size(m, fw, fh), reverse=True)
                        biggest = hand_size(ranked[0], fw, fh)
                        hands = [ranked[0]] + [m for m in ranked[1:2]
                                               if hand_size(m, fw, fh) >= 0.6 * biggest]
                        far_hands += [m for m in ranked if all(m is not k_ for k_ in hands)]
                        hint = MANY_HANDS_TEXT
                    for lm in far_hands:
                        draw_hand(canvas, lm, "far", ox, oy, rw, rh, f, dim=True)

                    for lm in hands:
                        label = classify_hand(lm)
                        labels.append(label)
                        draw_hand(canvas, lm, label, ox, oy, rw, rh, f)
                        xs, ys = [p.x for p in lm], [p.y for p in lm]
                        wrist = (lm[0].x, lm[0].y)
                        if min(xs) < 0.01 or max(xs) > 0.99 or min(ys) < 0.01 or max(ys) > 0.99:
                            cut_off = True
                            hint = "Hand cut off - move back or to the center"
                        elif max(ys) - min(ys) > 0.9:
                            hint = "Too close - move your hand back"

                    raw_action = decide_action(labels)
                    if cut_off:
                        raw_action = "none"
                        engaged = arm = None
                    streak = streak + 1 if raw_action == last_raw else 1
                    last_raw = raw_action
                    action = raw_action if (streak >= self.STABLE_FRAMES and not s.suspend) else "none"

                    group = CONTROL_GROUP.get(action)
                    ready = False
                    if group:
                        if engaged == group:
                            engaged, engaged_seen, arm = group, now, None
                            ready = True
                        else:
                            if arm != group:
                                arm, arm_start = group, now
                            arm_seen = now
                            progress = min(1.0, (now - arm_start) / ACTIVATE_DELAY)
                            if progress >= 1.0:
                                engaged, engaged_seen, arm = group, now, None
                                ready = True
                            else:
                                arm_text = ARM_TEXT[group]
                                prog, prog_left = progress, ACTIVATE_DELAY * (1.0 - progress)
                                draw_progress(canvas, progress, BGR[action], arm_text, f)
                    else:
                        if arm and now - arm_seen > self.ARM_GRACE:
                            arm = None
                        if engaged and now - engaged_seen > self.ENGAGE_GRACE:
                            engaged = None

                    if ready and action in ("vol_up", "vol_down"):
                        if not volume.ok:
                            hint = "Volume control is not available"
                        elif now - last_vol > 0.1:
                            cur = volume.get()
                            if cur is not None:
                                new = max(0, min(100, cur + (2 if action == "vol_up" else -2)))
                                volume.set(new)
                                s.volume = new
                                flash_text, flash_until = f"Volume {new}%", now + 0.9
                            last_vol = now

                    if ready and action in ("bright_up", "bright_down"):
                        if not bright.available:
                            hint = "Brightness control is not available"
                        elif now - last_bri > 0.3:
                            bright.nudge(5 if action == "bright_up" else -5)
                            s.brightness = bright.value
                            flash_text, flash_until = f"Brightness {bright.value}%", now + 0.9
                            last_bri = now

                    if latched is not None and action != latched and action != "none":
                        latched = None
                    if latched is not None and action == "none" and last_raw != latched:
                        latched = None

                    if action in HOLD_TIME and action != latched and now >= cooldown_until and not cut_off:
                        if hold_action != action:
                            hold_action, hold_start, hold_anchor = action, now, wrist
                        hold_lost = now
                        if wrist and hold_anchor and math.hypot(wrist[0] - hold_anchor[0],
                                                                wrist[1] - hold_anchor[1]) > self.MOVE_LIMIT:
                            hold_start, hold_anchor = now, wrist
                            hint = "Hold your hand still"
                        progress = min(1.0, (now - hold_start) / HOLD_TIME[action])
                        prog, prog_left = progress, HOLD_TIME[action] * (1.0 - progress)
                        draw_progress(canvas, progress, BGR[action], HOLD_TEXT[action], f)
                        if progress >= 1.0:
                            if action == "lock":
                                lock_screen()
                                flash_text = "Locking"
                                flash_until = now + 1.4
                                cooldown_until = now + 10
                            elif action == "media_pause":
                                flash_text = media.pause()
                                flash_until = now + 1.4
                                cooldown_until = now + 1.5
                            else:
                                flash_text = media.play()
                                flash_until = now + 1.4
                                cooldown_until = now + 1.5
                            latched = action
                            hold_action = None
                            prog, prog_left = None, None
                    elif hold_action is not None and now - hold_lost > self.HOLD_GRACE:
                        hold_action = None

                    if now - last_sync > 1.0 and volume.ok:
                        v = volume.get()
                        if v is not None:
                            s.volume = v
                        last_sync = now

                draw_corners(canvas, f)
                if blocked:
                    pill(canvas, "Camera is blocked", 0, h // 2 - int(30 * f),
                         BGR["blocked"], f, 0.7, center=True)
                    pill(canvas, "Uncover the lens", 0, h // 2 + int(14 * f),
                         (40, 40, 40), f, 0.5, center=True)
                elif s.paused:
                    pill(canvas, "Paused - Ctrl+Shift+P to resume", 0, h // 2 - int(14 * f),
                         BGR["paused"], f, 0.6, center=True)
                else:
                    banner = arm_text or (BANNER[action] if labels else "Show your hand")
                    pill(canvas, banner, int(18 * f), int(18 * f), BGR[action], f)
                    if hint:
                        pill(canvas, hint, 0, int(58 * f), (40, 40, 40), f, 0.5, center=True)
                if now < flash_until:
                    sc, th_ = 1.1 * f, max(2, int(3 * f))
                    (tw, th), _ = cv2.getTextSize(flash_text, FONT, sc, th_)
                    pos = ((w - tw) // 2, (h + th) // 2)
                    cv2.putText(canvas, flash_text, pos, FONT, sc, (0, 0, 0), th_ + 3, cv2.LINE_AA)
                    cv2.putText(canvas, flash_text, pos, FONT, sc, (255, 255, 255), th_, cv2.LINE_AA)

                if blocked:
                    s.action = "blocked"
                    s.status = "Camera is blocked"
                elif s.paused:
                    s.action = "paused"
                    s.status = "Paused"
                else:
                    s.action = action
                    s.status = arm_text or (BANNER[action] if labels else "Show your hand")
                s.detail = flash_text if (now < flash_until and not blocked and not s.paused) else ""
                s.notice = "More than two hands" if (extra and not blocked and not s.paused) else ""
                s.hint = hint if (hint and hint != MANY_HANDS_TEXT and not blocked and not s.paused) else ""
                s.progress = prog
                s.remaining = prog_left
                s.progress_color = COLORS[action] if prog is not None else COLORS["none"]
                if s.visible:
                    s.frame = to_frame(canvas)

        cap.release()


class App:
    SIZES = [(360, 270), (440, 330), (560, 420)]
    SIZE_NAMES = ("S", "M", "L")
    MINI_BAR = (150, 6)

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("Arivu Control")
        try:
            if os.path.exists(ICON_PATH):
                self.root.iconbitmap(ICON_PATH)
        except Exception:
            log("Icon could not be loaded:\n" + traceback.format_exc())
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg=LINE)
        self.scale = self.root.winfo_fpixels("1i") / 96.0
        self.size_idx = 1
        self.show_help = True
        self.dialog = None
        self.dialog_kind = None
        self.dialog_confirm = None
        self.dialog_cancel = None
        self.cam_alerted = False
        self.photo = None
        self.last_frame = None
        self.shown_action = None
        self.shown_status = None
        self.shown_mini = None
        self.battery_shown = "unset"
        self.battery_checked = 0.0
        self.bat_value = None
        self.placed = False
        self.drag = (0, 0)
        self.mini_drag = (0, 0)
        self.hotkeys = queue.Queue()

        self.shared = Shared()
        self.apply_size()
        self.build_ui()
        self.build_mini()
        self.refresh_battery()
        self.place_corner()
        self.placed = True
        round_window(self.root, self.px(14))
        self.root.after(200, lambda: show_in_taskbar(self.root))

        self.worker = Worker(self.shared)
        self.worker.start()
        start_hotkeys(self.hotkeys)
        self.tick()

    def px(self, n):
        return int(n * self.scale)

    def apply_size(self):
        w, h = self.SIZES[self.size_idx]
        self.shared.cam_size = (self.px(w), self.px(h))

    def title_button(self, parent, text, command, hover=LINE, hover_fg=TEXT):
        b = tk.Label(parent, text=text, bg=CARD, fg=MUTED, font=("Segoe UI", 10, "bold"),
                     padx=self.px(8), pady=self.px(6), cursor="hand2")
        b.bind("<Enter>", lambda e: b.config(bg=hover, fg=hover_fg))
        b.bind("<Leave>", lambda e: b.config(bg=CARD, fg=MUTED))
        b.bind("<Button-1>", lambda e: command())
        return b

    def draw_icons(self, canvas, icons, fg, bg):
        s = self.scale
        canvas.delete("all")
        tips = (6, 4, 6, 10)
        for n, pat in enumerate(icons):
            ox = n * 34

            def X(v):
                return (ox + v) * s

            def Y(v):
                return v * s

            if not any(pat):
                canvas.create_line(X(12.5), Y(23), X(18.5), Y(23), width=15 * s,
                                   capstyle="round", fill=fg)
                for i in range(4):
                    cx = 8.6 + i * 4.6
                    canvas.create_oval(X(cx - 2.6), Y(11.5), X(cx + 2.6), Y(18), fill=fg, outline=fg)
                for i in range(1, 4):
                    x = 8.6 + i * 4.6 - 2.3
                    canvas.create_line(X(x), Y(13), X(x), Y(20), width=1.2 * s, fill=bg)
                canvas.create_line(X(9), Y(25.5), X(20), Y(25.5), width=7.6 * s,
                                   capstyle="round", fill=bg)
                canvas.create_line(X(9), Y(25.5), X(20), Y(25.5), width=4.8 * s,
                                   capstyle="round", fill=fg)
                continue

            canvas.create_line(X(10.5), Y(26), X(21), Y(26), width=13 * s, capstyle="round", fill=fg)
            for i, up in enumerate(pat[1:]):
                x = 9 + i * 5
                if up:
                    canvas.create_line(X(x), Y(22), X(x), Y(tips[i]), width=4.4 * s,
                                       capstyle="round", fill=fg)
                else:
                    canvas.create_line(X(x - 1.8), Y(21.5), X(x + 1.8), Y(21.5), width=1.4 * s,
                                       capstyle="round", fill=bg)
            if pat[0]:
                canvas.create_line(X(6.5), Y(26), X(2.5), Y(15), width=4.4 * s,
                                   capstyle="round", fill=fg)
            else:
                canvas.create_line(X(8), Y(27.5), X(14), Y(27.5), width=1.4 * s,
                                   capstyle="round", fill=bg)

    def make_bar(self, parent, name, color):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", pady=self.px(3))
        name_label = tk.Label(row, text=name, width=10, anchor="w", bg=BG, fg=MUTED,
                              font=("Segoe UI", 10))
        name_label.pack(side="left")
        value = tk.Label(row, text="n/a", width=5, anchor="e", bg=BG, fg=TEXT,
                         font=("Segoe UI", 10, "bold"))
        value.pack(side="right")
        canvas = tk.Canvas(row, height=self.px(10), bg=BG, highlightthickness=0, bd=0)
        canvas.pack(side="left", fill="x", expand=True, padx=self.px(6))
        return {"canvas": canvas, "value": value, "color": color, "row": row, "name": name_label}

    def build_ui(self):
        body = tk.Frame(self.root, bg=BG)
        body.pack(padx=1, pady=1)

        self.accent = tk.Frame(body, bg=COLORS["vol_up"], height=self.px(3))
        self.accent.pack(fill="x")

        bar = tk.Frame(body, bg=CARD)
        bar.pack(fill="x")
        self.logo_img = None
        try:
            from PIL import Image, ImageChops, ImageDraw, ImageTk
            if os.path.exists(ICON_PATH):
                size = self.px(20)
                big = Image.open(ICON_PATH).convert("RGBA").resize((size * 4, size * 4),
                                                                    Image.LANCZOS)
                mask = Image.new("L", big.size, 0)
                ImageDraw.Draw(mask).rounded_rectangle((0, 0, big.width - 1, big.height - 1),
                                                       radius=size, fill=255)
                big.putalpha(ImageChops.multiply(big.getchannel("A"), mask))
                self.logo_img = ImageTk.PhotoImage(big.resize((size, size), Image.LANCZOS))
        except Exception:
            log("Title bar logo failed:\n" + traceback.format_exc())
        logo = None
        if self.logo_img is not None:
            logo = tk.Label(bar, image=self.logo_img, bg=CARD)
            logo.pack(side="left", padx=(self.px(10), 0), pady=self.px(4))
        title = tk.Label(bar, text="Arivu Control", bg=CARD, fg=TEXT, font=("Segoe UI", 10, "bold"))
        title.pack(side="left", padx=(self.px(8) if logo else self.px(12), 0), pady=self.px(6))
        self.title_button(bar, "✕", self.ask_exit, hover="#FF5C5C", hover_fg="white").pack(side="right")
        self.title_button(bar, "—", self.toggle_visible).pack(side="right")
        self.pause_btn = self.title_button(bar, "⏸", self.ask_pause)
        self.pause_btn.pack(side="right")
        self.title_button(bar, "?", self.toggle_help).pack(side="right")

        seg = tk.Frame(bar, bg=CARD)
        seg.pack(side="right", padx=(0, self.px(6)))
        self.size_btns = []
        for i, name in enumerate(self.SIZE_NAMES):
            b = tk.Label(seg, text=name, width=2, font=("Segoe UI", 9, "bold"),
                         pady=self.px(2), cursor="hand2")
            b.pack(side="left", padx=1)
            b.bind("<Button-1>", lambda e, i=i: self.set_size(i))
            self.size_btns.append(b)
        self.paint_size_buttons()
        for widget in (w for w in (bar, title, logo) if w is not None):
            widget.bind("<ButtonPress-1>", self.drag_start)
            widget.bind("<B1-Motion>", self.drag_move)

        cw, ch = self.shared.cam_size
        self.blank = tk.PhotoImage(width=cw, height=ch)
        self.cam = tk.Label(body, image=self.blank, bg="black", bd=0)
        self.cam.pack()

        status = tk.Frame(body, bg=BG)
        status.pack(fill="x", padx=self.px(12), pady=(self.px(10), 0))
        self.status_dot = tk.Label(status, text="●", bg=BG, fg=COLORS["vol_up"], font=("Segoe UI", 10))
        self.status_dot.pack(side="left")
        self.status_text = tk.Label(status, text="Starting...", bg=BG, fg=TEXT,
                                    font=("Segoe UI", 11, "bold"))
        self.status_text.pack(side="left", padx=self.px(6))

        info = tk.Frame(body, bg=BG)
        info.pack(fill="x", padx=self.px(12), pady=(self.px(8), 0))
        self.vol_bar = self.make_bar(info, "Volume", COLORS["vol_up"])
        self.bri_bar = self.make_bar(info, "Brightness", COLORS["bright_up"])
        self.bat_bar = self.make_bar(info, "Battery", COLORS["vol_up"])

        self.instr = tk.Frame(body, bg=BG)
        self.instr.pack(fill="x", padx=self.px(12), pady=(self.px(10), 0))
        tk.Label(self.instr, text="GESTURES", bg=BG, fg=MUTED, anchor="w",
                 font=("Segoe UI", 8, "bold")).pack(fill="x", pady=(0, self.px(3)))
        self.rows = []
        for key, icons, gesture, result in ROWS:
            row = tk.Frame(self.instr, bg=CARD)
            row.pack(fill="x", pady=self.px(2))
            tk.Frame(row, bg=COLORS[key], width=self.px(5)).pack(side="left", fill="y")
            ic = tk.Canvas(row, width=self.px(68), height=self.px(36), bg=CARD,
                           highlightthickness=0, bd=0)
            ic.pack(side="left", padx=(self.px(8), 0), pady=self.px(3))
            self.draw_icons(ic, icons, COLORS[key], CARD)
            g = tk.Label(row, text=gesture, bg=CARD, fg=TEXT, font=("Segoe UI", 10, "bold"), anchor="w")
            g.pack(side="left", padx=self.px(8), pady=self.px(5))
            r = tk.Label(row, text=result, bg=CARD, fg=MUTED, font=("Segoe UI", 10), anchor="e")
            r.pack(side="right", padx=self.px(10))
            self.rows.append((key, row, ic, icons, g, r))

        self.footer = tk.Label(
            body,
            text=("Ctrl+Shift+L  close   ·   Ctrl+Shift+H  hide\n"
                  "Ctrl+Shift+P  pause"),
            bg=BG, fg=MUTED, font=("Segoe UI", 9), justify="center")
        self.footer.pack(pady=(self.px(9), self.px(4)))
        self.credit = tk.Label(body, text="CODE BY T HARIN PRANAV  |  ⒽⓅ", bg=BG, fg=TEXT,
                               font=("Segoe UI", 10, "bold"))
        self.credit.pack(pady=(0, self.px(10)))

    def build_mini(self):
        m = tk.Toplevel(self.root)
        m.withdraw()
        m.overrideredirect(True)
        m.attributes("-topmost", True)
        m.configure(bg=LINE)
        box = tk.Frame(m, bg=CARD, cursor="fleur")
        box.pack(padx=1, pady=1)
        self.mini_dot = tk.Label(box, text="●", bg=CARD, fg=COLORS["vol_up"], font=("Segoe UI", 11))
        self.mini_dot.pack(side="left", padx=(self.px(12), self.px(2)))
        col = tk.Frame(box, bg=CARD)
        col.pack(side="left", padx=self.px(6), pady=self.px(8))
        title_row = tk.Frame(col, bg=CARD)
        title_row.pack(fill="x")
        mini_logo = None
        if getattr(self, "logo_img", None) is not None:
            mini_logo = tk.Label(title_row, image=self.logo_img, bg=CARD)
            mini_logo.pack(side="left", padx=(0, self.px(6)))
        mini_title = tk.Label(title_row, text="Arivu Control", bg=CARD, fg=TEXT,
                              font=("Segoe UI", 9, "bold"), anchor="w")
        mini_title.pack(side="left")
        self.mini_battery = tk.Label(title_row, text="", bg=CARD, fg=MUTED,
                                     font=("Segoe UI", 8), anchor="e")
        self.mini_battery.pack(side="right")
        self.mini_status = tk.Label(col, text="Running in background", bg=CARD, fg=MUTED,
                                    font=("Segoe UI", 9), anchor="w", width=40)
        self.mini_status.pack(anchor="w")
        self.mini_levels = tk.Label(col, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                                    anchor="w")
        self.mini_levels.pack(anchor="w")
        bar_row = tk.Frame(col, bg=CARD)
        bar_row.pack(anchor="w", pady=(self.px(4), 0))
        bw, bh = self.MINI_BAR
        self.mini_bar = tk.Canvas(bar_row, width=self.px(bw), height=self.px(bh), bg=CARD,
                                  highlightthickness=0, bd=0)
        self.mini_bar.pack(side="left")
        self.mini_time = tk.Label(bar_row, text="", bg=CARD, fg=MUTED, font=("Segoe UI", 8),
                                  width=5, anchor="e")
        self.mini_time.pack(side="left", padx=(self.px(6), 0))
        show = tk.Label(box, text="Show", bg=ACCENT, fg=DARK, font=("Segoe UI", 9, "bold"),
                        padx=self.px(14), pady=self.px(6), cursor="hand2")
        show.pack(side="right", padx=self.px(10), pady=self.px(8))
        show.bind("<Button-1>", lambda e: self.toggle_visible())
        self.mini = m
        round_window(m, self.px(12))
        self.set_mini_bar(None, COLORS["none"])

        for widget in (m, box, self.mini_dot, col, title_row, mini_title, self.mini_battery,
                       self.mini_status, self.mini_levels,
                       bar_row, self.mini_bar, self.mini_time):
            widget.bind("<ButtonPress-1>", self.mini_drag_start)
            widget.bind("<B1-Motion>", self.mini_drag_move)
        if mini_logo is not None:
            mini_logo.bind("<ButtonPress-1>", self.mini_drag_start)
            mini_logo.bind("<B1-Motion>", self.mini_drag_move)

    def place_corner(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        left, top, right, bottom = work_area(self.root)
        m = self.px(14)
        y = max(top, top + (bottom - top - h) // 2)
        self.root.geometry(f"{w}x{h}+{right - w - m}+{y}")

    def reanchor(self):
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        right = self.root.winfo_x() + self.root.winfo_width()
        bottom = self.root.winfo_y() + self.root.winfo_height()
        l, t, r, b = work_area(self.root)
        x = max(l, min(right - w, r - w))
        y = max(t, min(bottom - h, b - h))
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    def drag_start(self, e):
        self.drag = (e.x_root - self.root.winfo_x(), e.y_root - self.root.winfo_y())

    def drag_move(self, e):
        self.root.geometry(f"+{e.x_root - self.drag[0]}+{e.y_root - self.drag[1]}")

    def mini_drag_start(self, e):
        self.mini_drag = (e.x_root - self.mini.winfo_x(), e.y_root - self.mini.winfo_y())

    def mini_drag_move(self, e):
        l, t, r, b = work_area(self.root)
        w, h = self.mini.winfo_width(), self.mini.winfo_height()
        x = max(l, min(e.x_root - self.mini_drag[0], r - w))
        y = max(t, min(e.y_root - self.mini_drag[1], b - h))
        self.mini.geometry(f"+{x}+{y}")

    def paint_size_buttons(self):
        for i, b in enumerate(self.size_btns):
            on = (i == self.size_idx)
            b.config(bg=ACCENT if on else LINE, fg=DARK if on else MUTED)

    def set_size(self, idx):
        if idx == self.size_idx:
            return
        self.size_idx = idx
        self.paint_size_buttons()
        self.apply_size()
        cw, ch = self.shared.cam_size
        self.blank = tk.PhotoImage(width=cw, height=ch)
        self.cam.config(image=self.blank)
        self.photo = self.last_frame = None
        self.reanchor()

    def toggle_help(self):
        self.show_help = not self.show_help
        if self.show_help:
            self.instr.pack(fill="x", padx=self.px(12), pady=(self.px(10), 0), before=self.footer)
        else:
            self.instr.pack_forget()
        self.reanchor()

    def toggle_visible(self):
        s = self.shared
        l, t, r, b = work_area(self.root)
        if s.visible:
            self.mini.update_idletasks()
            w, h = self.mini.winfo_reqwidth(), self.mini.winfo_reqheight()
            m = self.px(14)
            self.mini.geometry(f"+{r - w - m}+{b - h - m}")
            s.visible = False
            self.shown_status = None
            self.shown_mini = None
            self.root.withdraw()
            self.mini.deiconify()
            self.mini.lift()
            self.mini.attributes("-topmost", True)
        else:
            self.mini.update_idletasks()
            self.root.update_idletasks()
            self.place_corner()
            s.visible = True
            self.last_frame = None
            self.shown_status = None
            self.mini.withdraw()
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def set_paused(self, value):
        self.shared.paused = value
        self.pause_btn.config(text="▶" if value else "⏸")
        self.toast("Arivu Control paused" if value else "Arivu Control resumed", 1600)

    def ask_pause(self):
        if self.shared.paused:
            self.open_dialog("pause", "Resume Arivu Control?",
                             "Your hand will control volume, brightness,\nmusic and lock again.",
                             "Resume", COLORS["vol_up"], "#101010", "Stay paused",
                             lambda: self.set_paused(False))
        else:
            self.open_dialog("pause", "Pause Arivu Control?",
                             "Your hand will be ignored until you resume.\nCtrl+Shift+P brings this popup back.",
                             "Pause", COLORS["paused"], "white", "Keep running",
                             lambda: self.set_paused(True))

    def toast(self, msg, ms=2600):
        t = tk.Toplevel(self.root)
        t.overrideredirect(True)
        t.attributes("-topmost", True)
        t.configure(bg=LINE)
        tk.Label(t, text=msg, bg=CARD, fg=TEXT, font=("Segoe UI", 10),
                 padx=self.px(14), pady=self.px(9)).pack(padx=1, pady=1)
        t.update_idletasks()
        w, h = t.winfo_reqwidth(), t.winfo_reqheight()
        l, tp, r, b = work_area(self.root)
        m = self.px(14)
        anchor = None
        if self.shared.visible and self.root.winfo_viewable():
            anchor = self.root
        elif not self.shared.visible and self.mini.winfo_viewable():
            anchor = self.mini
        if anchor is not None:
            x = anchor.winfo_x() + anchor.winfo_width() - w
            y = anchor.winfo_y() - h - self.px(8)
        else:
            x, y = r - w - m, b - h - m
        t.geometry(f"+{max(0, x)}+{max(0, y)}")
        round_window(t, self.px(10))
        t.after(ms, t.destroy)

    def ask_exit(self):
        self.open_dialog("exit", "Close Arivu Control?",
                         "Volume, brightness, music and lock gestures will stop\nuntil you start the app again.",
                         "Close app", COLORS["lock"], "white", "Keep running", self.quit)

    def open_dialog(self, kind, title, message, ok_text, ok_color, ok_fg, no_text, on_ok):
        if self.dialog is not None:
            if self.dialog_kind == kind:
                self.dialog.lift()
                return
            self.dialog.destroy()
            self.dialog = None
        self.shared.suspend = True
        d = tk.Toplevel(self.root)
        self.dialog, self.dialog_kind = d, kind
        d.title(title)
        d.overrideredirect(True)
        d.attributes("-topmost", True)
        d.configure(bg=LINE)
        box = tk.Frame(d, bg=CARD)
        box.pack(padx=1, pady=1)
        tk.Frame(box, bg=ok_color, height=self.px(3)).pack(fill="x")
        tk.Label(box, text=title, bg=CARD, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(padx=self.px(32), pady=(self.px(22), self.px(6)))
        tk.Label(box, text=message,
                 bg=CARD, fg=MUTED, font=("Segoe UI", 10), justify="center").pack(padx=self.px(32))

        def cancel(_=None):
            self.shared.suspend = False
            self.dialog = self.dialog_kind = self.dialog_confirm = self.dialog_cancel = None
            d.destroy()

        def confirm(_=None):
            cancel()
            on_ok()

        self.dialog_confirm = confirm
        self.dialog_cancel = cancel

        buttons = tk.Frame(box, bg=CARD)
        buttons.pack(pady=self.px(20))
        tk.Button(buttons, text=ok_text, command=confirm, bg=ok_color, fg=ok_fg,
                  activebackground=ok_color, activeforeground=ok_fg, relief="flat",
                  font=("Segoe UI", 10, "bold"), padx=self.px(18), pady=self.px(6),
                  cursor="hand2").pack(side="left", padx=self.px(6))
        if no_text:
            tk.Button(buttons, text=no_text, command=cancel, bg=LINE, fg=TEXT,
                      activebackground="#3A4454", activeforeground=TEXT, relief="flat",
                      font=("Segoe UI", 10), padx=self.px(18), pady=self.px(6),
                      cursor="hand2").pack(side="left", padx=self.px(6))
            hint = "Enter = " + ok_text.lower() + "    Esc = " + no_text.lower()
            if kind == "pause":
                hint += "    P = " + ok_text.lower()
        else:
            hint = "Enter or Esc = " + ok_text.lower()
        tk.Label(box, text=hint, bg=CARD, fg=MUTED,
                 font=("Segoe UI", 9)).pack(pady=(0, self.px(14)))
        d.bind("<Return>", confirm)
        d.bind("<Escape>", cancel)

        d.update_idletasks()
        w, h = d.winfo_reqwidth(), d.winfo_reqheight()
        wl, wt, wr, wb = work_area(self.root)
        d.geometry(f"+{wl + (wr - wl - w) // 2}+{wt + (wb - wt - h) // 2}")
        round_window(d, self.px(14))
        d.lift()
        d.after(80, d.focus_force)

    def check_camera(self):
        if self.shared.camera_blocked:
            if not self.cam_alerted and self.dialog is None:
                self.cam_alerted = True
                self.open_dialog("camera", "Camera is blocked",
                                 "The camera picture is completely black.\n"
                                 "Uncover the lens or open the privacy shutter,\n"
                                 "and close other apps that may be using the camera.",
                                 "OK", COLORS["blocked"], "white", None, lambda: None)
        else:
            self.cam_alerted = False
            if self.dialog_kind == "camera" and self.dialog_cancel:
                self.dialog_cancel()

    def refresh_battery(self):
        info = battery_info()
        if info == self.battery_shown:
            return
        had_row = self.battery_shown not in ("unset", None)
        self.battery_shown = info
        if info is None:
            self.bat_value = None
            self.bat_bar["row"].pack_forget()
            self.mini_battery.config(text="")
        else:
            pct, charging = info
            self.bat_value = pct
            if charging:
                color = COLORS["bright_down"]
            elif pct >= 40:
                color = COLORS["vol_up"]
            elif pct >= 20:
                color = COLORS["vol_down"]
            else:
                color = COLORS["lock"]
            self.bat_bar["color"] = color
            self.bat_bar["name"].config(text="Battery ⚡" if charging else "Battery")
            if not had_row:
                self.bat_bar["row"].pack(fill="x", pady=self.px(3))
            self.mini_battery.config(text=f"Battery {pct}%" + (" ⚡" if charging else ""))
        if self.placed:
            self.reanchor()

    def quit(self):
        self.shared.stop = True
        self.worker.join(1.5)
        self.root.destroy()

    def set_bar(self, bar, value):
        c = bar["canvas"]
        w, h = c.winfo_width(), c.winfo_height()
        c.delete("all")
        if w > 1:
            r = h / 2
            c.create_line(r, r, w - r, r, width=h, capstyle="round", fill=LINE)
            if value:
                c.create_line(r, r, r + (w - 2 * r) * value / 100, r, width=h,
                              capstyle="round", fill=bar["color"])
        bar["value"].config(text="n/a" if value is None else f"{value}%")

    def set_mini_bar(self, progress, color):
        c = self.mini_bar
        w, h = self.px(self.MINI_BAR[0]), self.px(self.MINI_BAR[1])
        r = h / 2
        c.delete("all")
        c.create_line(r, r, w - r, r, width=h, capstyle="round", fill=LINE)
        if progress:
            c.create_line(r, r, r + (w - 2 * r) * progress, r, width=h,
                          capstyle="round", fill=color)

    def update_mini(self):
        s = self.shared
        idle = s.status in ("Show your hand", "Ready", "Starting...")
        if s.camera_blocked:
            text = "Camera is blocked - uncover the lens"
        elif s.detail:
            text = s.detail
        elif s.hint:
            text = s.hint
        elif s.notice:
            text = s.notice
        elif idle:
            text = "Running in background"
        else:
            text = s.status
        prog = None if s.progress is None else round(s.progress, 2)
        left = "" if s.remaining is None else f"{s.remaining:.1f}s"
        vol = "n/a" if s.volume is None else f"{s.volume}%"
        bri = "n/a" if s.brightness is None else f"{s.brightness}%"
        levels = f"Volume {vol}   ·   Brightness {bri}"
        key = (text, prog, left, s.action, levels)
        if key == self.shown_mini:
            return
        self.shown_mini = key
        self.mini_status.config(text=text)
        self.mini_levels.config(text=levels)
        self.mini_time.config(text=left)
        self.set_mini_bar(prog, s.progress_color)

    def highlight(self, action):
        color = COLORS[action] if action != "none" else COLORS["vol_up"]
        for key, row, ic, icons, g, r in self.rows:
            on = (key == action)
            bg = COLORS[key] if on else CARD
            row.config(bg=bg)
            ic.config(bg=bg)
            self.draw_icons(ic, icons, "#101010" if on else COLORS[key], bg)
            g.config(bg=bg, fg="#101010" if on else TEXT)
            r.config(bg=bg, fg="#101010" if on else MUTED)
        self.status_dot.config(fg=color)
        self.accent.config(bg=color)
        self.mini_dot.config(fg=color)

    def tick(self):
        s = self.shared
        while not self.hotkeys.empty():
            item = self.hotkeys.get()
            if isinstance(item, tuple):
                self.toast("Shortcut already used by another app: " + ", ".join(item[1]), 5000)
            elif item == 1:
                self.ask_exit()
            elif item == 2:
                self.toggle_visible()
            elif item == 3:
                if self.dialog_kind == "pause" and self.dialog_confirm:
                    self.dialog_confirm()
                else:
                    self.ask_pause()

        self.check_camera()

        now = time.monotonic()
        if now - self.battery_checked > 10:
            self.battery_checked = now
            self.refresh_battery()

        if s.action != self.shown_action:
            self.shown_action = s.action
            self.highlight(s.action)

        if s.status != self.shown_status:
            self.shown_status = s.status
            self.status_text.config(text=s.status)

        if s.visible:
            frame = s.frame
            if frame is not None and frame is not self.last_frame and (frame[0], frame[1]) == s.cam_size:
                self.last_frame = frame
                self.photo = tk.PhotoImage(width=frame[0], height=frame[1], data=frame[2], format="PPM")
                self.cam.config(image=self.photo)
            self.set_bar(self.vol_bar, s.volume)
            self.set_bar(self.bri_bar, s.brightness)
            if self.bat_value is not None:
                self.set_bar(self.bat_bar, self.bat_value)
        else:
            self.update_mini()
        self.root.after(25, self.tick)


def main():
    try:
        App().root.mainloop()
    except Exception:
        log(traceback.format_exc())
        try:
            from tkinter import messagebox
            messagebox.showerror("Arivu Control",
                                 "Something went wrong. Details were saved in gesture_control.log")
        except Exception:
            pass


if __name__ == "__main__":
    main()