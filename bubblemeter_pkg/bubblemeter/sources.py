"""Frame sources: USB/V4L2 camera index, RTSP/HTTP stream, video file, or Raspberry Pi CSI camera."""
import time

import cv2


class Source:
    """read() -> (ok, frame_bgr, t_seconds).  t is source time for files, wall-clock for live."""

    def __init__(self, spec, width=1280, height=720, realtime_file=False):
        self.spec = str(spec).strip()
        self.is_file = False
        self.fps = 25.0
        self._pi = None
        self._cap = None
        self._n = 0
        self._t0 = None
        self._realtime_file = realtime_file
        if self.spec.lower() in ('picamera', 'picamera2', 'pi'):
            from picamera2 import Picamera2          # only on Raspberry Pi OS
            self._pi = Picamera2()
            cfg = self._pi.create_video_configuration(main={'size': (int(width), int(height)),
                                                            'format': 'RGB888'})   # = BGR byte order
            self._pi.configure(cfg)
            self._pi.start()
            self.fps = 30.0
            return
        if self.spec.isdigit():
            self._cap = cv2.VideoCapture(int(self.spec), cv2.CAP_V4L2) if hasattr(cv2, 'CAP_V4L2') \
                else cv2.VideoCapture(int(self.spec))
            if not self._cap.isOpened():
                self._cap = cv2.VideoCapture(int(self.spec))
            self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(width))
            self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(height))
            self._cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        else:
            self._cap = cv2.VideoCapture(self.spec)
            self.is_file = not self.spec.lower().startswith(('rtsp://', 'http://', 'https://'))
        if self._cap is not None and not self._cap.isOpened():
            raise RuntimeError(f'Could not open source: {self.spec}')
        if self._cap is not None:
            f = self._cap.get(cv2.CAP_PROP_FPS)
            if f and f > 1:
                self.fps = float(f)

    def read(self):
        if self._pi is not None:
            frame = self._pi.capture_array()
            if frame.ndim == 3 and frame.shape[2] == 4:
                frame = frame[:, :, :3]
            if self._t0 is None:
                self._t0 = time.monotonic()
            return True, frame, time.monotonic() - self._t0
        ok, frame = self._cap.read()
        if not ok:
            return False, None, 0.0
        if self.is_file:
            t = self._n / self.fps
            if self._realtime_file:                 # play at source speed
                if self._t0 is None:
                    self._t0 = time.monotonic()
                wait = self._t0 + t - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
        else:
            if self._t0 is None:
                self._t0 = time.monotonic()
            t = time.monotonic() - self._t0
        self._n += 1
        return True, frame, t

    def release(self):
        try:
            if self._pi is not None:
                self._pi.stop()
                self._pi.close()
            if self._cap is not None:
                self._cap.release()
        except Exception:
            pass
