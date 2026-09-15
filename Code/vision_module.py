"""
UCAS Vision Module
==================
Camera capture, pixel↔step calibration, and virtual boundary handling.

Design:
  - CameraThread runs OpenCV VideoCapture in the background so the GUI
    never blocks on frame reads. Latest frame is exposed as a numpy array.
  - Calibration is a 2-point similarity transform (translation + uniform
    scale + rotation) between pixel coordinates (u, v) and machine step
    coordinates (X, Y). Solved from two (pixel, step) pairs.
  - Boundary is a polygon in step-space. Method `contains_step_point`
    checks membership; `clamp_step_point_to_boundary` finds the nearest
    interior point when a move would exit.

Dependencies: opencv-python, numpy
    pip install opencv-python numpy

Import policy: if OpenCV is missing, the module still imports so the main
GUI can start; vision features become no-ops. Check VISION_AVAILABLE.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

try:
    import cv2
    import numpy as np
    VISION_AVAILABLE = True
    VISION_IMPORT_ERROR: Optional[str] = None
except Exception as exc:
    cv2 = None
    np = None
    VISION_AVAILABLE = False
    VISION_IMPORT_ERROR = str(exc)


# ============================================================
# CAMERA CAPTURE THREAD
# ============================================================

class CameraThread:
    """Background thread that continuously reads frames from a camera and
    holds the latest one. The GUI polls `get_latest_frame()` on its own
    timer — no blocking, no sync issues.

    Camera device index: 0 is usually the first USB webcam; 1 the second.
    On Windows you may need to try 0, 1, 2 until you find each camera.
    """

    def __init__(self, device_index: int = 0):
        self.device_index = device_index
        self.cap: Optional[cv2.VideoCapture] = None
        self._latest_frame = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last_error: Optional[str] = None
        self.frame_count = 0

    def start(self) -> bool:
        if not VISION_AVAILABLE:
            self.last_error = f"OpenCV not available: {VISION_IMPORT_ERROR}"
            return False
        if self._thread is not None and self._thread.is_alive():
            return True   # already running

        # Try multiple backends in order. On Windows the default (MSMF) often
        # opens the camera but returns black frames — DSHOW usually works.
        # On Linux/macOS the default backend is fine.
        import sys, platform
        if platform.system() == "Windows":
            backends_to_try = [
                ("CAP_DSHOW", cv2.CAP_DSHOW),
                ("CAP_MSMF",  cv2.CAP_MSMF),
                ("CAP_ANY",   cv2.CAP_ANY),
            ]
        else:
            backends_to_try = [("CAP_ANY", cv2.CAP_ANY)]

        opened = False
        for backend_name, backend_flag in backends_to_try:
            try:
                cap = cv2.VideoCapture(self.device_index, backend_flag)
                if not cap.isOpened():
                    cap.release()
                    continue
                # Verify we can actually READ a frame, not just open.
                # Some drivers open successfully but return black frames until
                # the resolution is negotiated. We test with a short retry.
                got_real_frame = False
                for _ in range(10):
                    ok, frame = cap.read()
                    if ok and frame is not None and frame.std() > 3:
                        got_real_frame = True
                        break
                    time.sleep(0.1)
                if not got_real_frame:
                    cap.release()
                    continue
                self.cap = cap
                opened = True
                print(f"Camera {self.device_index}: opened with {backend_name}")
                break
            except Exception as e:
                self.last_error = str(e)
                continue

        if not opened:
            self.last_error = (
                f"Camera {self.device_index}: no backend delivered valid frames. "
                f"Try a different camera index, or close other apps using the camera."
            )
            return False

        # Do NOT force a specific resolution — many webcams silently refuse
        # unsupported modes and then return black frames. Use whatever the
        # driver gives us as its default.

        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self) -> None:
        """Continuously grab the latest frame. Discarding older frames is
        intentional — we want the most recent state, not a queue of old frames."""
        while not self._stop_event.is_set():
            try:
                ok, frame = self.cap.read()
                if ok and frame is not None:
                    with self._lock:
                        self._latest_frame = frame
                        self.frame_count += 1
                else:
                    time.sleep(0.05)   # brief backoff on read failure
            except Exception as e:
                self.last_error = str(e)
                time.sleep(0.1)

    def get_latest_frame(self):
        """Return a copy of the latest frame (numpy BGR array), or None."""
        with self._lock:
            if self._latest_frame is None:
                return None
            return self._latest_frame.copy()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None
        self._thread = None


# ============================================================
# CALIBRATION: PIXEL <-> STEP TRANSFORM
# ============================================================

@dataclass
class Calibration:
    """Similarity transform mapping pixel (u,v) <-> step (X,Y).

    We store the transform as four numbers:
        step = a * pixel + b * perp(pixel) + t
    where a, b encode uniform scale + rotation, and t is translation.

    Equivalently, a 2x3 affine matrix. numpy handles this cleanly.
    """
    # Forward: pixel -> step. 2x3 matrix as flat list [m00, m01, m02, m10, m11, m12]
    # Meaning: step_x = m00*u + m01*v + m02
    #          step_y = m10*u + m11*v + m12
    matrix_px_to_step: Optional[list[float]] = None
    # Two anchor points used to compute the matrix, kept for re-editing.
    anchor_pixels: list[tuple[float, float]] = field(default_factory=list)
    anchor_steps: list[tuple[int, int]] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return self.matrix_px_to_step is not None

    @classmethod
    def from_two_points(cls,
                        px1: tuple[float, float], step1: tuple[int, int],
                        px2: tuple[float, float], step2: tuple[int, int]
                        ) -> "Calibration":
        """Fit a similarity transform (translation + uniform scale + rotation)
        from two pixel-step correspondences.

        This is enough when the camera is roughly perpendicular to the workspace
        and lens distortion is negligible. If your two calibration points end up
        at very similar pixel locations, the fit is ill-conditioned; pick points
        that span most of the workspace instead.
        """
        u1, v1 = px1
        u2, v2 = px2
        X1, Y1 = step1
        X2, Y2 = step2

        du, dv = u2 - u1, v2 - v1
        dX, dY = X2 - X1, Y2 - Y1
        pixel_dist_sq = du * du + dv * dv
        if pixel_dist_sq < 1.0:
            raise ValueError(
                "Calibration points are too close in the image. "
                "Pick points that are far apart."
            )
        # Solve step_delta = R @ pixel_delta where R is a scaled rotation.
        # For a 2D similarity: (dX, dY) = ((a, -b), (b, a)) @ (du, dv)
        # Solve:  a*du - b*dv = dX
        #         b*du + a*dv = dY
        a = (du * dX + dv * dY) / pixel_dist_sq
        b = (du * dY - dv * dX) / pixel_dist_sq
        # Then translation:  step = R @ pixel + t   =>   t = step1 - R @ pixel1
        tx = X1 - (a * u1 - b * v1)
        ty = Y1 - (b * u1 + a * v1)
        matrix = [a, -b, tx, b, a, ty]
        cal = cls(matrix_px_to_step=matrix,
                  anchor_pixels=[px1, px2],
                  anchor_steps=[step1, step2])
        return cal

    def pixel_to_step(self, u: float, v: float) -> tuple[int, int]:
        if not self.is_valid:
            raise RuntimeError("Calibration is not set.")
        m = self.matrix_px_to_step
        X = m[0] * u + m[1] * v + m[2]
        Y = m[3] * u + m[4] * v + m[5]
        return int(round(X)), int(round(Y))

    def step_to_pixel(self, X: float, Y: float) -> tuple[float, float]:
        """Inverse transform. Similarity transforms are trivially invertible."""
        if not self.is_valid:
            raise RuntimeError("Calibration is not set.")
        m = self.matrix_px_to_step
        # Forward is:  [X]   [a  -b] [u]   [tx]
        #              [Y] = [b   a] [v] + [ty]
        # So inverse:  [u]   1     [ a   b] [X - tx]
        #              [v] = --- * [-b   a] [Y - ty]
        #                    a²+b²
        a, mb = m[0], m[1]   # mb is -b, so b = -mb
        b = -mb
        det = a * a + b * b
        if det < 1e-9:
            raise RuntimeError("Calibration matrix is singular.")
        dx = X - m[2]
        dy = Y - m[5]
        u = (a * dx + b * dy) / det
        v = (-b * dx + a * dy) / det
        return u, v

    def to_dict(self) -> dict:
        return {
            "matrix_px_to_step": self.matrix_px_to_step,
            "anchor_pixels": self.anchor_pixels,
            "anchor_steps": self.anchor_steps,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Calibration":
        c = cls()
        c.matrix_px_to_step = d.get("matrix_px_to_step")
        c.anchor_pixels = [tuple(p) for p in d.get("anchor_pixels", [])]
        c.anchor_steps = [tuple(p) for p in d.get("anchor_steps", [])]
        return c


# ============================================================
# BOUNDARY POLYGON
# ============================================================

@dataclass
class Boundary:
    """Convex-or-concave polygon in step space. Also stores the pixel-space
    version for display. Optional home point inside the polygon."""

    vertices_step: list[tuple[int, int]] = field(default_factory=list)
    vertices_pixel: list[tuple[float, float]] = field(default_factory=list)
    home_step: Optional[tuple[int, int]] = None

    @property
    def is_valid(self) -> bool:
        return len(self.vertices_step) >= 3

    def contains_step_point(self, X: int, Y: int) -> bool:
        """Ray-casting point-in-polygon test. Works for arbitrary simple
        polygons (convex or concave), not for self-intersecting ones."""
        if not self.is_valid:
            return True   # no boundary set => nothing to enforce
        pts = self.vertices_step
        n = len(pts)
        inside = False
        j = n - 1
        for i in range(n):
            xi, yi = pts[i]
            xj, yj = pts[j]
            # Edge from (xj, yj) to (xi, yi) crosses horizontal ray to the right of (X, Y)?
            if ((yi > Y) != (yj > Y)):
                x_at_ray = (xj - xi) * (Y - yi) / (yj - yi + 1e-12) + xi
                if X < x_at_ray:
                    inside = not inside
            j = i
        return inside

    def clamp_step_point_to_boundary(self, X: int, Y: int) -> tuple[int, int]:
        """Return the nearest point on the boundary edge. Used when a move
        would exit the polygon and we want to stop *at* the wall rather than
        outside it.

        Note: this returns a point on the edge, which is technically ON the
        boundary rather than strictly inside. In practice that's fine for
        limiting motion. If you want a small safety margin, subtract a
        few steps in the direction from the edge toward the interior.
        """
        if not self.is_valid:
            return X, Y

        pts = self.vertices_step
        n = len(pts)
        best_dist_sq = float("inf")
        best_point = (X, Y)
        for i in range(n):
            ax, ay = pts[i]
            bx, by = pts[(i + 1) % n]
            # Foot of perpendicular from (X, Y) onto segment [(ax,ay),(bx,by)]
            dx, dy = bx - ax, by - ay
            seg_len_sq = dx * dx + dy * dy
            if seg_len_sq < 1e-9:
                # Zero-length edge, skip
                continue
            t = ((X - ax) * dx + (Y - ay) * dy) / seg_len_sq
            t = max(0.0, min(1.0, t))   # clamp to segment
            fx = ax + t * dx
            fy = ay + t * dy
            d_sq = (fx - X) ** 2 + (fy - Y) ** 2
            if d_sq < best_dist_sq:
                best_dist_sq = d_sq
                best_point = (int(round(fx)), int(round(fy)))
        return best_point

    def check_or_clamp_move(self, target_X: int, target_Y: int, clamp: bool
                            ) -> tuple[bool, int, int, str]:
        """Convenience for the motion pre-check:
          - Returns (ok, new_X, new_Y, note).
          - If target is inside: (True, X, Y, "").
          - If target is outside and clamp=True: (True, clamped_X, clamped_Y, "clamped").
          - If target is outside and clamp=False: (False, X, Y, "refused: outside boundary").
          - If no boundary is set: (True, X, Y, "").
        """
        if not self.is_valid:
            return True, target_X, target_Y, ""
        if self.contains_step_point(target_X, target_Y):
            return True, target_X, target_Y, ""
        if clamp:
            cx, cy = self.clamp_step_point_to_boundary(target_X, target_Y)
            return True, cx, cy, "clamped to boundary"
        return False, target_X, target_Y, "refused: outside boundary"

    def to_dict(self) -> dict:
        return {
            "vertices_step": [list(p) for p in self.vertices_step],
            "vertices_pixel": [list(p) for p in self.vertices_pixel],
            "home_step": list(self.home_step) if self.home_step is not None else None,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Boundary":
        b = cls()
        b.vertices_step = [tuple(p) for p in d.get("vertices_step", [])]
        b.vertices_pixel = [tuple(p) for p in d.get("vertices_pixel", [])]
        hs = d.get("home_step")
        b.home_step = tuple(hs) if hs is not None else None
        return b


# ============================================================
# FRAME OVERLAY HELPERS
# ============================================================

def draw_overlays_on_frame(frame,
                           boundary: Optional[Boundary],
                           calibration: Optional[Calibration],
                           head_step: Optional[tuple[int, int]] = None
                           ):
    """Draw the boundary polygon, home point, and current head position onto
    the frame. Returns the annotated frame (modifies in place).

    All positions are converted from step-space to pixel-space via the
    calibration transform. If calibration isn't set, we draw the raw pixel
    polygon (if we have one) but skip step-based overlays.
    """
    if not VISION_AVAILABLE or frame is None:
        return frame

    # ---- Boundary polygon ----
    if boundary is not None and boundary.is_valid:
        # Prefer pixel vertices if we have them; else project from step space
        pixel_pts = boundary.vertices_pixel
        if not pixel_pts and calibration is not None and calibration.is_valid:
            pixel_pts = []
            for (sx, sy) in boundary.vertices_step:
                try:
                    u, v = calibration.step_to_pixel(sx, sy)
                    pixel_pts.append((u, v))
                except Exception:
                    pass
        if pixel_pts:
            pts = np.array([[int(u), int(v)] for u, v in pixel_pts], dtype=np.int32)
            cv2.polylines(frame, [pts], isClosed=True, color=(0, 255, 0), thickness=2)
            # semi-transparent fill
            overlay = frame.copy()
            cv2.fillPoly(overlay, [pts], (0, 255, 0))
            cv2.addWeighted(overlay, 0.10, frame, 0.90, 0, frame)

    # ---- Home point ----
    if (boundary is not None and boundary.home_step is not None
            and calibration is not None and calibration.is_valid):
        try:
            u, v = calibration.step_to_pixel(*boundary.home_step)
            cv2.circle(frame, (int(u), int(v)), 8, (0, 200, 255), 2)
            cv2.putText(frame, "HOME", (int(u) + 12, int(v) + 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 200, 255), 1, cv2.LINE_AA)
        except Exception:
            pass

    # ---- Current head position ----
    if (head_step is not None
            and calibration is not None and calibration.is_valid):
        try:
            u, v = calibration.step_to_pixel(*head_step)
            # Colour by proximity to boundary
            inside = boundary.contains_step_point(*head_step) if (boundary and boundary.is_valid) else True
            colour = (0, 255, 0) if inside else (0, 0, 255)
            cv2.drawMarker(frame, (int(u), int(v)), colour,
                           markerType=cv2.MARKER_CROSS,
                           markerSize=20, thickness=2)
            cv2.circle(frame, (int(u), int(v)), 12, colour, 2)
        except Exception:
            pass

    # ---- Calibration anchor points ----
    if calibration is not None and calibration.is_valid:
        for i, (u, v) in enumerate(calibration.anchor_pixels):
            cv2.circle(frame, (int(u), int(v)), 6, (255, 255, 0), 1)
            cv2.putText(frame, f"C{i+1}", (int(u) + 8, int(v)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (255, 255, 0), 1, cv2.LINE_AA)

    return frame


def frame_to_tk_image(frame, max_width: int = 640):
    """Convert an OpenCV BGR frame to a (PhotoImage, PIL.Image) pair for Tk.

    IMPORTANT: the CALLER MUST KEEP A REFERENCE TO BOTH returned objects for
    as long as the image is displayed. Tk holds only a weak reference to the
    PhotoImage's pixel buffer, and Python will garbage-collect the underlying
    PIL Image the moment nothing points to it — the canvas then shows an
    empty/black rectangle. This is a well-known Tk gotcha.

    Returns (None, None) if PIL isn't available or the frame is empty.
    """
    if not VISION_AVAILABLE or frame is None:
        return None, None
    try:
        from PIL import Image, ImageTk
    except Exception:
        return None, None
    h, w = frame.shape[:2]
    if w > max_width:
        scale = max_width / w
        frame = cv2.resize(frame, (int(w * scale), int(h * scale)))
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    pil_img = Image.fromarray(rgb)
    photo = ImageTk.PhotoImage(image=pil_img)
    return photo, pil_img