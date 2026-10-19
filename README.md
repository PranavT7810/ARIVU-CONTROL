# Arivu Control

**Arivu Control** is a Windows desktop app that lets you control your system — volume, screen brightness, media playback, and screen lock — using hand gestures captured by your webcam. It runs quietly in the background with a small floating widget, and uses [MediaPipe](https://developers.google.com/mediapipe) for real-time hand tracking, so no extra hardware is needed beyond a webcam.

![platform](https://img.shields.io/badge/platform-Windows-blue) ![python](https://img.shields.io/badge/python-3.9%2B-blue) ![license](https://img.shields.io/badge/license-MIT-green)

## Features

- 🖐️ **Gesture-based control** of volume, brightness, media play/pause, and screen lock
- 🎥 **Live camera overlay** showing hand landmarks, the detected gesture, and action progress
- 🪟 **Floating always-on-top widget** with a compact "mini mode" for minimal screen footprint
- 🔋 **Battery status** display (for laptops)
- ⌨️ **Global hotkeys** to close, hide, or pause the app from anywhere
- 🛑 **Safety features**: camera-blocked detection, confirmation dialogs, hold-to-confirm for sensitive actions (lock screen, media control)
- 🎨 Clean, rounded, dark-themed UI built with Tkinter — no browser or heavy UI framework required
- 📦 Automatically downloads the MediaPipe hand-landmark model on first run

## Gestures

| Gesture | Icon | Action |
|---|---|---|
| Open hand | ✋ | Volume up |
| Fist | ✊ | Volume down |
| Two hands open | ✋✋ | Brightness up |
| Two hands fist | ✊✊ | Brightness down |
| Peace sign · hold | ✌️ | Pause music |
| Point · hold | ☝️ | Play music |
| Index + pinky · hold 2s | 🤟 | Lock screen |

Gestures that trigger a continuous effect (volume/brightness) activate after a short arm/engage delay; gestures that trigger a one-off action (play/pause/lock) require holding the pose steady for a short countdown to avoid accidental triggers.

## Hotkeys

| Shortcut | Action |
|---|---|
| `Ctrl + Shift + L` | Close the app |
| `Ctrl + Shift + H` | Hide / show the widget (mini mode) |
| `Ctrl + Shift + P` | Pause / resume gesture control |

## Requirements

- Windows 10/11
- Python 3.9+
- A webcam
- The following Python packages:

```
opencv-python
mediapipe
numpy
pycaw
comtypes
screen-brightness-control
pillow
```

> `pycaw` and `comtypes` are used for system volume control, `screen-brightness-control` for brightness, and `pillow` for rendering the rounded app icon. These are Windows-specific — the app relies on `ctypes.windll` and will not run on macOS/Linux as-is.

## Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/<your-username>/arivu-control.git
   cd arivu-control
   ```

2. Install the dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Run the app:

   ```bash
   python gesture_control.py
   ```

On first launch, Arivu Control will automatically download the MediaPipe hand-landmarker model (`hand_landmarker.task`) into the project folder.

## Usage

- Launch the app — a small camera widget appears pinned to the right edge of your screen.
- Show your hand to the webcam and hold a gesture steady to trigger the matching action.
- Click **—** to collapse the widget into a compact mini bar, or **?** to toggle the gesture reference panel.
- Click the pause button (or press `Ctrl+Shift+P`) to temporarily stop gesture detection without closing the app.
- Use the **S / M / L** buttons to resize the camera preview.

## How it works

Arivu Control captures webcam frames and runs them through MediaPipe's `HandLandmarker` model in video mode to detect up to two hands per frame. Finger positions are classified into simple poses (open, fist, point, peace, lock) by comparing landmark distances. Detected gestures are debounced over several frames and require either a short "arm" delay (continuous controls) or a hold timer (discrete actions) before firing, to prevent accidental triggers. System integration is handled through:

- **`pycaw`** — master volume get/set and audio session peak metering (to detect whether media is currently playing)
- **`screen-brightness-control`** — reading and nudging display brightness on a background thread
- **Win32 API (`ctypes.windll`)** — simulating the media play/pause key, locking the workstation, registering global hotkeys, reading battery status, and shaping/positioning the borderless window

## Troubleshooting

- **"Camera not found"** — make sure no other application is using the webcam and that camera access is allowed for desktop apps in Windows privacy settings.
- **"Volume/Brightness control is not available"** — some audio devices or displays (e.g. external monitors without DDC/CI support) aren't supported by `pycaw` / `screen-brightness-control`.
- Logs are written to `gesture_control.log` in the project folder — check there first if something isn't working.

## Credits

Code by **T Harin Pranav**

## License

This project is licensed under the MIT License — see the `LICENSE` file for details.
