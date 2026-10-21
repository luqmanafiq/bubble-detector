# BubbleMeter

Live bubble detection for a **static camera**: counts bubbles, measures each diameter, converts to
sphere volume, and writes everything to **Excel** while it runs. Built for a Raspberry Pi
(Tkinter GUI, no heavy dependencies), also runs on Windows/Linux/macOS.

https://github.com/user-attachments/assets/a804dbaa-74fa-430c-b55f-5c43cfcb483d

## Install on the Raspberry Pi
Raspberry Pi OS Bookworm, 64-bit, Pi 4 or (better) Pi 5:

    unzip bubblemeter.zip && cd bubblemeter_pkg
    bash install.sh

Then start it from the menu (Science > BubbleMeter), the desktop icon, or `bubblemeter`.
No screen attached? `bubblemeter-headless 0 --mm-per-px 0.03 --profile sensitive`.

To run on a laptop instead: `pip install opencv-python numpy openpyxl pillow`, then `python -m bubblemeter`.

## First run - do these three things
1. **Source**: `0` = first USB camera, `1` = second, `rtsp://...` = network camera, a file path = video file,
   `picamera` = Raspberry Pi ribbon camera. Press **Start**.
2. **Calibrate the scale.** `0.03 mm/px` was an assumption for the recorded WhatsApp clips; it is NOT
   valid for a new camera, lens, distance or resolution. Put an object of known size (ruler, caliper,
   a coin) in the SAME plane as the bubbles, press **Calibrate**, click both ends, type the real size in mm.
   Every diameter and volume depends on this (volume goes with the cube of it).
3. **Mask what is not bubbles**: drag a rectangle on the video over on-screen text, dials, cables or
   anything that flickers. "ROV text preset" loads the masks for the original ROV footage; "Clear masks"
   removes all.

Sensitivity: `normal` for clear dark bubbles, `sensitive` for faint/small ones (finds more, but also more
false detections - mask static clutter first). Detect width: 640 is a good Pi default; 480 is faster,
960 is slightly more accurate.

## What you get
`~/BubbleMeter/sessions/session_<date_time>/`
* `bubbles.csv` - one line per bubble, written the moment it is finished (safe against power loss).
* `bubbles.xlsx` - rebuilt every 20 s and when you press Stop or the video ends. Sheets:
  * **Summary** - count, total volume (mm3 / mL), mean/median/max diameter, gas rate (mL/min, mL/h),
    volume excluding low-confidence bubbles, and how much of the volume is the single largest bubble.
  * **Bubbles** - diameter (px), diameter (mm), volume (mm3), axes, method, confidence.
  * **Settings** - the mm-per-pixel cell. Diameters, volumes and the Summary are Excel *formulas*, so
    changing that one cell recalculates the whole workbook.

Volume of each bubble = pi/6 * d^3 (sphere). 1 mm3 = 1 microlitre; 1000 mm3 = 1 mL.
When a file ends (or you press Stop) all open bubbles are finished, counted and saved.

## How to read the numbers honestly
* Volume scales with d^3: a 10 % error in diameter is ~33 % in volume, and **one wrong large detection can
  be most of the total**. Always glance at the "Largest bubble = % of volume" line and at the biggest rows
  in the Bubbles sheet.
* Bubbles under ~8 px are near the noise floor (orange boxes, "low"). Use the checkbox to include/exclude.
* Ring-style bubbles (pale centre, thin dark rim) tend to be measured too small.
* Top-down cameras see bubbles come at the lens and only grow, which is hard to track; a SIDE view with a
  plain background, 60 fps, a fast shutter and an in-plane scale reference is much better.
* If the Pi cannot keep up (see "Processing fps"), frames are skipped; lower "Detect width".

## Files
    bubblemeter/engine.py   detection + tracking + measurement (no GUI, reusable)
    bubblemeter/logger.py   CSV + Excel writer
    bubblemeter/sources.py  USB / RTSP / file / Pi camera
    bubblemeter/app.py      Tkinter GUI          bubblemeter/cli.py   headless
Settings are remembered in `~/.bubblemeter/settings.json`.

## Start automatically on boot (optional)
Desktop autostart: copy the launcher -
    mkdir -p ~/.config/autostart && cp ~/.local/share/applications/bubblemeter.desktop ~/.config/autostart/

## Troubleshooting
* "Cannot open source": check `ls /dev/video*`; try index 1; for a Pi ribbon camera use `picamera`.
* Many false bubbles: mask static text/cables, lower sensitivity to `normal`, increase lighting stability.
* Few bubbles: use `sensitive`, raise Detect width, check focus and lighting.
* The camera must not move; automatic exposure changes can cause false detections - lock exposure if you can.
