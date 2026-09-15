"""
UCAS Control Program V8
=======================
Changes from V7:
  1. Camera integration via `vision_module`. Supports up to two cameras
     (overhead and head-mounted); the overhead camera drives calibration
     and the virtual boundary.
  2. Pixel<->step calibration from two manually-jogged reference points.
     No printed markers required.
  3. Virtual boundary polygon for X/Y motion, drawn by clicking points on
     the camera feed. Enforced the same way as Z/P limits:
       - Manual jog: clamp target to nearest polygon edge
       - Auto mode: refuse move that would exit polygon
  4. "Go home" jogs to a user-selected point inside the polygon.
  5. New "Camera" tab in the main window shows live feed with overlays
     (boundary, head position, calibration points).

Depends on: pyserial, mixer.py, webcolors (optional), opencv-python (optional),
            Pillow (optional).
If OpenCV or Pillow is missing, the app still runs in V7-style mode without
camera features.
"""

from __future__ import annotations

import json
import os
import re
import time
import tkinter as tk
from collections import defaultdict
from tkinter import ttk, messagebox, filedialog

import serial

# ------------------------------------------------------------
# Vision module (guarded)
# ------------------------------------------------------------
try:
    from vision_module import (
        VISION_AVAILABLE, VISION_IMPORT_ERROR,
        CameraThread, Calibration, Boundary,
        draw_overlays_on_frame, frame_to_tk_image,
    )
except Exception as _vision_exc:
    VISION_AVAILABLE = False
    VISION_IMPORT_ERROR = str(_vision_exc)
    CameraThread = None
    Calibration = None
    Boundary = None
    draw_overlays_on_frame = None
    frame_to_tk_image = None

# ------------------------------------------------------------
# Optional webcolors import
# ------------------------------------------------------------
try:
    import webcolors
    WEBCOLORS_AVAILABLE = True
except Exception as exc:
    webcolors = None
    WEBCOLORS_AVAILABLE = False
    WEBCOLORS_IMPORT_ERROR = str(exc)


# ============================================================
# UCAS CONNECTION SETTINGS
# ============================================================

PORT = "COM3"
BAUD = 115200
DELAY = 0.3
COMMAND_TIMEOUT = 120

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ucas_config.json",
)

# ------------------------------------------------------------
# Mixer import (guarded)
# ------------------------------------------------------------
try:
    from mixer import BASE_COLORS, suggest_recipes
    MIXER_AVAILABLE = True
    MIXER_IMPORT_ERROR = None
except Exception as exc:
    BASE_COLORS = {}
    suggest_recipes = None
    MIXER_AVAILABLE = False
    MIXER_IMPORT_ERROR = str(exc)


# ============================================================
# HELPERS
# ============================================================

HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")


def is_valid_hex(s: str) -> bool:
    return bool(HEX_RE.match(s.strip()))


def normalize_hex(s: str) -> str:
    return f"#{s.strip().lstrip('#').lower()}"


def resolve_color_input(text: str) -> tuple[str | None, str | None]:
    """Accept hex ("#ff8b38") OR a CSS3 color name ("orange", "papayawhip").
    Returns (normalized_hex, None) on success, (None, error_message) on failure."""
    text = text.strip()
    if not text:
        return None, "Please enter a color."

    # Try hex first
    if is_valid_hex(text):
        return normalize_hex(text), None

    # Try color name via webcolors
    if WEBCOLORS_AVAILABLE:
        try:
            hex_str = webcolors.name_to_hex(text.lower(), spec="css3")
            return hex_str.lower(), None
        except ValueError:
            return None, (
                f"'{text}' is not a valid hex code or CSS color name.\n"
                f"Examples of valid names: red, orange, cornflowerblue,\n"
                f"papayawhip, mediumseagreen, tomato, dodgerblue."
            )
    else:
        return None, (
            f"'{text}' isn't a valid hex code, and color names require the\n"
            f"`webcolors` library which is not installed.\n\n"
            f"Install with:  pip install webcolors\n"
            f"Or enter a hex code like #FF8B38 instead."
        )


def default_config() -> dict:
    """Fresh config. USER MUST CALIBRATE before use."""
    color_names = list(BASE_COLORS.keys()) if BASE_COLORS else [
        "yellow", "orange", "pink", "red", "green", "blue", "violet", "black"
    ]
    coords = {name: {"X": 0, "Y": 0} for name in color_names + ["target"]}
    return {
        "coordinates": coords,
        "steps_per_coord_X": 1000,
        "steps_per_coord_Y": 1000,
        "z_up": 0,
        "z_down": 0,
        "pipette_up": 0,
        "pipette_down": 0,
        # Per-axis speed (steps/s) and acceleration (steps/s^2).
        # Speed only affects timing; step-to-distance ratio is unchanged.
        "speed_X": 4000,
        "speed_Y": 4000,
        "speed_Z": 2000,
        "speed_P": 4000,
        "accel_X": 2000,
        "accel_Y": 2000,
        "accel_Z": 1000,
        "accel_P": 2000,
        # ---- Vision / camera ----
        # Device indices for overhead and head-mounted cameras. On Windows,
        # 0 is usually the first USB webcam; try 1, 2 to find each camera.
        # Set head index to -1 if you only have the overhead camera.
        "camera_overhead_index": 0,
        "camera_head_index": -1,
        # Calibration matrix + anchor points (empty until user calibrates).
        "calibration": None,
        # Virtual boundary polygon (empty until user draws one).
        "boundary": None,
    }


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        base = default_config()
        for k, v in base.items():
            cfg.setdefault(k, v)
        for name in list(BASE_COLORS.keys()) + ["target"]:
            cfg["coordinates"].setdefault(name, {"X": 0, "Y": 0})
            entry = cfg["coordinates"][name]
            entry.setdefault("X", 0)
            entry.setdefault("Y", 0)
            entry.pop("Z_bottom", None)
            entry.pop("Z", None)
        return cfg
    except Exception as e:
        print(f"Config load failed ({e}); using defaults")
        return default_config()


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        messagebox.showerror("Save failed", f"Could not write config:\n{e}")


def find_best_recipe(target_hex: str, total_drops_max: int = 10,
                     max_colors: int = 9, delta_e_ok: float = 1.5):
    if not MIXER_AVAILABLE:
        return None
    best = None
    n = 2
    while n <= max_colors:
        result = suggest_recipes(target_hex, n_max=n,
                                 total_drops_max=total_drops_max, top_k=1)
        if not result:
            n += 1; continue
        r = result[0]
        best = r
        if r.delta_e < delta_e_ok:
            break
        n += 1
    return best


def recipe_to_fractions(recipe) -> "defaultdict[str, list]":
    out = defaultdict(dict)
    total = sum(recipe.drops)
    for i, (c, d) in enumerate(zip(recipe.colors, recipe.drops)):
        out[f"color {i+1}"] = [c, d / total]
    return out


# ============================================================
# AXIS LIMITS
# ============================================================

def axis_limits(cfg: dict, axis: str) -> tuple[int, int] | None:
    """Return (lo, hi) tracked-step window for a limited axis, or None if
    the axis is unlimited (no calibration yet, or axis is X/Y)."""
    if axis == "Z":
        lo_key, hi_key = "z_down", "z_up"
    elif axis == "P":
        lo_key, hi_key = "pipette_up", "pipette_down"
    else:
        return None
    lo, hi = cfg.get(lo_key, 0), cfg.get(hi_key, 0)
    if lo == 0 and hi == 0:
        return None                          # uncalibrated — no limit yet
    # Ensure ordered
    return (min(lo, hi), max(lo, hi))


# ============================================================
# SETTINGS DIALOG
# ============================================================

class SettingsDialog(tk.Toplevel):
    """Coordinates + calibration + speed editor."""

    def __init__(self, parent, cfg: dict, on_save,
                 current_position: dict[str, int],
                 push_speeds_callback):
        super().__init__(parent)
        self.title("Settings — Coordinates, calibration & speed")
        self.transient(parent)
        self.grab_set()
        self.cfg = cfg
        self.on_save = on_save
        self.current_position = current_position
        self.push_speeds_callback = push_speeds_callback

        self.entries: dict[str, dict[str, ttk.Entry]] = {}
        pad = {"padx": 6, "pady": 3}

        # Build with a Notebook so it doesn't get too tall
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # ---- Tab 1: Coordinates + XY calibration ----
        tab_coord = ttk.Frame(nb)
        nb.add(tab_coord, text="Vial coordinates")

        coord_frame = ttk.LabelFrame(tab_coord, text="Vial coordinates (in coordinate units)")
        coord_frame.pack(fill="both", expand=True, padx=8, pady=8)

        for c, h in enumerate(["Vial", "X (coord)", "Y (coord)"]):
            ttk.Label(coord_frame, text=h, anchor="center", width=14
                      ).grid(row=0, column=c, **pad)
        for row_i, name in enumerate(cfg["coordinates"].keys(), start=1):
            ttk.Label(coord_frame, text=name).grid(row=row_i, column=0, sticky="w", **pad)
            self.entries[name] = {}
            for col_i, key in enumerate(("X", "Y"), start=1):
                e = ttk.Entry(coord_frame, width=12, justify="right")
                e.insert(0, str(cfg["coordinates"][name].get(key, 0)))
                e.grid(row=row_i, column=col_i, **pad)
                self.entries[name][key] = e

        cal_frame = ttk.LabelFrame(tab_coord, text="Axis scale (motor steps per 1 coordinate unit)")
        cal_frame.pack(fill="x", padx=8, pady=8)
        self.spc_x = ttk.Entry(cal_frame, width=12, justify="right")
        self.spc_x.insert(0, str(cfg.get("steps_per_coord_X", 1000)))
        self.spc_y = ttk.Entry(cal_frame, width=12, justify="right")
        self.spc_y.insert(0, str(cfg.get("steps_per_coord_Y", 1000)))
        ttk.Label(cal_frame, text="Steps per 1 X coord:").grid(row=0, column=0, sticky="w", **pad)
        self.spc_x.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cal_frame, text="Steps per 1 Y coord:").grid(row=1, column=0, sticky="w", **pad)
        self.spc_y.grid(row=1, column=1, sticky="w", **pad)

        # ---- Tab 2: Z & Pipette calibration ----
        tab_zp = ttk.Frame(nb)
        nb.add(tab_zp, text="Z & Pipette limits")

        zp_frame = ttk.LabelFrame(tab_zp, text="Z axis & Pipette positions (raw motor steps)")
        zp_frame.pack(fill="both", expand=True, padx=8, pady=8)

        self.z_up = ttk.Entry(zp_frame, width=12, justify="right")
        self.z_up.insert(0, str(cfg.get("z_up", 0)))
        self.z_down = ttk.Entry(zp_frame, width=12, justify="right")
        self.z_down.insert(0, str(cfg.get("z_down", 0)))
        self.p_up = ttk.Entry(zp_frame, width=12, justify="right")
        self.p_up.insert(0, str(cfg.get("pipette_up", 0)))
        self.p_down = ttk.Entry(zp_frame, width=12, justify="right")
        self.p_down.insert(0, str(cfg.get("pipette_down", 0)))

        rows = [
            ("Z up   (safe travel, head raised):",       self.z_up,   "Z"),
            ("Z down (pipetting depth, head lowered):",  self.z_down, "Z"),
            ("Pipette up   (plunger relaxed, holds liquid):", self.p_up,   "P"),
            ("Pipette down (plunger pushed, expels liquid):", self.p_down, "P"),
        ]
        for r, (label, entry, axis) in enumerate(rows):
            ttk.Label(zp_frame, text=label).grid(row=r, column=0, sticky="w", **pad)
            entry.grid(row=r, column=1, sticky="w", **pad)
            ttk.Button(
                zp_frame, text=f"Capture from {axis}",
                command=lambda e=entry, a=axis: self._capture(e, a),
            ).grid(row=r, column=2, sticky="w", **pad)

        note = (
            "TERMINOLOGY  —  \"up\" always means the disengaged / at-rest state:\n"
            "   Z up          →  head is raised, tip clear of vials\n"
            "   Z down        →  head is lowered into a vial\n"
            "   Pipette up    →  plunger relaxed, tip holds liquid\n"
            "   Pipette down  →  plunger pushed all the way, liquid expelled\n\n"
            "SAFETY LIMITS  —  once BOTH up and down are set (non-zero),\n"
            "the Z and pipette axes cannot move outside this window.\n"
            "   • Manual jog: move is CLAMPED to the window; warning shown\n"
            "   • Auto mode:  out-of-window move is REFUSED; sequence stops\n"
            "Fresh configs (both = 0) are unlimited until calibrated.\n\n"
            "CAPTURE = read the machine's currently tracked position for that axis."
        )
        ttk.Label(zp_frame, text=note, foreground="gray", justify="left"
                  ).grid(row=len(rows), column=0, columnspan=3, sticky="w", **pad)

        # ---- Tab 3: Speed / acceleration ----
        tab_speed = ttk.Frame(nb)
        nb.add(tab_speed, text="Speed")

        sp_frame = ttk.LabelFrame(
            tab_speed,
            text="Per-axis max speed (steps/s) and acceleration (steps/s²)"
        )
        sp_frame.pack(fill="both", expand=True, padx=8, pady=8)

        ttk.Label(sp_frame, text="Axis").grid(row=0, column=0, **pad)
        ttk.Label(sp_frame, text="Max speed").grid(row=0, column=1, **pad)
        ttk.Label(sp_frame, text="Acceleration").grid(row=0, column=2, **pad)

        self.speed_entries: dict[str, ttk.Entry] = {}
        self.accel_entries: dict[str, ttk.Entry] = {}
        for r, axis in enumerate(("X", "Y", "Z", "P"), start=1):
            ttk.Label(sp_frame, text=axis, width=6).grid(row=r, column=0, **pad)
            e_sp = ttk.Entry(sp_frame, width=12, justify="right")
            e_sp.insert(0, str(cfg.get(f"speed_{axis}", 4000)))
            e_sp.grid(row=r, column=1, **pad)
            self.speed_entries[axis] = e_sp
            e_ac = ttk.Entry(sp_frame, width=12, justify="right")
            e_ac.insert(0, str(cfg.get(f"accel_{axis}", 2000)))
            e_ac.grid(row=r, column=2, **pad)
            self.accel_entries[axis] = e_ac

        speed_note = (
            "1 step travels the SAME physical distance at any speed.\n"
            "Only the time-between-steps changes. Coordinate calibration is\n"
            "unaffected by speed changes.\n\n"
            "Typical ranges (safe starting values):\n"
            "   X, Y : 2000 – 8000 steps/s  (light head, can go faster)\n"
            "   Z    : 1000 – 4000 steps/s  (mind gravity + heavy pipette)\n"
            "   P    : 2000 – 6000 steps/s  (aspiration should not be too fast)\n\n"
            "If a motor stalls or skips steps, LOWER the speed.\n"
            "Speeds are pushed to the Arduino on Save. Requires firmware V2\n"
            "(OTTO_TMC2209_minimal_v2). Old firmware ignores the <S> command."
        )
        ttk.Label(sp_frame, text=speed_note, foreground="gray", justify="left"
                  ).grid(row=6, column=0, columnspan=3, sticky="w", **pad)

        # ---- Tab 4: Camera & vision (V8) ----
        if VISION_AVAILABLE:
            tab_cam = ttk.Frame(nb)
            nb.add(tab_cam, text="Camera")

            cam_frame = ttk.LabelFrame(tab_cam, text="Camera device indices")
            cam_frame.pack(fill="x", padx=8, pady=8)

            ttk.Label(cam_frame, text="Overhead camera index:").grid(
                row=0, column=0, sticky="w", **pad)
            self.cam_ov = ttk.Entry(cam_frame, width=6, justify="right")
            self.cam_ov.insert(0, str(cfg.get("camera_overhead_index", 0)))
            self.cam_ov.grid(row=0, column=1, sticky="w", **pad)

            ttk.Label(cam_frame, text="Head-mounted camera index:").grid(
                row=1, column=0, sticky="w", **pad)
            self.cam_hd = ttk.Entry(cam_frame, width=6, justify="right")
            self.cam_hd.insert(0, str(cfg.get("camera_head_index", 1)))
            self.cam_hd.grid(row=1, column=1, sticky="w", **pad)

            ttk.Label(cam_frame, text=(
                "Try 0, 1, 2 to find each camera. Use -1 for the head-mounted\n"
                "camera if you only have the overhead one connected.\n"
                "Changes take effect after you stop and restart cameras\n"
                "on the Camera tab."
            ), foreground="gray", justify="left"
                     ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

            # Show current calibration & boundary status (read-only info)
            info_frame = ttk.LabelFrame(tab_cam, text="Current vision state")
            info_frame.pack(fill="x", padx=8, pady=8)

            cal_status = "not calibrated" if not (cfg.get("calibration")) else "calibrated"
            bnd_dict = cfg.get("boundary")
            n_verts = len(bnd_dict.get("vertices_step", [])) if bnd_dict else 0
            bnd_status = "not set" if n_verts < 3 else f"{n_verts} vertices"
            home_status = "not set" if not (bnd_dict and bnd_dict.get("home_step")) else "set"

            ttk.Label(info_frame, text=f"Pixel↔step calibration: {cal_status}"
                      ).pack(anchor="w", padx=8, pady=2)
            ttk.Label(info_frame, text=f"Boundary polygon:       {bnd_status}"
                      ).pack(anchor="w", padx=8, pady=2)
            ttk.Label(info_frame, text=f"Home point:             {home_status}"
                      ).pack(anchor="w", padx=8, pady=2)

            ttk.Label(info_frame, text=(
                "Set up calibration, boundary, and home on the Camera tab in\n"
                "the main window. This settings tab only holds device indices.\n\n"
                "SAFETY NOTE: the virtual boundary is a SOFTWARE guard based on\n"
                "the camera view. If the camera is bumped or calibration is off,\n"
                "the boundary may not match reality. Software boundaries are NOT\n"
                "a substitute for physical limit switches on a real machine."
            ), foreground="gray", justify="left"
                     ).pack(anchor="w", padx=8, pady=6)

        # ---- Save / Cancel ----
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=4)

    def _capture(self, entry_widget: ttk.Entry, axis: str) -> None:
        entry_widget.delete(0, tk.END)
        entry_widget.insert(0, str(self.current_position.get(axis, 0)))

    def _save(self) -> None:
        try:
            for name, ax_entries in self.entries.items():
                for key, e in ax_entries.items():
                    self.cfg["coordinates"][name][key] = int(e.get())
            self.cfg["steps_per_coord_X"] = int(self.spc_x.get())
            self.cfg["steps_per_coord_Y"] = int(self.spc_y.get())
            self.cfg["z_up"] = int(self.z_up.get())
            self.cfg["z_down"] = int(self.z_down.get())
            self.cfg["pipette_up"] = int(self.p_up.get())
            self.cfg["pipette_down"] = int(self.p_down.get())
            for axis in ("X", "Y", "Z", "P"):
                self.cfg[f"speed_{axis}"] = max(1, int(self.speed_entries[axis].get()))
                self.cfg[f"accel_{axis}"] = max(1, int(self.accel_entries[axis].get()))
            if VISION_AVAILABLE:
                self.cfg["camera_overhead_index"] = int(self.cam_ov.get())
                self.cfg["camera_head_index"] = int(self.cam_hd.get())
        except ValueError:
            messagebox.showerror("Invalid input", "All values must be integers.")
            return
        save_config(self.cfg)
        self.on_save()
        # Push new speeds to Arduino if connected
        try:
            self.push_speeds_callback()
        except Exception as e:
            print(f"Could not push speeds to Arduino: {e}")
        self.destroy()


# ============================================================
# MAIN APPLICATION
# ============================================================

class UCASApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("UCAS Control Program V7")
        self.root.geometry("1180x830")

        self.ser: serial.Serial | None = None
        self.current_position = {"X": 0, "Y": 0, "Z": 0, "P": 0}
        self.stop_requested = False
        self.cfg = load_config()

        # ---- Vision state (only used if VISION_AVAILABLE) ----
        self.camera_overhead: CameraThread | None = None
        self.camera_head: CameraThread | None = None

        # Load calibration and boundary from config (empty if not set)
        self.calibration = Calibration() if VISION_AVAILABLE else None
        self.boundary = Boundary() if VISION_AVAILABLE else None
        if VISION_AVAILABLE:
            cal_dict = self.cfg.get("calibration")
            if cal_dict:
                self.calibration = Calibration.from_dict(cal_dict)
            bnd_dict = self.cfg.get("boundary")
            if bnd_dict:
                self.boundary = Boundary.from_dict(bnd_dict)

        self.big_font = ("Arial", 15, "bold")
        self.normal_font = ("Arial", 12)
        self.status_font = ("Arial", 13)
        style = ttk.Style()
        style.configure("Big.TButton", font=self.big_font, padding=8)
        style.configure("Big.TLabel", font=self.big_font)
        style.configure("Normal.TLabel", font=self.normal_font)
        style.configure("Status.TLabel", font=self.status_font)

        self._build_menu()
        self._build_top_bar()
        self._build_mode_switch()
        self._build_mode_container()

        self.mode_var.set("manual")
        self._show_mode("manual")

    # --------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Coordinates, calibration & speed...",
                                  command=self._open_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="Export config...", command=self._export_config)
        settings_menu.add_command(label="Import config...", command=self._import_config)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.root.config(menu=menubar)

    def _open_settings(self):
        SettingsDialog(self.root, self.cfg,
                       on_save=self._on_settings_saved,
                       current_position=self.current_position,
                       push_speeds_callback=self.push_speeds_to_arduino)

    def _on_settings_saved(self):
        if hasattr(self, "auto_frame") and self.auto_frame is not None:
            self.auto_frame.refresh_from_config()
        if hasattr(self, "manual_frame") and self.manual_frame is not None:
            self.manual_frame.refresh_position_display()

    def _export_config(self):
        path = filedialog.asksaveasfilename(defaultextension=".json",
                                            filetypes=[("JSON", "*.json")])
        if path:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2)

    def _import_config(self):
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                self.cfg = json.load(f)
            save_config(self.cfg)
            self._on_settings_saved()
            messagebox.showinfo("Imported", "Config imported.")
        except Exception as e:
            messagebox.showerror("Import failed", str(e))

    # --------------------------------------------------------
    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=8)
        ttk.Button(bar, text="Connect", command=self.connect,
                   style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Disconnect", command=self.disconnect,
                   style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Ping", command=self.ping,
                   style="Big.TButton", width=7).pack(side="left", padx=4)

        # ---- Home button (V8) ----
        ttk.Button(bar, text="⌂ Home", command=self.go_home,
                   style="Big.TButton", width=8
                   ).pack(side="left", padx=4)

        self.stop_btn = tk.Button(
            bar, text="■ FORCE STOP", command=self.request_stop,
            font=self.big_font, bg="#c62828", fg="white",
            activebackground="#8b0000", activeforeground="white",
            padx=14, pady=6, relief="raised", bd=3,
        )
        self.stop_btn.pack(side="right", padx=4)

        self.status = ttk.Label(bar, text="Disconnected", style="Status.TLabel",
                                foreground="gray")
        self.status.pack(side="left", padx=15)

    def request_stop(self):
        self.stop_requested = True
        self.status.config(text="STOP REQUESTED — halting after current command",
                           foreground="red")

    def clear_stop(self):
        self.stop_requested = False

    def _build_mode_switch(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(frame, text="Mode:", style="Big.TLabel"
                  ).pack(side="left", padx=(0, 10))
        self.mode_var = tk.StringVar(value="manual")
        ttk.Radiobutton(frame, text="Manual", variable=self.mode_var,
                        value="manual", command=lambda: self._show_mode("manual")
                        ).pack(side="left", padx=4)
        ttk.Radiobutton(frame, text="Automatic", variable=self.mode_var,
                        value="auto", command=lambda: self._show_mode("auto")
                        ).pack(side="left", padx=4)
        if VISION_AVAILABLE:
            ttk.Radiobutton(frame, text="Camera", variable=self.mode_var,
                            value="camera", command=lambda: self._show_mode("camera")
                            ).pack(side="left", padx=4)

    def _build_mode_container(self):
        self.mode_container = ttk.Frame(self.root)
        self.mode_container.pack(fill="both", expand=True, padx=10, pady=6)
        self.manual_frame = ManualModeFrame(self.mode_container, self)
        self.auto_frame = AutoModeFrame(self.mode_container, self)
        if VISION_AVAILABLE:
            self.camera_frame = CameraFrame(self.mode_container, self)
        else:
            self.camera_frame = None

    def _show_mode(self, mode: str):
        for f in (self.manual_frame, self.auto_frame, self.camera_frame):
            if f is not None:
                f.pack_forget()
        if mode == "manual":
            self.manual_frame.pack(fill="both", expand=True)
            self.manual_frame.refresh_position_display()
        elif mode == "auto":
            self.auto_frame.pack(fill="both", expand=True)
            self.auto_frame.refresh_from_config()
        elif mode == "camera" and self.camera_frame is not None:
            self.camera_frame.pack(fill="both", expand=True)
            self.camera_frame.on_show()

    # ========================================================
    # SERIAL
    # ========================================================
    def connect(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(PORT, BAUD, timeout=5)
            time.sleep(2)
            self.ser.reset_input_buffer()
            self.status.config(text=f"Connected to {PORT}", foreground="green")
            # Push speeds on connect so Arduino gets our stored config
            self.push_speeds_to_arduino()
        except Exception as e:
            self.ser = None
            self.status.config(text="Connection failed", foreground="red")
            messagebox.showerror("Connection error", str(e))

    def disconnect(self):
        if self.ser:
            try:
                if self.ser.is_open:
                    self.ser.close()
            except Exception:
                pass
        self.ser = None
        self.status.config(text="Disconnected", foreground="gray")

    def ping(self):
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return
        try:
            self.ser.reset_input_buffer()
            self.ser.write(b"<?>")
            self.ser.flush()
            start = time.time()
            while time.time() - start < 5:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    print("Arduino:", line)
                    if line == "<ready>":
                        self.status.config(text="Arduino READY", foreground="green")
                        return
                time.sleep(0.01)
            self.status.config(text="Ping timeout", foreground="red")
        except Exception as e:
            messagebox.showerror("Serial error", str(e))

    def push_speeds_to_arduino(self) -> None:
        """Send an <S ...> command to the Arduino with per-axis max speed and
        acceleration. Silent no-op if not connected."""
        if not self.ser or not self.ser.is_open:
            return
        # Send four separate commands, one per axis, so each axis can carry
        # its own acceleration value on the same line.
        for axis in ("X", "Y", "Z", "P"):
            speed = int(self.cfg.get(f"speed_{axis}", 4000))
            accel = int(self.cfg.get(f"accel_{axis}", 2000))
            cmd = f"S {axis}{speed} A{accel}"
            self._send_raw_and_wait_ok(cmd)

    def _send_raw_and_wait_ok(self, cmd: str) -> bool:
        """Send a command that is NOT a motion command (does not update
        position tracker). Used for speed configuration."""
        if not self.ser or not self.ser.is_open:
            return False
        try:
            self.ser.reset_input_buffer()
            self.ser.write(f"<{cmd}>".encode("utf-8"))
            self.ser.flush()
            return self._wait_for_ok()
        except Exception as e:
            print(f"Send failed for '{cmd}': {e}")
            return False

    # ========================================================
    # MOTION with axis limits
    # ========================================================
    def send_motion(self, cmd: str, enforce_limits: bool = True,
                    clamp_z_p: bool = False) -> bool:
        """Send one motion command; check axis limits before sending.

        enforce_limits: if True, refuse (or clamp) moves that would exit the
                        z/p limit window.
        clamp_z_p: if True, silently clamp Z/P deltas so the final position
                   stays inside the window. If False, refuse the move entirely
                   when it would exit the window.
        """
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return False

        # Parse command deltas and apply limits
        if enforce_limits:
            cmd = self._apply_limits_to_cmd(cmd, clamp=clamp_z_p)
            if cmd is None:
                return False   # refused

        full_cmd = f"<{cmd}>"
        print("Sending:", full_cmd)
        try:
            self.ser.reset_input_buffer()
            self.ser.write(full_cmd.encode("utf-8"))
            self.ser.flush()
            ok = self._wait_for_ok()
            if ok:
                self._apply_move_to_position(cmd)
                if hasattr(self, "manual_frame") and self.manual_frame is not None:
                    self.manual_frame.refresh_position_display()
            return ok
        except Exception as e:
            print("Serial error:", e)
            self.status.config(text="Serial communication error", foreground="red")
            messagebox.showerror("Serial error", str(e))
            return False

    def _apply_limits_to_cmd(self, cmd: str, clamp: bool) -> str | None:
        """Check the requested move against Z/P limits AND the XY boundary
        polygon (if calibration and boundary are set).

        - clamp=True: for Z/P adjust delta; for XY clamp target to boundary edge
        - clamp=False: refuse any move outside limits/boundary
        """
        parts = cmd.strip().split()
        if not parts or parts[0] != "M":
            return cmd
        head = parts[0]
        tokens = parts[1:]

        # ---- First pass: Z and P per-axis limits (unchanged from V7) ----
        out_tokens = []
        # Also remember requested X, Y deltas so we can do a 2D boundary check
        req_dx = 0
        req_dy = 0
        has_xy = False
        for token in tokens:
            if len(token) < 2:
                out_tokens.append(token); continue
            axis = token[0]
            try:
                delta = int(token[1:])
            except ValueError:
                out_tokens.append(token); continue

            if axis == "X":
                req_dx = delta
                has_xy = True
                out_tokens.append(token)   # will re-check below
                continue
            if axis == "Y":
                req_dy = delta
                has_xy = True
                out_tokens.append(token)   # will re-check below
                continue

            # Z or P per-axis limit check
            limits = axis_limits(self.cfg, axis) if axis in ("Z", "P") else None
            if limits is None:
                out_tokens.append(token)
                continue

            lo, hi = limits
            current = self.current_position[axis]
            target = current + delta
            if lo <= target <= hi:
                out_tokens.append(token)
                continue

            if clamp:
                new_target = max(lo, min(hi, target))
                new_delta = new_target - current
                if new_delta == 0:
                    self.status.config(
                        text=f"{axis}-axis at limit ({current}); move ignored",
                        foreground="orange")
                    continue
                out_tokens.append(f"{axis}{new_delta}")
                self.status.config(
                    text=f"{axis} move clamped: {delta:+d} -> {new_delta:+d}",
                    foreground="orange")
            else:
                messagebox.showerror(
                    "Move refused — out of range",
                    f"{axis}-axis move would exit the calibrated window.\n\n"
                    f"Current  {axis} = {current}\n"
                    f"Delta    = {delta:+d}  (target {target})\n"
                    f"Window   [{lo} … {hi}]"
                )
                return None

        # ---- Second pass: XY boundary polygon (V8) ----
        if has_xy and VISION_AVAILABLE and self.boundary is not None and self.boundary.is_valid:
            target_X = self.current_position["X"] + req_dx
            target_Y = self.current_position["Y"] + req_dy
            ok, new_X, new_Y, note = self.boundary.check_or_clamp_move(
                target_X, target_Y, clamp=clamp
            )
            if not ok:
                messagebox.showerror(
                    "Move refused — outside virtual boundary",
                    f"Target ({target_X}, {target_Y}) is outside the boundary polygon.\n\n"
                    f"{note}"
                )
                return None
            if note:
                # Boundary clamped the target: rewrite the X and Y tokens
                new_dx = new_X - self.current_position["X"]
                new_dy = new_Y - self.current_position["Y"]
                filtered = []
                for t in out_tokens:
                    if t.startswith("X") or t.startswith("Y"):
                        continue
                    filtered.append(t)
                if new_dx != 0:
                    filtered.append(f"X{new_dx}")
                if new_dy != 0:
                    filtered.append(f"Y{new_dy}")
                out_tokens = filtered
                self.status.config(
                    text=f"XY move clamped to boundary: ({req_dx:+d}, {req_dy:+d}) -> "
                         f"({new_dx:+d}, {new_dy:+d})",
                    foreground="orange")

        if not out_tokens:
            return None
        return head + " " + " ".join(out_tokens)

    def _apply_move_to_position(self, cmd: str):
        parts = cmd.strip().split()
        if not parts or parts[0] != "M":
            return
        for token in parts[1:]:
            if len(token) < 2:
                continue
            axis = token[0]
            if axis not in self.current_position:
                continue
            try:
                delta = int(token[1:])
            except ValueError:
                continue
            self.current_position[axis] += delta

    def _wait_for_ok(self) -> bool:
        start = time.time()
        while time.time() - start < COMMAND_TIMEOUT:
            try:
                if self.ser.in_waiting:
                    line = self.ser.readline().decode(errors="ignore").strip()
                    if line:
                        print("Arduino:", line)
                    if line == "<ok>":
                        return True
                    if line.startswith("<err"):
                        return False
                time.sleep(0.01)
                self.root.update()
            except Exception as e:
                print("Serial read error:", e)
                return False
        print(f"Timeout: no <ok> in {COMMAND_TIMEOUT}s.")
        return False

    # --------------------------------------------------------
    def move_axis_absolute(self, axis: str, target_step: int) -> bool:
        delta = target_step - self.current_position[axis]
        if delta == 0:
            return True
        # Auto-mode absolute moves refuse out-of-window moves.
        return self.send_motion(f"M {axis}{delta}", enforce_limits=True, clamp_z_p=False)

    def move_xy_synchronized(self, target_x_step: int, target_y_step: int) -> bool:
        dx = target_x_step - self.current_position["X"]
        dy = target_y_step - self.current_position["Y"]
        if dx == 0 and dy == 0:
            return True
        parts = []
        if dx != 0: parts.append(f"X{dx}")
        if dy != 0: parts.append(f"Y{dy}")
        # X/Y are unlimited, but we still parse through limit-checker (it will
        # pass through X and Y unchanged).
        return self.send_motion("M " + " ".join(parts),
                                enforce_limits=True, clamp_z_p=False)

    # ========================================================
    # VISION HELPERS (V8)
    # ========================================================
    def start_cameras(self) -> None:
        """Start whichever cameras aren't already running. Silent if OpenCV
        is missing. Head camera is skipped if its index is -1 or equals the
        overhead index (single-camera setup)."""
        if not VISION_AVAILABLE:
            return
        ov_idx = int(self.cfg.get("camera_overhead_index", 0))
        hd_idx = int(self.cfg.get("camera_head_index", -1))

        if self.camera_overhead is None:
            self.camera_overhead = CameraThread(ov_idx)
            if not self.camera_overhead.start():
                err = self.camera_overhead.last_error
                print(f"Overhead camera failed to start: {err}")
                self.camera_overhead = None

        # Only try the head camera if it has a distinct, non-negative index
        skip_head = (hd_idx < 0) or (hd_idx == ov_idx)
        if self.camera_head is None and not skip_head:
            self.camera_head = CameraThread(hd_idx)
            if not self.camera_head.start():
                err = self.camera_head.last_error
                print(f"Head camera failed to start: {err}")
                self.camera_head = None

    def stop_cameras(self) -> None:
        if self.camera_overhead is not None:
            self.camera_overhead.stop()
            self.camera_overhead = None
        if self.camera_head is not None:
            self.camera_head.stop()
            self.camera_head = None

    def save_vision_to_config(self) -> None:
        """Serialise current calibration and boundary into the persistent config."""
        if not VISION_AVAILABLE:
            return
        self.cfg["calibration"] = self.calibration.to_dict() if self.calibration.is_valid else None
        self.cfg["boundary"] = self.boundary.to_dict() if (self.boundary and self.boundary.is_valid) else None
        save_config(self.cfg)

    def go_home(self) -> None:
        """Jog X and Y to the boundary's home point. Z is raised to z_up first
        for safety (in case the user forgot to lift the head)."""
        if not VISION_AVAILABLE:
            messagebox.showinfo("Vision not available",
                                "Camera / vision features require OpenCV.")
            return
        if self.boundary is None or self.boundary.home_step is None:
            messagebox.showwarning(
                "No home point",
                "Set a home point first: on the Camera tab, click 'Set home' "
                "and then click a point inside the boundary polygon on the "
                "camera feed."
            )
            return
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return

        # Safety: raise Z to z_up before moving in XY
        z_up = int(self.cfg.get("z_up", 0))
        if self.cfg.get("z_up", 0) != 0 or self.cfg.get("z_down", 0) != 0:
            self.move_axis_absolute("Z", z_up)

        hx, hy = self.boundary.home_step
        ok = self.move_xy_synchronized(hx, hy)
        if ok:
            self.status.config(text=f"Home reached ({hx}, {hy})", foreground="green")
        else:
            self.status.config(text="Go home failed", foreground="red")


# ============================================================
# MANUAL MODE
# ============================================================

class ManualModeFrame(ttk.Frame):
    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app
        self.sequence: list[str] = []
        self.step_size = tk.IntVar(value=1000)

        pad = {"padx": 8, "pady": 6}

        row = 0
        ttk.Label(self, text="Step size:", style="Big.TLabel"
                  ).grid(row=row, column=0, **pad)
        ttk.Entry(self, textvariable=self.step_size, font=app.big_font, width=10
                  ).grid(row=row, column=1, **pad)
        ttk.Label(self, text="1 – 10000 steps", style="Normal.TLabel"
                  ).grid(row=row, column=2, columnspan=2, sticky="w")

        row = 1
        for axis in ["X", "Y", "Z", "P"]:
            ttk.Label(self, text=f"{axis} axis", style="Big.TLabel"
                      ).grid(row=row, column=0, **pad)
            ttk.Button(self, text="←  −",
                       command=lambda a=axis: self.add_move(a, -1),
                       style="Big.TButton", width=8).grid(row=row, column=1, **pad)
            ttk.Button(self, text="+  →",
                       command=lambda a=axis: self.add_move(a, 1),
                       style="Big.TButton", width=8).grid(row=row, column=2, **pad)

            if axis == "Z":
                cap = ttk.Frame(self)
                cap.grid(row=row, column=3, sticky="w", **pad)
                ttk.Button(cap, text="Capture as Z up",
                           command=lambda: self._capture_axis("z_up", "Z"), width=18
                           ).pack(side="left", padx=(0, 4))
                ttk.Button(cap, text="Capture as Z down",
                           command=lambda: self._capture_axis("z_down", "Z"), width=18
                           ).pack(side="left")
            elif axis == "P":
                cap = ttk.Frame(self)
                cap.grid(row=row, column=3, sticky="w", **pad)
                ttk.Button(cap, text="Capture as Pipette up",
                           command=lambda: self._capture_axis("pipette_up", "P"), width=22
                           ).pack(side="left", padx=(0, 4))
                ttk.Button(cap, text="Capture as Pipette down",
                           command=lambda: self._capture_axis("pipette_down", "P"), width=22
                           ).pack(side="left")
            row += 1

        # Position readout
        pos_frame = ttk.LabelFrame(self, text="Tracked position (raw motor steps)")
        pos_frame.grid(row=row, column=0, columnspan=4, sticky="ew", padx=8, pady=6)
        row += 1
        self.pos_labels: dict[str, ttk.Label] = {}
        for i, axis in enumerate(("X", "Y", "Z", "P")):
            ttk.Label(pos_frame, text=f"{axis}:", style="Big.TLabel"
                      ).grid(row=0, column=i * 2, padx=6, pady=4)
            lbl = ttk.Label(pos_frame, text="0", style="Big.TLabel",
                            foreground="blue", width=8)
            lbl.grid(row=0, column=i * 2 + 1, padx=6, pady=4, sticky="w")
            self.pos_labels[axis] = lbl
        ttk.Button(pos_frame, text="Zero all (set current as origin)",
                   command=self._zero_all_axes, style="Big.TButton"
                   ).grid(row=0, column=8, padx=10)

        # Sequence
        ttk.Label(self, text="Stored sequence:", style="Big.TLabel"
                  ).grid(row=row, column=0, sticky="nw", **pad)
        self.sequence_box = tk.Text(self, height=10, width=60, font=("Consolas", 13))
        self.sequence_box.grid(row=row, column=1, columnspan=4, **pad)
        self.sequence_box.tag_configure("running", background="yellow")
        self.sequence_box.tag_configure("done", foreground="gray")
        self.sequence_box.tag_configure("aborted", foreground="red")
        row += 1

        ttk.Button(self, text="START", command=self.run_sequence,
                   style="Big.TButton", width=12
                   ).grid(row=row, column=1, **pad)
        ttk.Button(self, text="Run Last", command=self.run_last,
                   style="Big.TButton", width=10
                   ).grid(row=row, column=2, **pad)
        ttk.Button(self, text="Clear", command=self.clear_sequence,
                   style="Big.TButton", width=10
                   ).grid(row=row, column=3, **pad)

    def refresh_position_display(self):
        for axis, lbl in self.pos_labels.items():
            lbl.config(text=str(self.app.current_position[axis]))

    def _zero_all_axes(self):
        for axis in ("X", "Y", "Z", "P"):
            self.app.current_position[axis] = 0
        self.refresh_position_display()
        self.app.status.config(text="Origin set at current position", foreground="green")

    def _capture_axis(self, cfg_key: str, axis: str):
        self.app.cfg[cfg_key] = self.app.current_position[axis]
        save_config(self.app.cfg)
        messagebox.showinfo("Captured", f"{cfg_key} = {self.app.cfg[cfg_key]} (steps)")

    def get_step_size(self):
        try:
            v = int(self.step_size.get())
            if v < 1 or v > 10000:
                raise ValueError
            return v
        except Exception:
            messagebox.showerror("Invalid step size", "Step size must be 1–10000.")
            return None

    def add_move(self, axis, direction):
        step = self.get_step_size()
        if step is None:
            return
        self.sequence.append(f"M {axis}{step * direction}")
        self.update_sequence_box()

    def update_sequence_box(self):
        self.sequence_box.delete("1.0", tk.END)
        for i, cmd in enumerate(self.sequence, start=1):
            self.sequence_box.insert(tk.END, f"{i:02d}: <{cmd}>\n")

    def clear_sequence(self):
        self.sequence.clear()
        self.update_sequence_box()
        self.app.status.config(text="Sequence cleared", foreground="gray")

    def run_sequence(self):
        if not self.sequence:
            messagebox.showinfo("No sequence", "No stored commands to run.")
            return
        if not self.app.ser or not self.app.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return

        self.app.clear_stop()
        self.app.status.config(text="Running sequence...", foreground="blue")
        self.app.root.update()

        for i, cmd in enumerate(self.sequence):
            if self.app.stop_requested:
                self._mark_aborted_from(i)
                self.app.status.config(text=f"STOPPED before command {i+1}. "
                                            f"{len(self.sequence) - i} discarded.",
                                       foreground="red")
                messagebox.showwarning("Force stopped",
                                       f"Sequence aborted.\n"
                                       f"{len(self.sequence) - i} command(s) discarded.")
                return

            self.sequence_box.tag_remove("running", "1.0", tk.END)
            ln = i + 1
            self.sequence_box.tag_add("running", f"{ln}.0", f"{ln}.end")
            self.sequence_box.see(f"{ln}.0")
            self.app.status.config(text=f"Running {i+1}/{len(self.sequence)}: <{cmd}>",
                                   foreground="blue")
            self.app.root.update()

            # Manual mode CLAMPS Z/P moves (silent) rather than refusing
            if not self.app.send_motion(cmd, enforce_limits=True, clamp_z_p=True):
                self.app.status.config(text=f"STOPPED at {i+1}: <{cmd}>", foreground="red")
                messagebox.showerror("Sequence stopped",
                                     f"Command {i+1} failed:\n\n<{cmd}>")
                return

            self.sequence_box.tag_remove("running", f"{ln}.0", f"{ln}.end")
            self.sequence_box.tag_add("done", f"{ln}.0", f"{ln}.end")

            if self.app.stop_requested and i + 1 < len(self.sequence):
                self._mark_aborted_from(i + 1)
                self.app.status.config(text=f"STOPPED after command {i+1}. "
                                            f"{len(self.sequence) - (i + 1)} discarded.",
                                       foreground="red")
                messagebox.showwarning("Force stopped",
                                       f"Sequence aborted after command {i+1}.\n"
                                       f"{len(self.sequence) - (i + 1)} command(s) discarded.")
                return

            time.sleep(DELAY)

        self.sequence_box.tag_remove("running", "1.0", tk.END)
        self.app.status.config(text="Sequence complete", foreground="green")

    def _mark_aborted_from(self, start_index: int):
        self.sequence_box.tag_remove("running", "1.0", tk.END)
        for j in range(start_index, len(self.sequence)):
            ln = j + 1
            self.sequence_box.tag_add("aborted", f"{ln}.0", f"{ln}.end")

    def run_last(self):
        if not self.sequence:
            messagebox.showinfo("No command", "No command stored yet.")
            return
        cmd = self.sequence[-1]
        self.app.clear_stop()
        self.app.status.config(text=f"Running: <{cmd}>", foreground="blue")
        self.app.root.update()
        ok = self.app.send_motion(cmd, enforce_limits=True, clamp_z_p=True)
        self.app.status.config(text="Last command complete" if ok else "Last command failed",
                               foreground="green" if ok else "red")


# ============================================================
# AUTOMATIC MODE
# ============================================================

class AutoModeFrame(ttk.Frame):
    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app

        self.target_input_var = tk.StringVar()
        self.resolved_hex_var = tk.StringVar(value="—")
        self.multiplier_var = tk.StringVar(value="1")
        self.current_recipe = None
        self.current_action_list: list[dict] = []

        pad = {"padx": 8, "pady": 6}

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        ttk.Label(left, text="Target color:", style="Big.TLabel"
                  ).grid(row=0, column=0, sticky="w", **pad)
        self.hex_entry = ttk.Entry(left, textvariable=self.target_input_var,
                                   font=app.big_font, width=18)
        self.hex_entry.grid(row=0, column=1, sticky="w", **pad)
        self.hex_entry.bind("<KeyRelease>", lambda e: self._refresh_swatch())

        hint = ("Enter a hex code (#FF8B38) or a CSS color name."
                if WEBCOLORS_AVAILABLE else
                "Enter a hex code (#FF8B38). Install `webcolors` for name support.")
        ttk.Label(left, text=hint, style="Normal.TLabel", foreground="gray"
                  ).grid(row=1, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(left, text="Resolved:", style="Normal.TLabel"
                  ).grid(row=2, column=0, sticky="w", **pad)
        ttk.Label(left, textvariable=self.resolved_hex_var, style="Big.TLabel"
                  ).grid(row=2, column=1, sticky="w", **pad)

        ttk.Label(left, text="Recipe multiplier ×:", style="Big.TLabel"
                  ).grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(left, textvariable=self.multiplier_var, font=app.big_font, width=12
                  ).grid(row=3, column=1, sticky="w", **pad)

        ttk.Label(left, text="Target preview:", style="Normal.TLabel"
                  ).grid(row=4, column=0, sticky="w", **pad)
        self.swatch = tk.Canvas(left, width=140, height=60, bg="#ffffff",
                                highlightthickness=1, highlightbackground="black")
        self.swatch.grid(row=4, column=1, sticky="w", **pad)

        ttk.Button(left, text="Confirm target", command=self._on_confirm,
                   style="Big.TButton", width=16
                   ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        self.start_btn = ttk.Button(left, text="START mixing", command=self._on_start,
                                    style="Big.TButton", width=16, state="disabled")
        self.start_btn.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        ttk.Label(right, text="Computed recipe:", style="Big.TLabel").pack(anchor="w")
        self.recipe_box = tk.Text(right, height=7, width=64, font=("Consolas", 12))
        self.recipe_box.pack(fill="x", pady=4)

        ttk.Label(right, text="Planned actions:", style="Big.TLabel"
                  ).pack(anchor="w", pady=(8, 0))
        self.action_box = tk.Text(right, height=15, width=64, font=("Consolas", 12))
        self.action_box.pack(fill="both", expand=True, pady=4)
        self.action_box.tag_configure("running", background="yellow")
        self.action_box.tag_configure("done", foreground="gray")
        self.action_box.tag_configure("aborted", foreground="red")

        if not MIXER_AVAILABLE:
            self.recipe_box.insert(tk.END,
                f"⚠  mixer.py could not be imported.\n"
                f"Automatic mode is unavailable:\n\n  {MIXER_IMPORT_ERROR}\n\n"
                f"Manual mode still works.")
            self.hex_entry.configure(state="disabled")
            self.start_btn.configure(state="disabled")

    def refresh_from_config(self):
        pass

    def _refresh_swatch(self):
        hexcolor, err = resolve_color_input(self.target_input_var.get())
        if hexcolor:
            self.swatch.config(bg=hexcolor)
            self.resolved_hex_var.set(hexcolor)
        else:
            self.swatch.config(bg="#ffffff")
            self.resolved_hex_var.set("—")

    def _on_confirm(self):
        if not MIXER_AVAILABLE:
            messagebox.showerror("Mixer unavailable",
                                 f"mixer.py could not be imported:\n\n{MIXER_IMPORT_ERROR}")
            return

        # Resolve input to hex
        target, err = resolve_color_input(self.target_input_var.get())
        if err:
            messagebox.showwarning("Invalid color", err)
            self.start_btn.configure(state="disabled")
            return

        try:
            multiplier = int(self.multiplier_var.get())
            if multiplier < 1:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid multiplier",
                                   "Recipe multiplier must be a positive integer.")
            self.start_btn.configure(state="disabled")
            return

        self.resolved_hex_var.set(target)
        self._refresh_swatch()

        recipe = find_best_recipe(target)
        if recipe is None:
            messagebox.showerror("Recipe error", "Could not compute a recipe.")
            return
        self.current_recipe = recipe

        missing = []
        for color_name in recipe.colors:
            c = self.app.cfg["coordinates"].get(color_name, {})
            if c.get("X", 0) == 0 and c.get("Y", 0) == 0:
                missing.append(color_name)
        tgt = self.app.cfg["coordinates"].get("target", {})
        if tgt.get("X", 0) == 0 and tgt.get("Y", 0) == 0:
            missing.append("target")
        if missing:
            if not messagebox.askyesno(
                "Coordinates look unset",
                "These vials appear to have no coordinates:\n\n"
                + ", ".join(missing) + "\n\nContinue anyway?"):
                return

        # Warn if Z or pipette limits aren't set (auto mode uses their values
        # directly, and unlimited windows disable the safety check).
        z_lim = axis_limits(self.app.cfg, "Z")
        p_lim = axis_limits(self.app.cfg, "P")
        if z_lim is None or p_lim is None:
            missing_lims = []
            if z_lim is None: missing_lims.append("Z (z_up / z_down)")
            if p_lim is None: missing_lims.append("Pipette (pipette_up / pipette_down)")
            if not messagebox.askyesno(
                "Safety limits not set",
                "The following axes are not calibrated with min/max positions:\n\n"
                + "\n".join(missing_lims)
                + "\n\nWithout limits, out-of-range moves will NOT be caught.\n\n"
                  "Continue anyway?"):
                return

        fractions = recipe_to_fractions(recipe)
        total_cycles = sum(recipe.drops) * multiplier
        self.recipe_box.delete("1.0", tk.END)
        self.recipe_box.insert(tk.END, f"Target        : {target}\n")
        self.recipe_box.insert(tk.END, f"Predicted     : {recipe.predicted_hex}   (ΔE = {recipe.delta_e:.2f})\n")
        self.recipe_box.insert(tk.END, f"Multiplier    : ×{multiplier}\n")
        self.recipe_box.insert(tk.END, f"Total cycles  : {total_cycles} pipette actions\n\n")
        for key, (color, frac) in fractions.items():
            i = int(key.split()[1]) - 1
            cycles = recipe.drops[i] * multiplier
            self.recipe_box.insert(tk.END,
                f"{key}: {color:<10s} fraction={frac:.3f}  cycles={cycles}\n")

        self.current_action_list = self._build_action_plan(recipe, multiplier)
        self._refresh_action_box()
        self.start_btn.configure(state="normal")

    def _build_action_plan(self, recipe, multiplier: int) -> list[dict]:
        actions: list[dict] = []
        tgt = self.app.cfg["coordinates"]["target"]
        for color_idx, (color, base_drops) in enumerate(zip(recipe.colors, recipe.drops)):
            cycles = base_drops * multiplier
            src = self.app.cfg["coordinates"].get(color, {"X": 0, "Y": 0})
            for c in range(cycles):
                cycle_label = f"[color {color_idx+1}: {color}, cycle {c+1}/{cycles}]"
                actions.append({"kind": "aspirate",
                                "text": f"{cycle_label} Aspirate from {color}",
                                "src": src})
                actions.append({"kind": "dispense",
                                "text": f"{cycle_label} Dispense into target",
                                "tgt": tgt})
        return actions

    def _refresh_action_box(self):
        self.action_box.delete("1.0", tk.END)
        for i, act in enumerate(self.current_action_list, start=1):
            self.action_box.insert(tk.END, f"{i:03d}: {act['text']}\n")

    def _on_start(self):
        if not self.current_action_list:
            messagebox.showinfo("No plan", "Confirm a target first.")
            return
        if not self.app.ser or not self.app.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return

        self.app.clear_stop()
        self.start_btn.configure(state="disabled")
        self.app.status.config(text="AUTO: running mix", foreground="blue")
        self.app.root.update()

        try:
            for i, act in enumerate(self.current_action_list):
                if self.app.stop_requested:
                    self._mark_aborted_from(i)
                    self.app.status.config(
                        text=f"AUTO STOPPED before action {i+1}. "
                             f"{len(self.current_action_list) - i} discarded.",
                        foreground="red")
                    messagebox.showwarning("Force stopped",
                        f"Mixing aborted.\n"
                        f"{len(self.current_action_list) - i} action(s) discarded.")
                    self.start_btn.configure(state="normal")
                    return

                self.action_box.tag_remove("running", "1.0", tk.END)
                ln = i + 1
                self.action_box.tag_add("running", f"{ln}.0", f"{ln}.end")
                self.action_box.see(f"{ln}.0")
                self.app.status.config(
                    text=f"AUTO {i+1}/{len(self.current_action_list)}: {act['text']}",
                    foreground="blue")
                self.app.root.update()

                if act["kind"] == "aspirate":
                    ok = self._do_aspirate(act["src"])
                else:
                    ok = self._do_dispense(act["tgt"])

                if not ok:
                    if self.app.stop_requested:
                        self._mark_aborted_from(i)
                        self.app.status.config(
                            text=f"AUTO STOPPED during action {i+1}. "
                                 f"{len(self.current_action_list) - i} discarded.",
                            foreground="red")
                        messagebox.showwarning("Force stopped",
                            f"Mixing aborted during action {i+1}.")
                    else:
                        messagebox.showerror(
                            "Auto mix stopped",
                            f"Action {i+1} failed:\n\n{act['text']}")
                        self.app.status.config(text=f"AUTO STOPPED at {i+1}", foreground="red")
                    self.start_btn.configure(state="normal")
                    return

                self.action_box.tag_remove("running", f"{ln}.0", f"{ln}.end")
                self.action_box.tag_add("done", f"{ln}.0", f"{ln}.end")
        except Exception as e:
            messagebox.showerror("Auto mix error", str(e))
            self.app.status.config(text="AUTO error", foreground="red")
            self.start_btn.configure(state="normal")
            return

        self.app.status.config(text="AUTO complete", foreground="green")
        messagebox.showinfo("Mixing complete",
                            f"Mixing finished.\n\nTarget: {self.resolved_hex_var.get()}")
        self.start_btn.configure(state="normal")

    def _mark_aborted_from(self, start_index: int):
        self.action_box.tag_remove("running", "1.0", tk.END)
        for j in range(start_index, len(self.current_action_list)):
            ln = j + 1
            self.action_box.tag_add("aborted", f"{ln}.0", f"{ln}.end")

    # --------------------------------------------------------
    def _coord_to_step_x(self, coord: int) -> int:
        return int(coord) * int(self.app.cfg["steps_per_coord_X"])
    def _coord_to_step_y(self, coord: int) -> int:
        return int(coord) * int(self.app.cfg["steps_per_coord_Y"])
    def _stop_check(self) -> bool:
        return self.app.stop_requested

    def _do_aspirate(self, src: dict) -> bool:
        x_step = self._coord_to_step_x(src["X"])
        y_step = self._coord_to_step_y(src["Y"])
        z_up = int(self.app.cfg["z_up"])
        z_down = int(self.app.cfg["z_down"])
        p_up = int(self.app.cfg["pipette_up"])
        p_down = int(self.app.cfg["pipette_down"])

        if not self.app.move_xy_synchronized(x_step, y_step): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("Z", z_down): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("P", p_down): return False
        if self._stop_check(): return False
        time.sleep(DELAY)
        if not self.app.move_axis_absolute("P", p_up): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("Z", z_up): return False
        time.sleep(DELAY)
        return True

    def _do_dispense(self, tgt: dict) -> bool:
        x_step = self._coord_to_step_x(tgt["X"])
        y_step = self._coord_to_step_y(tgt["Y"])
        z_up = int(self.app.cfg["z_up"])
        z_down = int(self.app.cfg["z_down"])
        p_up = int(self.app.cfg["pipette_up"])
        p_down = int(self.app.cfg["pipette_down"])

        if not self.app.move_xy_synchronized(x_step, y_step): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("Z", z_down): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("P", p_down): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("Z", z_up): return False
        if self._stop_check(): return False
        time.sleep(DELAY)

        if not self.app.move_axis_absolute("P", p_up): return False
        time.sleep(DELAY)
        return True


# ============================================================
# CAMERA MODE (V8)
# ============================================================

class CameraFrame(ttk.Frame):
    """Live camera view with:
       - Overlay of boundary polygon, home point, current head position,
         and calibration anchor points
       - Buttons for calibration, boundary editing, and home setting
       - Interactive click modes on the canvas

    Click modes (mutually exclusive):
       - IDLE       : clicks do nothing
       - CAL_POINT_1: next click captures pixel for calibration anchor 1
       - CAL_POINT_2: next click captures pixel for calibration anchor 2
       - DRAW_POLY  : each click adds a polygon vertex; double-click closes
       - SET_HOME   : next click marks a point as home
    """

    # Poll the camera every this many ms
    FRAME_POLL_MS = 66     # ~15 fps display

    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app
        self._click_mode = "IDLE"
        self._pending_polygon: list[tuple[float, float]] = []
        self._last_photo = None    # keep a reference so tk doesn't GC it
        self._last_pil = None      # keep the underlying PIL image alive too
        self._latest_frame_size = (1, 1)   # actual frame w,h (before display resize)
        self._display_size = (1, 1)        # displayed w,h
        self._cameras_started = False

        pad = {"padx": 6, "pady": 6}

        # ---- Left: controls ----
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        ttk.Label(left, text="Cameras", style="Big.TLabel").pack(anchor="w", **pad)
        ttk.Button(left, text="Start cameras", command=self._start_cameras,
                   style="Big.TButton", width=18).pack(anchor="w", **pad)
        ttk.Button(left, text="Stop cameras", command=self._stop_cameras,
                   style="Big.TButton", width=18).pack(anchor="w", **pad)
        ttk.Button(left, text="Save current frame → PNG",
                   command=self._save_current_frame,
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        self.cam_status = ttk.Label(left, text="Cameras: not started",
                                    style="Normal.TLabel", foreground="gray")
        self.cam_status.pack(anchor="w", **pad)

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=6)

        ttk.Label(left, text="Calibration", style="Big.TLabel").pack(anchor="w", **pad)
        ttk.Label(left,
                  text=("2-point calibration:\n"
                        "  1. Jog head to point A, use 'Set A here'\n"
                        "  2. Click point A in the camera image\n"
                        "  3. Jog to point B (far from A), 'Set B here'\n"
                        "  4. Click point B in the image\n"
                        "Save when done."),
                  style="Normal.TLabel", foreground="gray", justify="left"
                  ).pack(anchor="w", **pad)
        ttk.Button(left, text="Set A here (current XY)",
                   command=lambda: self._set_calibration_step_anchor(0),
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Click A in image →",
                   command=lambda: self._enter_click_mode("CAL_POINT_1"),
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Set B here (current XY)",
                   command=lambda: self._set_calibration_step_anchor(1),
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Click B in image →",
                   command=lambda: self._enter_click_mode("CAL_POINT_2"),
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Compute & save calibration",
                   command=self._save_calibration,
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        self.cal_status = ttk.Label(left, text="Calibration: —",
                                    style="Normal.TLabel", foreground="gray")
        self.cal_status.pack(anchor="w", **pad)

        ttk.Separator(left, orient="horizontal").pack(fill="x", pady=6)

        ttk.Label(left, text="Boundary & home", style="Big.TLabel").pack(anchor="w", **pad)
        ttk.Button(left, text="Draw boundary polygon",
                   command=self._enter_draw_polygon_mode,
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Set home (click in image)",
                   command=lambda: self._enter_click_mode("SET_HOME"),
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        ttk.Button(left, text="Clear boundary",
                   command=self._clear_boundary,
                   style="Big.TButton", width=24).pack(anchor="w", **pad)
        self.mode_status = ttk.Label(left, text="Click mode: IDLE",
                                     style="Normal.TLabel", foreground="blue")
        self.mode_status.pack(anchor="w", **pad)

        # ---- Right: the live camera canvas ----
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        ttk.Label(right, text="Overhead camera (click to interact)",
                  style="Big.TLabel").pack(anchor="w")
        self.canvas = tk.Canvas(right, width=800, height=600, bg="black",
                                highlightthickness=1, highlightbackground="gray")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Double-Button-1>", self._on_canvas_double_click)

        # Working state for calibration anchors during setup
        self._cal_anchor_steps: list[tuple[int, int] | None] = [None, None]
        self._cal_anchor_pixels: list[tuple[float, float] | None] = [None, None]

        self._refresh_status_labels()

    # --------------------------------------------------------
    def on_show(self):
        """Called when the user switches to this tab."""
        self._refresh_status_labels()
        # Do NOT auto-start cameras — user starts them explicitly

    def _start_cameras(self):
        self.app.start_cameras()
        self._cameras_started = True
        self.cam_status.config(text="Cameras: started (polling)", foreground="green")
        # Kick off the frame polling loop
        self._poll_frame()

    def _stop_cameras(self):
        self.app.stop_cameras()
        self._cameras_started = False
        self.cam_status.config(text="Cameras: stopped", foreground="gray")

    def _save_current_frame(self):
        """Grab the latest overhead frame and write it to disk. Useful for
        diagnosing 'display is black' vs 'camera itself returns black'."""
        cam = self.app.camera_overhead
        if cam is None:
            messagebox.showwarning("No camera",
                "Overhead camera is not started.")
            return
        frame = cam.get_latest_frame()
        if frame is None:
            messagebox.showwarning("No frame",
                f"No frame available yet. Camera frame_count = {cam.frame_count}. "
                "Wait a moment and try again.")
            return
        try:
            import cv2
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "camera_snapshot.png")
            cv2.imwrite(path, frame)
            h, w = frame.shape[:2]
            mean = float(frame.mean())
            std = float(frame.std())
            verdict = ("Frame looks BLACK (mean<5, std<5) — camera issue"
                       if mean < 5 and std < 5 else
                       "Frame has content — display issue if it looked black in the app"
                       if std > 10 else
                       "Frame is very dim — check lighting")
            messagebox.showinfo("Frame saved",
                f"Saved to: {path}\n\n"
                f"Size:  {w}x{h}\n"
                f"Mean:  {mean:.1f} (0=black, 255=white)\n"
                f"Std:   {std:.1f} (variance across pixels)\n\n"
                f"{verdict}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # --------------------------------------------------------
    def _poll_frame(self):
        """Read the latest frame from the overhead camera, overlay,
        and paint onto the canvas. Reschedules itself."""
        if not self._cameras_started:
            return

        cam = self.app.camera_overhead
        if cam is not None:
            frame = cam.get_latest_frame()
            if frame is not None:
                self._latest_frame_size = (frame.shape[1], frame.shape[0])

                head_step = (self.app.current_position["X"],
                             self.app.current_position["Y"])

                # Overlay boundary, calibration, head position
                annotated = draw_overlays_on_frame(
                    frame,
                    self.app.boundary,
                    self.app.calibration,
                    head_step=head_step,
                )

                # Overlay pending polygon during drawing
                if self._click_mode == "DRAW_POLY" and self._pending_polygon:
                    import cv2, numpy as np
                    if len(self._pending_polygon) >= 1:
                        for (u, v) in self._pending_polygon:
                            cv2.circle(annotated, (int(u), int(v)), 4, (0, 255, 255), -1)
                    if len(self._pending_polygon) >= 2:
                        pts = np.array(
                            [[int(u), int(v)] for u, v in self._pending_polygon],
                            dtype=np.int32
                        )
                        cv2.polylines(annotated, [pts], isClosed=False,
                                      color=(0, 255, 255), thickness=2)

                # Convert to PhotoImage and display.
                # CRITICAL: we must keep references to BOTH the PhotoImage and
                # the underlying PIL Image, or Python GC will free the pixel
                # buffer and the canvas will show black. This is the standard
                # Tk PhotoImage gotcha.
                photo, pil_img = frame_to_tk_image(annotated, max_width=800)
                if photo is not None:
                    self._last_photo = photo
                    self._last_pil = pil_img
                    self.canvas.delete("frame")
                    # Draw at (0,0) anchored to top-left. Configure canvas size
                    # to match the image so the display area doesn't stay tiny.
                    self.canvas.config(width=photo.width(), height=photo.height())
                    self.canvas.create_image(0, 0, image=photo, anchor="nw", tags="frame")
                    self._display_size = (photo.width(), photo.height())

        self.after(self.FRAME_POLL_MS, self._poll_frame)

    # --------------------------------------------------------
    def _canvas_to_frame_coords(self, cx: int, cy: int) -> tuple[float, float]:
        """Convert a canvas click position to original frame coordinates.
        We display a resized frame, so we need to undo the resize."""
        disp_w, disp_h = self._display_size
        frame_w, frame_h = self._latest_frame_size
        if disp_w == 0 or disp_h == 0:
            return cx, cy
        u = cx * (frame_w / disp_w)
        v = cy * (frame_h / disp_h)
        return u, v

    def _on_canvas_click(self, event):
        if self._click_mode == "IDLE":
            return
        u, v = self._canvas_to_frame_coords(event.x, event.y)

        if self._click_mode == "CAL_POINT_1":
            self._cal_anchor_pixels[0] = (u, v)
            self._exit_click_mode()
            messagebox.showinfo("Point A captured",
                f"Pixel A recorded: ({u:.0f}, {v:.0f})\n\n"
                f"Now jog to point B and 'Set B here', then 'Click B in image'.")
            self._refresh_status_labels()

        elif self._click_mode == "CAL_POINT_2":
            self._cal_anchor_pixels[1] = (u, v)
            self._exit_click_mode()
            messagebox.showinfo("Point B captured",
                f"Pixel B recorded: ({u:.0f}, {v:.0f})\n\n"
                f"Click 'Compute & save calibration' to finish.")
            self._refresh_status_labels()

        elif self._click_mode == "DRAW_POLY":
            self._pending_polygon.append((u, v))
            self.mode_status.config(
                text=f"Drawing polygon: {len(self._pending_polygon)} points (double-click to finish)",
                foreground="blue")

        elif self._click_mode == "SET_HOME":
            self._set_home_from_pixel(u, v)
            self._exit_click_mode()

    def _on_canvas_double_click(self, event):
        if self._click_mode == "DRAW_POLY":
            self._finalize_polygon()

    # --------------------------------------------------------
    # CALIBRATION
    # --------------------------------------------------------
    def _set_calibration_step_anchor(self, which: int):
        """Record the machine's current XY as one of the two calibration anchors."""
        x = self.app.current_position["X"]
        y = self.app.current_position["Y"]
        self._cal_anchor_steps[which] = (x, y)
        letter = "A" if which == 0 else "B"
        self.cal_status.config(text=f"Step position {letter} set: ({x}, {y})",
                               foreground="blue")

    def _save_calibration(self):
        for i, letter in enumerate(("A", "B")):
            if self._cal_anchor_steps[i] is None:
                messagebox.showwarning("Incomplete calibration",
                                       f"Step position for point {letter} is not set.")
                return
            if self._cal_anchor_pixels[i] is None:
                messagebox.showwarning("Incomplete calibration",
                                       f"Pixel position for point {letter} is not set.")
                return
        try:
            cal = Calibration.from_two_points(
                self._cal_anchor_pixels[0], self._cal_anchor_steps[0],
                self._cal_anchor_pixels[1], self._cal_anchor_steps[1],
            )
        except ValueError as e:
            messagebox.showerror("Calibration failed", str(e))
            return
        self.app.calibration = cal
        self.app.save_vision_to_config()
        messagebox.showinfo("Calibration saved",
            f"Calibration ready.\n\n"
            f"Pixel A ({self._cal_anchor_pixels[0][0]:.0f}, {self._cal_anchor_pixels[0][1]:.0f}) "
            f"↔ step {self._cal_anchor_steps[0]}\n"
            f"Pixel B ({self._cal_anchor_pixels[1][0]:.0f}, {self._cal_anchor_pixels[1][1]:.0f}) "
            f"↔ step {self._cal_anchor_steps[1]}")
        self._refresh_status_labels()

    # --------------------------------------------------------
    # BOUNDARY POLYGON
    # --------------------------------------------------------
    def _enter_draw_polygon_mode(self):
        if not self.app.calibration.is_valid:
            messagebox.showwarning("Calibrate first",
                "Set up pixel↔step calibration before drawing a boundary.")
            return
        self._pending_polygon = []
        self._enter_click_mode("DRAW_POLY")
        self.mode_status.config(
            text="Drawing polygon: click to add points, double-click to finish",
            foreground="blue")

    def _finalize_polygon(self):
        if len(self._pending_polygon) < 3:
            messagebox.showwarning("Polygon too small",
                "Need at least 3 points for a polygon.")
            self._pending_polygon = []
            self._exit_click_mode()
            return

        # Convert to step space
        step_vertices = []
        for (u, v) in self._pending_polygon:
            try:
                sx, sy = self.app.calibration.pixel_to_step(u, v)
                step_vertices.append((sx, sy))
            except Exception as e:
                messagebox.showerror("Boundary error", f"Could not convert pixel to step: {e}")
                self._pending_polygon = []
                self._exit_click_mode()
                return

        self.app.boundary.vertices_step = step_vertices
        self.app.boundary.vertices_pixel = list(self._pending_polygon)
        self.app.save_vision_to_config()
        n = len(step_vertices)
        self._pending_polygon = []
        self._exit_click_mode()
        messagebox.showinfo("Boundary saved",
                            f"{n}-vertex boundary polygon saved.")
        self._refresh_status_labels()

    def _clear_boundary(self):
        if not messagebox.askyesno("Clear boundary",
                                   "Delete the boundary polygon and home point?"):
            return
        self.app.boundary.vertices_step = []
        self.app.boundary.vertices_pixel = []
        self.app.boundary.home_step = None
        self.app.save_vision_to_config()
        self._refresh_status_labels()

    # --------------------------------------------------------
    # HOME
    # --------------------------------------------------------
    def _set_home_from_pixel(self, u: float, v: float):
        if not self.app.calibration.is_valid:
            messagebox.showwarning("Calibrate first", "Set calibration first.")
            return
        sx, sy = self.app.calibration.pixel_to_step(u, v)
        if self.app.boundary.is_valid and not self.app.boundary.contains_step_point(sx, sy):
            if not messagebox.askyesno(
                "Home outside boundary",
                f"The point ({sx}, {sy}) is OUTSIDE the boundary polygon.\n\n"
                "That means 'Go home' would refuse to move there.\n\n"
                "Save anyway?"
            ):
                return
        self.app.boundary.home_step = (sx, sy)
        self.app.save_vision_to_config()
        messagebox.showinfo("Home set", f"Home point set at step ({sx}, {sy}).")
        self._refresh_status_labels()

    # --------------------------------------------------------
    def _enter_click_mode(self, mode: str):
        self._click_mode = mode
        self.mode_status.config(text=f"Click mode: {mode}", foreground="blue")
        self.canvas.config(cursor="crosshair")

    def _exit_click_mode(self):
        self._click_mode = "IDLE"
        self.mode_status.config(text="Click mode: IDLE", foreground="blue")
        self.canvas.config(cursor="")

    def _refresh_status_labels(self):
        # Calibration
        if self.app.calibration is not None and self.app.calibration.is_valid:
            self.cal_status.config(text="Calibration: ready", foreground="green")
        else:
            self.cal_status.config(text="Calibration: not set", foreground="orange")


# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = UCASApp(root)
    # Clean up cameras and serial when window closes
    def _on_close():
        try:
            app.stop_cameras()
        except Exception:
            pass
        try:
            app.disconnect()
        except Exception:
            pass
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", _on_close)
    root.mainloop()