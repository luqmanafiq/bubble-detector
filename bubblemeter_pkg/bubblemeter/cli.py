"""Headless runner (no screen needed):  python -m bubblemeter.cli SOURCE [options]"""
import argparse
import time
from pathlib import Path

import cv2

from .engine import BubbleEngine, Params
from .logger import SessionLogger
from .sources import Source


def main(argv=None):
    ap = argparse.ArgumentParser(description='BubbleMeter headless')
    ap.add_argument('source', help='camera index (0), RTSP/HTTP URL, video file, or "picamera"')
    ap.add_argument('--mm-per-px', type=float, default=0.03)
    ap.add_argument('--profile', choices=['normal', 'sensitive'], default='normal')
    ap.add_argument('--proc-width', type=int, default=960, help='detection width (640 is faster on a Pi)')
    ap.add_argument('--out', default='~/BubbleMeter/sessions')
    ap.add_argument('--width', type=int, default=1280)
    ap.add_argument('--height', type=int, default=720)
    ap.add_argument('--realtime', action='store_true', help='play files at their own speed')
    ap.add_argument('--max-seconds', type=float, default=0, help='stop after N seconds (0 = until source ends)')
    ap.add_argument('--no-ignore', action='store_true', help='do not mask any region')
    a = ap.parse_args(argv)

    src = Source(a.source, a.width, a.height, realtime_file=a.realtime)
    eng = BubbleEngine(Params(profile=a.profile, proc_width=a.proc_width,
                              ignore=[] if a.no_ignore else None))
    log = SessionLogger(a.out, a.mm_per_px, label=a.source)
    t_wall = time.time()
    n = 0
    t = 0.0
    try:
        while True:
            ok, frame, t = src.read()
            if not ok:
                break
            _, done = eng.process(frame, t)
            for rec in done:
                log.add(rec)
            n += 1
            log.set_duration(t)
            if n % 200 == 0:
                s = log.stats()
                print(f't={t:6.1f}s  bubbles={s["count"]}  volume={s["total_ml"]*1000:.3f} mm3', flush=True)
                log.save_xlsx()
            if a.max_seconds and t >= a.max_seconds:
                break
    except KeyboardInterrupt:
        pass
    for rec in eng.flush(t):
        log.add(rec)
    src.release()
    log.save_xlsx(t)
    log.close()
    s = log.stats()
    el = time.time() - t_wall
    print(f'FINAL: {s["count"]} bubbles | total volume {s["total_mm3"]:.3f} mm3 ({s["total_ml"]:.5f} mL) | '
          f'mean d {s["mean_mm"]:.3f} mm | {n} frames in {el:.1f}s = {n/max(el,1e-6):.1f} fps')
    print('Saved:', log.xlsx_path)


if __name__ == '__main__':
    main()
