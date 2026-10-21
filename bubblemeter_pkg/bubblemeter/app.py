"""BubbleMeter GUI (Tkinter - light enough for a Raspberry Pi)."""
import argparse
import json
import math
import os
import platform
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path

import cv2
import numpy as np
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageTk

from .engine import DEFAULT_IGNORE, BubbleEngine, Params
from .logger import SessionLogger, sphere_volume_mm3
from .sources import Source

CONFIG_PATH = Path.home() / '.bubblemeter' / 'settings.json'
IS_ARM = platform.machine().lower().startswith(('arm', 'aarch'))
DEFAULTS = dict(source='0', mm_per_px=0.03, profile='normal', proc_width=640 if IS_ARM else 960,
                cam_width=1280, cam_height=720, include_low=True, realtime_file=True,
                out_dir='~/BubbleMeter/sessions', ignore=[list(z) for z in DEFAULT_IGNORE])
DISP_W = 800


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        cfg.update(json.loads(CONFIG_PATH.read_text()))
    except Exception:
        pass
    return cfg


def save_cfg(cfg):
    try:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass


# ====================================================================== worker thread
class Worker(threading.Thread):
    def __init__(self, cfg, ignore):
        super().__init__(daemon=True)
        self.cfg = dict(cfg)
        self.ignore = [tuple(r) for r in ignore]
        self.stop_evt = threading.Event()
        self.latest = None            # (rgb ndarray, (native W, native H))
        self.frame_size = None
        self.logger = None
        self.engine = None
        self.recent = deque(maxlen=14)
        self.n_active = 0
        self.fps = 0.0
        self.elapsed = 0.0
        self.error = None
        self.finished = False
        self.message = 'starting...'
        self.mm = float(cfg['mm_per_px'])

    def set_mm(self, v):
        self.mm = float(v)
        if self.logger:
            self.logger.set_mm_per_px(v)

    def set_ignore(self, rects):
        self.ignore = [tuple(r) for r in rects]
        if self.engine:
            self.engine.set_ignore(self.ignore)

    def run(self):
        c = self.cfg
        try:
            src = Source(c['source'], c['cam_width'], c['cam_height'],
                         realtime_file=bool(c['realtime_file']))
        except Exception as e:
            self.error = f'Cannot open source: {e}'
            self.finished = True
            return
        self.engine = BubbleEngine(Params(profile=c['profile'], proc_width=int(c['proc_width']),
                                          ignore=self.ignore))
        self.logger = SessionLogger(c['out_dir'], self.mm, label=str(c['source']))
        self.message = 'running'
        t_prev = time.time()
        last_save = time.time()
        t = 0.0
        try:
            while not self.stop_evt.is_set():
                ok, frame, t = src.read()
                if not ok:
                    break
                H, W = frame.shape[:2]
                self.frame_size = (W, H)
                boxes, done = self.engine.process(frame, t)
                for rec in done:
                    self.logger.add(rec)
                    d_mm = rec['d_px'] * self.mm
                    self.recent.appendleft(
                        f"#{rec['id']:<4} t={rec['t_last']:6.1f}s  d={d_mm:.3f} mm  "
                        f"V={sphere_volume_mm3(d_mm):.4f} mm3" + ('  (low)' if rec['low_conf'] else ''))
                self.logger.set_duration(t)
                self.elapsed = t
                self.n_active = self.engine.n_active
                # ---- preview (drawn at display size, cheap)
                sc = DISP_W / W
                small = cv2.resize(frame, (DISP_W, int(round(H * sc))), interpolation=cv2.INTER_AREA)
                for x0, y0, x1, y1 in self.ignore:
                    cv2.rectangle(small, (int(x0 * small.shape[1]), int(y0 * small.shape[0])),
                                  (int(x1 * small.shape[1]), int(y1 * small.shape[0])), (60, 60, 200), 1)
                for x, y, d, low in boxes:
                    half = max(d / 2 * sc + 3, 6)
                    col = (0, 165, 255) if low else (0, 255, 0)
                    cx, cy = x * sc, y * sc
                    cv2.rectangle(small, (int(cx - half), int(cy - half)), (int(cx + half), int(cy + half)), col, 2)
                    cv2.putText(small, f'{d * self.mm:.2f}mm', (int(cx - half), max(int(cy - half) - 4, 10)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
                self.latest = (cv2.cvtColor(small, cv2.COLOR_BGR2RGB), (W, H))
                now = time.time()
                self.fps = 0.9 * self.fps + 0.1 / max(now - t_prev, 1e-6)
                t_prev = now
                if self.logger.dirty and now - last_save > 20:       # autosave
                    self.logger.save_xlsx()
                    last_save = now
        except Exception as e:                                           # keep the data we have
            self.error = f'Processing error: {e}'
        for rec in self.engine.flush(t):                                 # finish counting
            self.logger.add(rec)
        src.release()
        try:
            self.logger.save_xlsx(t)
        except Exception as e:
            self.error = f'Excel save failed: {e}'
        self.message = 'finished'
        self.finished = True


# ====================================================================== GUI
class App:
    def __init__(self, root, args):
        self.root = root
        self.cfg = load_cfg()
        if args.source:
            self.cfg['source'] = args.source
        if args.profile:
            self.cfg['profile'] = args.profile
        self.worker = None
        self.ignore = [tuple(r) for r in self.cfg.get('ignore', [])]
        self.photo = None
        self.cal_pts = []
        self.cal_mode = False
        self.drag = None
        self.disp_size = (DISP_W, int(DISP_W * 9 / 16))
        self.args = args
        root.title('BubbleMeter')
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self._build()
        self.root.after(100, self.poll)
        if args.autostart:
            self.root.after(500, self.start)

    # ------------------------------------------------------------ layout
    def _build(self):
        r = self.root
        left = ttk.Frame(r)
        left.grid(row=0, column=0, padx=8, pady=8, sticky='n')
        self.canvas = tk.Canvas(left, width=DISP_W, height=int(DISP_W * 9 / 16), bg='black', highlightthickness=0)
        self.canvas.pack()
        self.canvas.bind('<ButtonPress-1>', self.on_press)
        self.canvas.bind('<B1-Motion>', self.on_motion)
        self.canvas.bind('<ButtonRelease-1>', self.on_release)
        self.status = tk.StringVar(value='Ready. Choose a source and press Start.')
        ttk.Label(left, textvariable=self.status, wraplength=DISP_W).pack(anchor='w', pady=(4, 0))
        ttk.Label(left, text='Tip: drag on the video to mask a region (text, dials, a cable). '
                             'Green box = measured bubble, orange = low confidence (<8 px).',
                  wraplength=DISP_W, foreground='#555').pack(anchor='w')
        self.listbox = tk.Listbox(left, height=7, width=100, font=('DejaVu Sans Mono', 9))
        self.listbox.pack(anchor='w', pady=(6, 0))

        right = ttk.Frame(r)
        right.grid(row=0, column=1, padx=8, pady=8, sticky='n')
        # ---- totals
        box = ttk.LabelFrame(right, text='Live totals')
        box.pack(fill='x')
        self.v = {k: tk.StringVar(value='-') for k in
                  ('count', 'active', 'ml', 'mm3', 'mean', 'maxd', 'share', 'rate', 'elapsed', 'fps')}
        big = ('DejaVu Sans', 18, 'bold')
        ttk.Label(box, text='Total volume').grid(row=0, column=0, sticky='w', padx=6)
        tk.Label(box, textvariable=self.v['ml'], font=big, fg='#0a5').grid(row=0, column=1, sticky='e', padx=6)
        rows = [('Bubbles logged', 'count'), ('Active now', 'active'), ('Volume (mm3)', 'mm3'),
                ('Mean diameter (mm)', 'mean'), ('Largest (mm)', 'maxd'), ('Largest bubble = % of volume', 'share'),
                ('Gas rate (mL/min)', 'rate'),
                ('Elapsed (s)', 'elapsed'), ('Processing fps', 'fps')]
        for n, (lab, key) in enumerate(rows, start=1):
            ttk.Label(box, text=lab).grid(row=n, column=0, sticky='w', padx=6)
            ttk.Label(box, textvariable=self.v[key]).grid(row=n, column=1, sticky='e', padx=6)
        # ---- settings
        sb = ttk.LabelFrame(right, text='Settings')
        sb.pack(fill='x', pady=8)
        self.var_source = tk.StringVar(value=str(self.cfg['source']))
        self.var_mm = tk.StringVar(value=str(self.cfg['mm_per_px']))
        self.var_profile = tk.StringVar(value=self.cfg['profile'])
        self.var_pw = tk.StringVar(value=str(self.cfg['proc_width']))
        self.var_low = tk.BooleanVar(value=bool(self.cfg['include_low']))
        self.var_rt = tk.BooleanVar(value=bool(self.cfg['realtime_file']))
        ttk.Label(sb, text='Source').grid(row=0, column=0, sticky='w', padx=4)
        ttk.Entry(sb, textvariable=self.var_source, width=26).grid(row=0, column=1, padx=4)
        ttk.Button(sb, text='File...', width=6, command=self.browse).grid(row=0, column=2)
        ttk.Label(sb, text='mm per pixel').grid(row=1, column=0, sticky='w', padx=4)
        e = ttk.Entry(sb, textvariable=self.var_mm, width=10)
        e.grid(row=1, column=1, sticky='w', padx=4)
        e.bind('<FocusOut>', lambda ev: self.apply_mm())
        e.bind('<Return>', lambda ev: self.apply_mm())
        ttk.Button(sb, text='Calibrate', width=9, command=self.calibrate).grid(row=1, column=2)
        ttk.Label(sb, text='Sensitivity').grid(row=2, column=0, sticky='w', padx=4)
        ttk.Combobox(sb, textvariable=self.var_profile, values=['normal', 'sensitive'], width=10,
                     state='readonly').grid(row=2, column=1, sticky='w', padx=4)
        ttk.Label(sb, text='Detect width').grid(row=3, column=0, sticky='w', padx=4)
        ttk.Combobox(sb, textvariable=self.var_pw, values=['480', '640', '800', '960'], width=10,
                     state='readonly').grid(row=3, column=1, sticky='w', padx=4)
        ttk.Checkbutton(sb, text='Include low-confidence in totals', variable=self.var_low).grid(
            row=4, column=0, columnspan=3, sticky='w', padx=4)
        ttk.Checkbutton(sb, text='Play files at real speed', variable=self.var_rt).grid(
            row=5, column=0, columnspan=3, sticky='w', padx=4)
        # ---- buttons
        bb = ttk.Frame(right)
        bb.pack(fill='x')
        self.btn_start = ttk.Button(bb, text='Start', command=self.start)
        self.btn_start.grid(row=0, column=0, padx=2, pady=2, sticky='ew')
        self.btn_stop = ttk.Button(bb, text='Stop && Save', command=self.stop, state='disabled')
        self.btn_stop.grid(row=0, column=1, padx=2, pady=2, sticky='ew')
        ttk.Button(bb, text='Save Excel now', command=self.save_now).grid(row=1, column=0, padx=2, pady=2, sticky='ew')
        ttk.Button(bb, text='Open results folder', command=self.open_folder).grid(row=1, column=1, padx=2, pady=2, sticky='ew')
        ttk.Button(bb, text='Clear masks', command=self.clear_zones).grid(row=2, column=0, padx=2, pady=2, sticky='ew')
        ttk.Button(bb, text='ROV text preset', command=self.preset_zones).grid(row=2, column=1, padx=2, pady=2, sticky='ew')
        ttk.Label(right, text='Volume = sum of pi/6 * d^3 per bubble (spheres).\n'
                              'Error in volume is ~3x the error in diameter.',
                  foreground='#555').pack(anchor='w', pady=(8, 0))

    # ------------------------------------------------------------ actions
    def browse(self):
        p = filedialog.askopenfilename(title='Choose a video file',
                                       filetypes=[('Video', '*.mp4 *.avi *.mov *.mkv'), ('All', '*.*')])
        if p:
            self.var_source.set(p)

    def apply_mm(self):
        try:
            v = float(self.var_mm.get())
            if v <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror('mm per pixel', 'Enter a positive number, e.g. 0.03')
            return None
        self.cfg['mm_per_px'] = v
        if self.worker:
            self.worker.set_mm(v)
        save_cfg(self.cfg)
        return v

    def collect(self):
        mm = self.apply_mm()
        if mm is None:
            return False
        self.cfg.update(source=self.var_source.get().strip(), profile=self.var_profile.get(),
                        proc_width=int(self.var_pw.get()), include_low=self.var_low.get(),
                        realtime_file=self.var_rt.get(), ignore=[list(z) for z in self.ignore])
        save_cfg(self.cfg)
        return True

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        if not self.collect():
            return
        self.worker = Worker(self.cfg, self.ignore)
        self.worker.start()
        self.btn_start.state(['disabled'])
        self.btn_stop.state(['!disabled'])
        self.listbox.delete(0, 'end')
        self.status.set('Running... (the first ~0.5 s builds the background)')

    def stop(self):
        if self.worker and self.worker.is_alive():
            self.worker.stop_evt.set()
            self.status.set('Stopping and saving...')

    def save_now(self):
        if self.worker and self.worker.logger:
            try:
                p = self.worker.logger.save_xlsx(self.worker.elapsed)
                self.status.set(f'Saved {p}')
            except Exception as e:
                messagebox.showerror('Excel', str(e))
        else:
            messagebox.showinfo('Excel', 'Nothing to save yet - press Start first.')

    def open_folder(self):
        d = self.worker.logger.dir if (self.worker and self.worker.logger) else Path(self.cfg['out_dir']).expanduser()
        d.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform.startswith('linux'):
                subprocess.Popen(['xdg-open', str(d)])
            elif sys.platform == 'darwin':
                subprocess.Popen(['open', str(d)])
            else:
                os.startfile(str(d))      # noqa
        except Exception:
            messagebox.showinfo('Results folder', str(d))

    def _push_zones(self):
        self.cfg['ignore'] = [list(z) for z in self.ignore]
        save_cfg(self.cfg)
        if self.worker:
            self.worker.set_ignore(self.ignore)

    def clear_zones(self):
        self.ignore = []
        self._push_zones()

    def preset_zones(self):
        self.ignore = [tuple(z) for z in DEFAULT_IGNORE]
        self._push_zones()

    # ------------------------------------------------------------ calibration + mask drawing
    def calibrate(self):
        if not (self.worker and self.worker.frame_size):
            messagebox.showinfo('Calibrate', 'Start the camera first, then click Calibrate.')
            return
        self.cal_mode = True
        self.cal_pts = []
        self.canvas.delete('cal')
        self.status.set('CALIBRATE: click two points on the video that are a known distance apart.')

    def _rel(self, ev):
        w, h = self.disp_size
        return min(max(ev.x / w, 0), 1), min(max(ev.y / h, 0), 1)

    def on_press(self, ev):
        if self.cal_mode:
            self.cal_pts.append(self._rel(ev))
            w, h = self.disp_size
            x, y = ev.x, ev.y
            self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, outline='yellow', width=2, tags='cal')
            if len(self.cal_pts) == 2:
                (x0, y0), (x1, y1) = self.cal_pts
                self.canvas.create_line(x0 * w, y0 * h, x1 * w, y1 * h, fill='yellow', width=2, tags='cal')
                self.cal_mode = False
                W, H = self.worker.frame_size
                px = math.hypot((x1 - x0) * W, (y1 - y0) * H)
                if px < 5:
                    self.status.set('Points too close - calibration cancelled.')
                    return
                mm = simpledialog.askfloat('Calibrate', f'The two points are {px:.1f} px apart.\n'
                                           'Real distance between them (mm):', parent=self.root, minvalue=0.001)
                self.canvas.delete('cal')
                if mm:
                    self.var_mm.set(f'{mm / px:.5f}')
                    self.apply_mm()
                    self.status.set(f'Calibrated: {mm / px:.5f} mm per pixel')
                else:
                    self.status.set('Calibration cancelled.')
            return
        self.drag = self._rel(ev)

    def on_motion(self, ev):
        if self.drag and not self.cal_mode:
            w, h = self.disp_size
            x0, y0 = self.drag
            self.canvas.delete('drag')
            self.canvas.create_rectangle(x0 * w, y0 * h, ev.x, ev.y, outline='red', dash=(4, 2), tags='drag')

    def on_release(self, ev):
        if self.drag and not self.cal_mode:
            x0, y0 = self.drag
            x1, y1 = self._rel(ev)
            self.drag = None
            self.canvas.delete('drag')
            if abs(x1 - x0) > 0.01 and abs(y1 - y0) > 0.01:
                self.ignore.append((min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))
                self._push_zones()

    # ------------------------------------------------------------ periodic refresh
    def poll(self):
        w = self.worker
        if w is not None:
            if w.latest is not None:
                rgb, _ = w.latest
                h, wd = rgb.shape[:2]
                self.disp_size = (wd, h)
                self.photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                self.canvas.config(width=wd, height=h)
                self.canvas.delete('frame')
                self.canvas.create_image(0, 0, image=self.photo, anchor='nw', tags='frame')
                self.canvas.tag_lower('frame')
            self.refresh_stats()
            if w.finished:
                self.btn_start.state(['!disabled'])
                self.btn_stop.state(['disabled'])
                if w.error:
                    self.status.set(w.error)
                elif w.logger:
                    s = w.logger.stats(self.var_low.get(), w.elapsed)
                    self.status.set(f"FINISHED - final count {s['count']} bubbles, total volume "
                                    f"{s['total_ml']:.5f} mL. Excel: {w.logger.xlsx_path}")
                self.worker = None
                if self.args.quit_when_done:
                    self.root.after(1500, self.on_close)
                    return
        self.root.after(33, self.poll)

    def refresh_stats(self):
        w = self.worker
        if not w or not w.logger:
            return
        s = w.logger.stats(self.var_low.get(), w.elapsed)
        self.v['count'].set(str(s['count']))
        self.v['active'].set(str(w.n_active))
        self.v['ml'].set(f"{s['total_ml']:.5f} mL")
        self.v['mm3'].set(f"{s['total_mm3']:.3f}")
        self.v['mean'].set(f"{s['mean_mm']:.3f}")
        self.v['maxd'].set(f"{s['max_mm']:.3f}")
        self.v['share'].set(f"{100 * s['top_share']:.0f}%")
        self.v['rate'].set(f"{s['ml_per_min']:.4f}")
        self.v['elapsed'].set(f'{w.elapsed:.1f}')
        self.v['fps'].set(f'{w.fps:.1f}')
        if self.listbox.size() == 0 or list(w.recent)[:1] != [self.listbox.get(0)]:
            self.listbox.delete(0, 'end')
            for line in w.recent:
                self.listbox.insert('end', line)
        if w.error and not w.finished:
            self.status.set(w.error)

    def on_close(self):
        w = self.worker
        if w and w.is_alive():
            w.stop_evt.set()
            w.join(timeout=8)
        save_cfg(self.cfg)
        self.root.destroy()


def main(argv=None):
    ap = argparse.ArgumentParser(description='BubbleMeter GUI')
    ap.add_argument('--source')
    ap.add_argument('--profile', choices=['normal', 'sensitive'])
    ap.add_argument('--autostart', action='store_true')
    ap.add_argument('--quit-when-done', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--screenshot', help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    root = tk.Tk()
    app = App(root, args)
    if args.screenshot:
        def shot():
            try:
                from PIL import ImageGrab
                ImageGrab.grab(xdisplay=os.environ.get('DISPLAY')).save(args.screenshot)
            except Exception as e:
                print('screenshot failed:', e)
        root.after(int(float(os.environ.get('SHOT_AFTER_MS', '9000'))), shot)
    root.mainloop()


if __name__ == '__main__':
    main()
