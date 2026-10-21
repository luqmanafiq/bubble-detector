"""
Live (frame-by-frame) bubble detector, tracker and diameter meter.

Designed for a STATIC camera. Everything is causal (uses only past frames), so it works on a
live feed.  Diameters are reported in NATIVE pixels of the frames you feed in; convert to mm
with your calibration (mm per pixel) elsewhere.

Pipeline per frame
  1. Background  : median of the last few stored frames (one stored every `bg_step` frames).
  2. Detection   : pixel darker than background AND darker than its surroundings; blobs are
                   filtered by size / shape / darkness.  Runs on a downscaled copy (proc_width).
  3. Measurement : on the native-resolution pixels around the blob: half-max outline ->
                   area -> equivalent-circle diameter.
  4. Tracking    : nearest neighbour with velocity prediction; short gaps allowed.
  5. Finalise    : when a track disappears, its diameter = median over its sharpest frames.
                   Slow jittery tracks (cables, cracks) are rejected.  Result -> `finished`.
"""
from collections import deque

import cv2
import numpy as np

REF_W = 960                  # detection width the thresholds below were tuned at
TO_N = 1280.0 / 960.0        # "native px of a 1280-wide clip" per reference px (tuning unit)

PROFILES = {
    # normal: clear, dark bubbles.   sensitive: faint / small / blurred bubbles (more false hits)
    'normal':    dict(d_t=18, d_s=14, min_peak=45, min_area=30, aspect=(0.45, 2.2)),
    'sensitive': dict(d_t=11, d_s=8,  min_peak=27, min_area=10, aspect=(0.3, 3.5)),
}

# Regions to ignore, as fractions of the frame (x0, y0, x1, y1).  These defaults cover the
# on-screen text / dials of the ROV footage; clear or replace them for your own camera.
DEFAULT_IGNORE = [
    (0.3125, 0.0, 0.698, 0.111), (0.3125, 0.778, 0.698, 1.0), (0.0, 0.0, 0.135, 0.185),
    (0.8125, 0.0, 1.0, 0.185), (0.927, 0.0, 1.0, 1.0), (0.667, 0.333, 1.0, 0.481),
    (0.8125, 0.704, 1.0, 1.0), (0.0, 0.741, 0.135, 1.0), (0.0, 0.0, 0.026, 1.0),
]


class Params:
    def __init__(self, profile='normal', proc_width=960, ignore=None, bg_step=5, bg_n=6,
                 min_bg=3, max_gap=3, hits=3, travel=12.0, gate=45.0, low_conf_px=8.0):
        self.profile = profile
        self.proc_width = proc_width
        self.ignore = list(DEFAULT_IGNORE if ignore is None else ignore)
        self.bg_step, self.bg_n, self.min_bg = bg_step, bg_n, min_bg
        self.max_gap, self.hits = max_gap, hits
        self.travel, self.gate = travel, gate          # in reference (960-wide) pixels
        self.low_conf_px = low_conf_px                  # in 1280-wide native pixels


class _Track:
    __slots__ = ('pts', 'v', 'last', 'confirmed', 'suspect', 'best_peak', 'best_roi', 't0', 't1')

    def __init__(self, i, x, y, meas, t=0.0):
        self.t0 = self.t1 = t
        self.pts = [(i, x, y, meas)]
        self.v = (0.0, 0.0)
        self.last = i
        self.confirmed = False
        self.suspect = False
        self.best_peak = -1.0
        self.best_roi = None


class BubbleEngine:
    def __init__(self, params=None):
        self.p = params or Params()
        self.W = self.H = None
        self.i = 0
        self.hist = deque(maxlen=self.p.bg_n)       # (small gray, native gray)
        self.bg_s = None
        self.active = []
        self.logged = 0                             # bubbles finalised so far
        self._zone_t0 = self._zone_t1 = None
        self._pending_ignore = None
        self._t = 0.0
        self.prof = PROFILES[self.p.profile]

    # ---------------------------------------------------------------- setup
    def _setup(self, frame):
        self.H, self.W = frame.shape[:2]
        self.pw = int(min(self.p.proc_width, self.W))
        self.ph = int(round(self.pw * self.H / self.W))
        self.s = self.pw / REF_W                    # detection scale vs. tuning scale
        self.sn = self.W / self.pw                  # native px per detection px
        self.k = self.W / 1280.0                    # native scale vs. tuning clips
        self.zone = np.zeros((self.ph, self.pw), np.uint8)
        self._build_mask(self.p.ignore)

    def _build_mask(self, rects):
        m = np.ones((self.ph, self.pw), np.uint8)
        for x0, y0, x1, y1 in rects:
            m[int(y0 * self.ph):int(np.ceil(y1 * self.ph)), int(x0 * self.pw):int(np.ceil(x1 * self.pw))] = 0
        self.mask = cv2.erode(m, np.ones((5, 5), np.uint8))

    def set_ignore(self, rects):
        """Thread-safe: applied at the next frame."""
        self._pending_ignore = list(rects)

    @property
    def low_conf_native_px(self):
        return self.p.low_conf_px * (self.W / 1280.0 if self.W else 1.0)

    # ---------------------------------------------------------------- detection
    def _detect(self, gs):
        s, pr = self.s, self.prof
        g = gs.astype(np.float32)
        d_t = cv2.GaussianBlur(self.bg_s - g, (0, 0), max(0.5, 1.0 * s))
        ds = cv2.resize(g, (max(self.pw // 4, 8), max(self.ph // 4, 8)), interpolation=cv2.INTER_AREA)
        loc = cv2.resize(cv2.GaussianBlur(ds, (0, 0), max(0.5, 3.0 * s)), (self.pw, self.ph),
                         interpolation=cv2.INTER_LINEAR)
        d_s = loc - cv2.GaussianBlur(g, (0, 0), max(0.5, 1.2 * s))
        cand = ((d_t > pr['d_t']) & (d_s > pr['d_s']) & (self.mask > 0)).astype(np.uint8)
        cand = cv2.morphologyEx(cand, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        cand = cv2.dilate(cand, np.ones((3, 3), np.uint8))
        n, lab, st, cen = cv2.connectedComponentsWithStats(cand, connectivity=8)
        amin, amax = pr['min_area'] * s * s, 4000 * s * s
        out = []
        for k in range(1, n):
            x, y, w, h, a = st[k]
            if not (amin <= a <= amax and pr['aspect'][0] <= w / max(h, 1) <= pr['aspect'][1]):
                continue
            sub = d_t[y:y + h, x:x + w][lab[y:y + h, x:x + w] == k]
            if sub.max() < pr['min_peak']:
                continue
            out.append([float(cen[k][0]), float(cen[k][1]), int(w), int(h), None])
        return out

    # ---------------------------------------------------------------- measurement
    def _measure(self, gn, cx, cy, w, h):
        """Half-max outline of the blob on NATIVE pixels -> equivalent-circle diameter."""
        sn = self.sn
        R = max(int(40 * self.k), 16)
        X, Y = int(cx * sn), int(cy * sn)
        x0, x1 = max(X - R, 0), min(X + R, self.W)
        y0, y1 = max(Y - R, 0), min(Y + R, self.H)
        if x1 - x0 < 8 or y1 - y0 < 8:
            return None
        cur = gn[y0:y1, x0:x1].astype(np.float32)
        bg = np.median(np.stack([hh[1][y0:y1, x0:x1] for hh in self.hist]), axis=0).astype(np.float32)
        d = cv2.GaussianBlur(bg - cur, (0, 0), 1.0)
        cyy, cxx = Y - y0, X - x0
        rr = int(max(w, h) * sn / 2 + 4)
        yy, xx = np.ogrid[:d.shape[0], :d.shape[1]]
        near = ((yy - cyy) ** 2 + (xx - cxx) ** 2) <= rr * rr
        if not near.any():
            return None
        peak = float(d[near].max())
        if peak < 20:
            return None
        thr = max(0.5 * peak, 10)
        n, lab, st, cen = cv2.connectedComponentsWithStats((d > thr).astype(np.uint8), connectivity=8)
        best, bd = None, 1e9
        for k in range(1, n):
            if st[k, cv2.CC_STAT_AREA] < 6:
                continue
            dist = np.hypot(cen[k][0] - cxx, cen[k][1] - cyy)
            if dist < bd:
                best, bd = k, dist
        if best is None or bd > rr:
            return None
        comp = (lab == best).astype(np.uint8)
        cnts, _ = cv2.findContours(comp, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        cnt = max(cnts, key=cv2.contourArea)
        filled = np.zeros_like(comp)
        cv2.drawContours(filled, [cnt], -1, 1, -1)           # fill holes (ring bubbles)
        area = float(filled.sum())
        d_eq = 2 * np.sqrt(area / np.pi)
        bx, by, bw, bh = cv2.boundingRect(cnt)
        touches = bx == 0 or by == 0 or bx + bw >= d.shape[1] or by + bh >= d.shape[0]
        if len(cnt) >= 5:
            _, (ma, mb), _ = cv2.fitEllipse(cnt)
            major, minor = max(ma, mb), min(ma, mb)
        else:
            major = minor = d_eq
        return dict(d=d_eq, major=major, minor=minor, peak=peak, touches=touches,
                    roi=cur, ctr=(cxx, cyy))

    # ---------------------------------------------------------------- ring bubbles
    @staticmethod
    def _rim_ok(g, cx, cy, r):
        th = np.linspace(0, 2 * np.pi, 72, endpoint=False)

        def ring(rad):
            xs = (cx + rad * np.cos(th)).round().astype(int)
            ys = (cy + rad * np.sin(th)).round().astype(int)
            m = (xs >= 0) & (ys >= 0) & (xs < g.shape[1]) & (ys < g.shape[0])
            return g[ys[m], xs[m]].astype(float), int(m.sum())

        e, ne = ring(r)
        if ne < 50:
            return False
        ref = np.median(np.concatenate([ring(r + 4)[0], ring(r + 6)[0]]))
        return np.mean(e < ref - 5) >= 0.55 and (ref - np.median(e)) >= 6

    def _ring_rescue(self, roi_f, ctr, d_px):
        """Small dark arc -> look for a full circular rim passing through it."""
        R = max(int(34 * self.k), 12)
        cx, cy = ctr
        x0, y0 = max(int(cx - R), 0), max(int(cy - R), 0)
        roi = roi_f[y0:int(cy + R), x0:int(cx + R)].astype(np.uint8)
        if roi.shape[0] < 20 or roi.shape[1] < 20:
            return None
        gb = cv2.GaussianBlur(roi, (0, 0), 1.5)
        cs = cv2.HoughCircles(gb, cv2.HOUGH_GRADIENT, dp=1, minDist=6, param1=60, param2=18,
                              minRadius=max(int(d_px / 2) + 2, 5), maxRadius=max(int(26 * self.k), 8))
        if cs is None:
            return None
        tol = 3.5 * max(self.k, 0.5)
        for ccx, ccy, r in cs[0]:
            if abs(np.hypot((cx - x0) - ccx, (cy - y0) - ccy) - r) <= tol and self._rim_ok(gb, ccx, ccy, r):
                return 2 * r
        return None

    # ---------------------------------------------------------------- tracking
    def _track(self, blobs, i):
        s = self.s
        gate = self.p.gate * s
        cand = []
        for t in self.active:
            gap = i - t.last
            px = t.pts[-1][1] + t.v[0] * gap
            py = t.pts[-1][2] + t.v[1] * gap
            for j, b in enumerate(blobs):
                dd = np.hypot(b[0] - px, b[1] - py)
                if dd < gate + 8 * s * gap:
                    cand.append((dd, id(t), j, t))
        cand.sort(key=lambda c: c[0])
        used_t, used_b = set(), set()
        for dd, tid, j, t in cand:
            if tid in used_t or j in used_b:
                continue
            used_t.add(tid)
            used_b.add(j)
            b = blobs[j]
            gap = i - t.last
            t.v = ((b[0] - t.pts[-1][1]) / gap, (b[1] - t.pts[-1][2]) / gap)
            t.pts.append((i, b[0], b[1], b[4]))
            t.last = i
            t.t1 = self._t
            m = b[4]
            if m is not None and not m['touches'] and m['peak'] > t.best_peak:
                t.best_peak, t.best_roi = m['peak'], (m['roi'], m['ctr'], m['d'])
            self._update_flags(t)
        for j, b in enumerate(blobs):
            if j in used_b:
                continue
            t = _Track(i, b[0], b[1], b[4], self._t)
            m = b[4]
            if m is not None and not m['touches']:
                t.best_peak, t.best_roi = m['peak'], (m['roi'], m['ctr'], m['d'])
            self.active.append(t)

    def _feat(self, t):
        """(n, path, displacement, straightness) in 1280-wide-clip pixel units."""
        u = TO_N / self.s
        p = np.array([(q[1] * u, q[2] * u) for q in t.pts])
        path = float(np.sum(np.hypot(np.diff(p[:, 0]), np.diff(p[:, 1])))) if len(p) > 1 else 0.0
        disp = float(np.hypot(*(p[-1] - p[0])))
        return len(p), path, disp, disp / max(path, 1e-6)

    def _update_flags(self, t):
        n, path, disp, st = self._feat(t)
        if not t.confirmed and n >= self.p.hits:
            dd = np.hypot(t.pts[-1][1] - t.pts[0][1], t.pts[-1][2] - t.pts[0][2])
            if dd >= self.p.travel * self.s:
                t.confirmed = True
        t.suspect = n >= 15 and st < 0.45 and path / max(n - 1, 1) < 10

    def _is_jitter(self, t):
        n, path, disp, st = self._feat(t)
        slow = path / max(n - 1, 1) < 10
        return slow and ((n >= 15 and st < 0.45) or (n >= 8 and disp < 18) or (n >= 25 and disp < 60))

    def _summarize(self, t):
        ms = [(q[0], q[3]) for q in t.pts if q[3] is not None and not q[3]['touches']]
        if not ms:
            return None
        pk = max(m['peak'] for _, m in ms)
        sel = [m for _, m in ms if m['peak'] >= 0.8 * pk]
        d = float(np.median([m['d'] for m in sel]))
        return dict(d=d, major=float(np.median([m['major'] for m in sel])),
                    minor=float(np.median([m['minor'] for m in sel])))

    def _finalise(self, t, t_now):
        """Return a record dict, or None if the track is not a bubble."""
        n, path, disp, st = self._feat(t)
        if n < self.p.hits or not t.confirmed:
            return None
        if self._is_jitter(t):
            for q in t.pts:
                cv2.circle(self.zone, (int(q[1]), int(q[2])), int(30 * self.s), 1, -1)
            self._zone_mark(t)
            return None
        if self._zone_active():
            pts = [(int(q[1]), int(q[2])) for q in t.pts]
            near = np.mean([self.zone[min(max(y, 0), self.ph - 1), min(max(x, 0), self.pw - 1)] > 0
                            for x, y in pts])
            fast = path / max(n - 1, 1) >= 8 and st >= 0.9 and path >= 60
            if near >= 0.5 and not fast:
                return None
        sm = self._summarize(t)
        if sm is None:
            return None
        method = 'dark blob'
        if sm['d'] < 16 * self.k and t.best_roi is not None:
            r = self._ring_rescue(t.best_roi[0], t.best_roi[1], sm['d'])
            if r:
                sm['d'], method = r, 'ring (rim fit)'
        return dict(d_px=sm['d'], major_px=sm['major'], minor_px=sm['minor'], method=method,
                    low_conf=sm['d'] < self.low_conf_native_px)

    def _zone_mark(self, t):
        a, b = t.t0, t.t1
        self._zone_t0 = a if self._zone_t0 is None else min(self._zone_t0, a)
        self._zone_t1 = b if self._zone_t1 is None else max(self._zone_t1, b)

    def _zone_active(self):
        # a flickering cable persists for seconds; a bubble burst does not
        return self._zone_t0 is not None and (self._zone_t1 - self._zone_t0) >= 4.0

    # ---------------------------------------------------------------- public API
    def process(self, frame, t):
        """Feed one BGR frame (t = seconds since start).
        Returns (boxes, finished): boxes = [(x, y, d_px, low_conf)] in native px for the
        bubbles tracked in this frame; finished = list of bubble records closed this frame."""
        if self.W is None:
            self._setup(frame)
        self._t = t
        if self._pending_ignore is not None:
            self._build_mask(self._pending_ignore)
            self._pending_ignore = None
        i = self.i
        self.i += 1
        gn = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gs = cv2.resize(gn, (self.pw, self.ph), interpolation=cv2.INTER_AREA)
        if self.bg_s is not None:
            blobs = self._detect(gs)
            for b in blobs:
                b[4] = self._measure(gn, b[0], b[1], b[2], b[3])
            self._track(blobs, i)
        finished = self._close(i, t, force=False)
        if i % self.p.bg_step == 0:
            self.hist.append((gs, gn))
            if len(self.hist) >= self.p.min_bg:
                self.bg_s = np.median(np.stack([h[0] for h in self.hist]), axis=0).astype(np.float32)
        return self._boxes(i), finished

    def _close(self, i, t_now, force):
        finished, keep = [], []
        for tr in self.active:
            if force or i - tr.last > self.p.max_gap:
                rec = self._finalise(tr, t_now)
                if rec is not None:
                    rec['t_first'], rec['t_last'] = tr.t0, tr.t1
                    self.logged += 1
                    rec['id'] = self.logged
                    finished.append(rec)
            else:
                keep.append(tr)
        self.active = keep
        return finished

    def _boxes(self, i):
        out = []
        for tr in self.active:
            if tr.last != i or not tr.confirmed or tr.suspect:
                continue
            ms = [q[3] for q in tr.pts if q[3] is not None and not q[3]['touches']]
            if not ms:
                continue
            pk = max(m['peak'] for m in ms)
            d = float(np.median([m['d'] for m in ms if m['peak'] >= 0.8 * pk]))
            out.append((tr.pts[-1][1] * self.sn, tr.pts[-1][2] * self.sn, d, d < self.low_conf_native_px))
        return out

    def flush(self, t_now=0.0):
        """Call when the source ends: close all open tracks and return their records."""
        return self._close(self.i, t_now, force=True)

    @property
    def n_active(self):
        return sum(1 for t in self.active if t.confirmed and not t.suspect)
