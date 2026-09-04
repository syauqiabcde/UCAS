"""
UCAS Control Program V5
=======================
Changes from V4:
  1. Fixed-volume pipette: recipes are executed as INTEGER pipette CYCLES
     per color (e.g. 3:1 -> 3 cycles from color 1, 1 cycle from color 2).
     Volume is expressed as "number of pipette actions" not mL.
  2. Every aspirate = full plunger stroke  rest -> full -> rest.
     Every dispense = rest -> full, then Z lifts, then plunger returns to rest.
  3. Aspirate procedure: XY move -> lower Z to vial's Z_bottom
     -> plunger cycle -> raise Z to Z_travel.
  4. Dispense procedure: XY move -> lower Z to target's Z_bottom
     -> push plunger to full -> raise Z to Z_travel -> release plunger to rest.
  5. X and Y vial positions are stored as user-friendly integer COORDINATES
     (1, 2, 3, ...). Conversion to motor steps is via per-axis calibration
     constants (steps_per_coord_X, steps_per_coord_Y).
  6. Z (per-vial "bottom" step position) and P (plunger) are calibrated
     as raw step positions. Values can be typed in Settings OR captured
     from the machine's current tracked position via "Capture" buttons
     in Manual mode.

Depends on: pyserial, mixer.py (must expose BASE_COLORS and suggest_recipes)
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
# Mixer import (guarded so auto mode fails gracefully if broken)
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


def default_config() -> dict:
    """Fresh config with zeroed coordinates for every known color + target
    and sensible calibration placeholders. USER MUST CALIBRATE before use."""
    color_names = list(BASE_COLORS.keys()) if BASE_COLORS else [
        "yellow", "orange", "pink", "red", "green", "blue", "violet", "black"
    ]
    coords = {}
    for name in color_names + ["target"]:
        # X, Y in COORDINATE UNITS; Z_bottom in raw motor steps
        coords[name] = {"X": 0, "Y": 0, "Z_bottom": 0}

    return {
        "coordinates": coords,
        # X, Y calibration: motor steps per 1 coordinate unit
        "steps_per_coord_X": 1000,
        "steps_per_coord_Y": 1000,
        # Z travel position (raw steps): the "safe up" position between vials
        "z_travel": 0,
        # Pipette calibration (raw steps): rest = plunger not pushed;
        # full = plunger pushed all the way
        "pipette_rest": 0,
        "pipette_full": 2000,
    }


def load_config() -> dict:
    if not os.path.exists(CONFIG_PATH):
        return default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Merge in any missing keys from defaults
        base = default_config()
        for k, v in base.items():
            cfg.setdefault(k, v)
        # Ensure every base color and target have entries
        for name in list(BASE_COLORS.keys()) + ["target"]:
            cfg["coordinates"].setdefault(name, {"X": 0, "Y": 0, "Z_bottom": 0})
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
    """Escalate n=2 upward until ΔE < delta_e_ok, capped at max_colors.
    Mirrors the user's test.py logic."""
    if not MIXER_AVAILABLE:
        return None
    best = None
    n = 2
    while n <= max_colors:
        result = suggest_recipes(target_hex, n_max=n,
                                 total_drops_max=total_drops_max, top_k=1)
        if not result:
            n += 1
            continue
        r = result[0]
        best = r
        if r.delta_e < delta_e_ok:
            break
        n += 1
    return best


def recipe_to_fractions(recipe) -> "defaultdict[str, list]":
    """Convert a mixer Recipe into {color n: [name, fraction]} dict."""
    out = defaultdict(dict)
    total = sum(recipe.drops)
    for i, (c, d) in enumerate(zip(recipe.colors, recipe.drops)):
        out[f"color {i+1}"] = [c, d / total]
    return out


# ============================================================
# SETTINGS DIALOG
# ============================================================

class SettingsDialog(tk.Toplevel):
    """Modal window for editing vial coordinates and calibration constants."""

    def __init__(self, parent, cfg: dict, on_save,
                 current_position: dict[str, int]):
        super().__init__(parent)
        self.title("Settings — Coordinates & calibration")
        self.transient(parent)
        self.grab_set()
        self.cfg = cfg
        self.on_save = on_save
        self.current_position = current_position

        self.entries: dict[str, dict[str, ttk.Entry]] = {}
        pad = {"padx": 6, "pady": 3}

        # ---- Coordinates table ----
        coord_frame = ttk.LabelFrame(
            self, text="Vial coordinates  —  X, Y in coordinate units, Z_bottom in motor steps"
        )
        coord_frame.pack(fill="both", expand=True, padx=10, pady=8)

        headers = ["Vial", "X (coord)", "Y (coord)", "Z_bottom (steps)"]
        for c, h in enumerate(headers):
            ttk.Label(coord_frame, text=h, anchor="center", width=14
                      ).grid(row=0, column=c, **pad)

        for row_i, name in enumerate(cfg["coordinates"].keys(), start=1):
            ttk.Label(coord_frame, text=name).grid(row=row_i, column=0, sticky="w", **pad)
            self.entries[name] = {}
            for col_i, key in enumerate(("X", "Y", "Z_bottom"), start=1):
                e = ttk.Entry(coord_frame, width=12, justify="right")
                e.insert(0, str(cfg["coordinates"][name].get(key, 0)))
                e.grid(row=row_i, column=col_i, **pad)
                self.entries[name][key] = e

        # ---- Axis-scale calibration ----
        cal_frame = ttk.LabelFrame(self, text="Axis-scale calibration (motor steps per 1 coordinate unit)")
        cal_frame.pack(fill="x", padx=10, pady=8)

        self.spc_x = ttk.Entry(cal_frame, width=12, justify="right")
        self.spc_x.insert(0, str(cfg.get("steps_per_coord_X", 1000)))
        self.spc_y = ttk.Entry(cal_frame, width=12, justify="right")
        self.spc_y.insert(0, str(cfg.get("steps_per_coord_Y", 1000)))

        ttk.Label(cal_frame, text="Steps per 1 X coord:").grid(row=0, column=0, sticky="w", **pad)
        self.spc_x.grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(cal_frame, text="Steps per 1 Y coord:").grid(row=1, column=0, sticky="w", **pad)
        self.spc_y.grid(row=1, column=1, sticky="w", **pad)

        # ---- Z & Pipette positions (raw steps, capturable) ----
        zp_frame = ttk.LabelFrame(self, text="Z travel & Pipette calibration (raw motor steps)")
        zp_frame.pack(fill="x", padx=10, pady=8)

        self.z_travel = ttk.Entry(zp_frame, width=12, justify="right")
        self.z_travel.insert(0, str(cfg.get("z_travel", 0)))
        self.p_rest = ttk.Entry(zp_frame, width=12, justify="right")
        self.p_rest.insert(0, str(cfg.get("pipette_rest", 0)))
        self.p_full = ttk.Entry(zp_frame, width=12, justify="right")
        self.p_full.insert(0, str(cfg.get("pipette_full", 2000)))

        rows = [
            ("Z travel (safe up):",     self.z_travel, "Z"),
            ("Pipette rest (0):",       self.p_rest,   "P"),
            ("Pipette full (pushed):",  self.p_full,   "P"),
        ]
        for r, (label, entry, axis) in enumerate(rows):
            ttk.Label(zp_frame, text=label).grid(row=r, column=0, sticky="w", **pad)
            entry.grid(row=r, column=1, sticky="w", **pad)
            ttk.Button(
                zp_frame,
                text=f"Capture from {axis}",
                command=lambda e=entry, a=axis: self._capture(e, a),
            ).grid(row=r, column=2, sticky="w", **pad)

        ttk.Label(
            zp_frame,
            text=(
                "Capture = read the machine's current tracked position for that axis.\n"
                "Jog the head to the desired position in Manual mode first, then press Capture here.\n"
                "You can also capture directly from Manual mode via the 'Capture' buttons there."
            ),
            foreground="gray",
        ).grid(row=len(rows), column=0, columnspan=3, sticky="w", **pad)

        # ---- Buttons ----
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
            self.cfg["z_travel"] = int(self.z_travel.get())
            self.cfg["pipette_rest"] = int(self.p_rest.get())
            self.cfg["pipette_full"] = int(self.p_full.get())
        except ValueError:
            messagebox.showerror("Invalid input", "All coordinate and calibration values must be integers.")
            return
        save_config(self.cfg)
        self.on_save()
        self.destroy()


# ============================================================
# MAIN APPLICATION
# ============================================================

class UCASApp:
    """Root controller: serial connection, mode switching, shared motion."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("UCAS Control Program V5")
        self.root.geometry("1150x800")

        self.ser: serial.Serial | None = None

        # Software-tracked absolute position (raw motor steps for X,Y,Z,P).
        # NOTE: this drifts if any move fails silently. Recalibrate the origin
        # at the start of each session before running auto mode.
        self.current_position = {"X": 0, "Y": 0, "Z": 0, "P": 0}

        self.cfg = load_config()

        # Fonts / styles
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
        settings_menu.add_command(label="Coordinates & calibration...", command=self._open_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="Export config...", command=self._export_config)
        settings_menu.add_command(label="Import config...", command=self._import_config)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.root.config(menu=menubar)

    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, on_save=self._on_settings_saved,
                       current_position=self.current_position)

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
        ttk.Button(bar, text="Connect", command=self.connect, style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Disconnect", command=self.disconnect, style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Ping", command=self.ping, style="Big.TButton", width=7).pack(side="left", padx=4)
        self.status = ttk.Label(bar, text="Disconnected", style="Status.TLabel", foreground="gray")
        self.status.pack(side="left", padx=15)

    def _build_mode_switch(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(frame, text="Mode:", style="Big.TLabel").pack(side="left", padx=(0, 10))
        self.mode_var = tk.StringVar(value="manual")
        ttk.Radiobutton(frame, text="Manual", variable=self.mode_var,
                        value="manual", command=lambda: self._show_mode("manual")).pack(side="left", padx=4)
        ttk.Radiobutton(frame, text="Automatic", variable=self.mode_var,
                        value="auto", command=lambda: self._show_mode("auto")).pack(side="left", padx=4)

    def _build_mode_container(self):
        self.mode_container = ttk.Frame(self.root)
        self.mode_container.pack(fill="both", expand=True, padx=10, pady=6)
        self.manual_frame = ManualModeFrame(self.mode_container, self)
        self.auto_frame = AutoModeFrame(self.mode_container, self)

    def _show_mode(self, mode: str):
        for f in (self.manual_frame, self.auto_frame):
            f.pack_forget()
        if mode == "manual":
            self.manual_frame.pack(fill="both", expand=True)
            self.manual_frame.refresh_position_display()
        else:
            self.auto_frame.pack(fill="both", expand=True)
            self.auto_frame.refresh_from_config()

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

    # ========================================================
    # MOTION
    # ========================================================
    def send_motion(self, cmd: str) -> bool:
        """Send one motion command, wait for <ok>, update tracked position."""
        if not self.ser or not self.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return False
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
    # High-level helpers used by BOTH modes to move to an absolute
    # tracked step position on a single axis.
    # --------------------------------------------------------
    def move_axis_absolute(self, axis: str, target_step: int) -> bool:
        """Move a single axis to the given absolute (tracked) step position."""
        delta = target_step - self.current_position[axis]
        if delta == 0:
            return True
        return self.send_motion(f"M {axis}{delta}")

    def move_xy_absolute(self, target_x_step: int, target_y_step: int) -> bool:
        """Move X and Y simultaneously to absolute step positions."""
        dx = target_x_step - self.current_position["X"]
        dy = target_y_step - self.current_position["Y"]
        if dx == 0 and dy == 0:
            return True
        parts = []
        if dx != 0:
            parts.append(f"X{dx}")
        if dy != 0:
            parts.append(f"Y{dy}")
        return self.send_motion("M " + " ".join(parts))


# ============================================================
# MANUAL MODE
# ============================================================

class ManualModeFrame(ttk.Frame):
    """Jog + sequence + capture-current-position buttons for calibration."""

    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app
        self.sequence: list[str] = []
        self.step_size = tk.IntVar(value=1000)

        pad = {"padx": 8, "pady": 6}

        # ---- Step size ----
        row = 0
        ttk.Label(self, text="Step size:", style="Big.TLabel").grid(row=row, column=0, **pad)
        ttk.Entry(self, textvariable=self.step_size, font=app.big_font, width=10
                  ).grid(row=row, column=1, **pad)
        ttk.Label(self, text="1 – 10000 steps", style="Normal.TLabel"
                  ).grid(row=row, column=2, columnspan=2, sticky="w")

        # ---- Axis jog buttons + capture buttons ----
        row = 1
        for axis in ["X", "Y", "Z", "P"]:
            ttk.Label(self, text=f"{axis} axis", style="Big.TLabel").grid(row=row, column=0, **pad)
            ttk.Button(self, text="←  −", command=lambda a=axis: self.add_move(a, -1),
                       style="Big.TButton", width=8).grid(row=row, column=1, **pad)
            ttk.Button(self, text="+  →", command=lambda a=axis: self.add_move(a, 1),
                       style="Big.TButton", width=8).grid(row=row, column=2, **pad)

            # Capture buttons: only meaningful for Z and P calibration
            if axis == "Z":
                ttk.Button(self, text="Capture as Z_travel",
                           command=self._capture_z_travel, width=22
                           ).grid(row=row, column=3, sticky="w", **pad)
            elif axis == "P":
                cap_frame = ttk.Frame(self)
                cap_frame.grid(row=row, column=3, sticky="w", **pad)
                ttk.Button(cap_frame, text="Capture as Pipette rest",
                           command=self._capture_p_rest, width=22
                           ).pack(side="left", padx=(0, 4))
                ttk.Button(cap_frame, text="Capture as Pipette full",
                           command=self._capture_p_full, width=22
                           ).pack(side="left")
            row += 1

        # ---- Live position readout ----
        pos_frame = ttk.LabelFrame(self, text="Tracked position (raw motor steps)")
        pos_frame.grid(row=row, column=0, columnspan=4, sticky="ew", padx=8, pady=6)
        row += 1
        self.pos_labels: dict[str, ttk.Label] = {}
        for i, axis in enumerate(("X", "Y", "Z", "P")):
            ttk.Label(pos_frame, text=f"{axis}:", style="Big.TLabel"
                      ).grid(row=0, column=i * 2, padx=6, pady=4)
            lbl = ttk.Label(pos_frame, text="0", style="Big.TLabel", foreground="blue", width=8)
            lbl.grid(row=0, column=i * 2 + 1, padx=6, pady=4, sticky="w")
            self.pos_labels[axis] = lbl
        ttk.Button(pos_frame, text="Zero all (set current as origin)",
                   command=self._zero_all_axes, style="Big.TButton"
                   ).grid(row=0, column=8, padx=10)

        # ---- Stored sequence ----
        ttk.Label(self, text="Stored sequence:", style="Big.TLabel").grid(
            row=row, column=0, sticky="nw", **pad)
        self.sequence_box = tk.Text(self, height=10, width=60, font=("Consolas", 13))
        self.sequence_box.grid(row=row, column=1, columnspan=4, **pad)
        self.sequence_box.tag_configure("running", background="yellow")
        row += 1

        # ---- Sequence controls ----
        ttk.Button(self, text="START", command=self.run_sequence, style="Big.TButton", width=12
                   ).grid(row=row, column=1, **pad)
        ttk.Button(self, text="Run Last", command=self.run_last, style="Big.TButton", width=10
                   ).grid(row=row, column=2, **pad)
        ttk.Button(self, text="Clear", command=self.clear_sequence, style="Big.TButton", width=10
                   ).grid(row=row, column=3, **pad)

    # --------------------------------------------------------
    def refresh_position_display(self):
        for axis, lbl in self.pos_labels.items():
            lbl.config(text=str(self.app.current_position[axis]))

    def _zero_all_axes(self):
        for axis in ("X", "Y", "Z", "P"):
            self.app.current_position[axis] = 0
        self.refresh_position_display()
        self.app.status.config(text="Origin set at current position", foreground="green")

    def _capture_z_travel(self):
        self.app.cfg["z_travel"] = self.app.current_position["Z"]
        save_config(self.app.cfg)
        messagebox.showinfo("Captured", f"Z_travel = {self.app.cfg['z_travel']} (steps)")

    def _capture_p_rest(self):
        self.app.cfg["pipette_rest"] = self.app.current_position["P"]
        save_config(self.app.cfg)
        messagebox.showinfo("Captured", f"Pipette rest = {self.app.cfg['pipette_rest']} (steps)")

    def _capture_p_full(self):
        self.app.cfg["pipette_full"] = self.app.current_position["P"]
        save_config(self.app.cfg)
        messagebox.showinfo("Captured", f"Pipette full = {self.app.cfg['pipette_full']} (steps)")

    # --------------------------------------------------------
    def get_step_size(self):
        try:
            v = int(self.step_size.get())
            if v < 1 or v > 10000:
                raise ValueError
            return v
        except Exception:
            messagebox.showerror("Invalid step size", "Step size must be between 1 and 10000.")
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

        self.app.status.config(text="Running sequence...", foreground="blue")
        self.app.root.update()

        for i, cmd in enumerate(self.sequence):
            self.sequence_box.tag_remove("running", "1.0", tk.END)
            ln = i + 1
            self.sequence_box.tag_add("running", f"{ln}.0", f"{ln}.end")
            self.sequence_box.see(f"{ln}.0")
            self.app.status.config(text=f"Running {i+1}/{len(self.sequence)}: <{cmd}>",
                                   foreground="blue")
            self.app.root.update()

            if not self.app.send_motion(cmd):
                self.app.status.config(text=f"STOPPED at {i+1}: <{cmd}>", foreground="red")
                messagebox.showerror("Sequence stopped",
                                     f"Command {i+1} failed:\n\n<{cmd}>\n\nNo <ok>.")
                return
            time.sleep(DELAY)

        self.sequence_box.tag_remove("running", "1.0", tk.END)
        self.app.status.config(text="Sequence complete", foreground="green")

    def run_last(self):
        if not self.sequence:
            messagebox.showinfo("No command", "No command stored yet.")
            return
        cmd = self.sequence[-1]
        self.app.status.config(text=f"Running: <{cmd}>", foreground="blue")
        self.app.root.update()
        ok = self.app.send_motion(cmd)
        self.app.status.config(text="Last command complete" if ok else "Last command failed",
                               foreground="green" if ok else "red")


# ============================================================
# AUTOMATIC MODE
# ============================================================

class AutoModeFrame(ttk.Frame):
    """Target hex -> recipe -> pipette-cycle plan -> execute."""

    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app

        self.target_hex_var = tk.StringVar()
        self.multiplier_var = tk.StringVar(value="1")   # scale-up factor for the recipe
        self.current_recipe = None
        self.current_action_list: list[dict] = []

        pad = {"padx": 8, "pady": 6}

        # ---- Left column: input controls ----
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        ttk.Label(left, text="Target color (hex):", style="Big.TLabel"
                  ).grid(row=0, column=0, sticky="w", **pad)
        self.hex_entry = ttk.Entry(left, textvariable=self.target_hex_var,
                                   font=app.big_font, width=12)
        self.hex_entry.grid(row=0, column=1, sticky="w", **pad)
        self.hex_entry.bind("<KeyRelease>", lambda e: self._refresh_swatch())

        ttk.Label(left, text="Recipe multiplier ×:", style="Big.TLabel"
                  ).grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(left, textvariable=self.multiplier_var, font=app.big_font, width=12
                  ).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(left, text="e.g. 2× a 3:1 recipe = 6:2 cycles",
                  style="Normal.TLabel", foreground="gray"
                  ).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        ttk.Label(left, text="Target preview:", style="Normal.TLabel"
                  ).grid(row=3, column=0, sticky="w", **pad)
        self.swatch = tk.Canvas(left, width=140, height=60, bg="#ffffff",
                                highlightthickness=1, highlightbackground="black")
        self.swatch.grid(row=3, column=1, sticky="w", **pad)

        ttk.Button(left, text="Confirm target", command=self._on_confirm,
                   style="Big.TButton", width=16
                   ).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        self.start_btn = ttk.Button(left, text="START mixing", command=self._on_start,
                                    style="Big.TButton", width=16, state="disabled")
        self.start_btn.grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        # ---- Right column: recipe + action list ----
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

        if not MIXER_AVAILABLE:
            self.recipe_box.insert(tk.END,
                f"⚠  mixer.py could not be imported.\n"
                f"Automatic mode is unavailable until fixed:\n\n"
                f"  {MIXER_IMPORT_ERROR}\n\nManual mode still works.")
            self.hex_entry.configure(state="disabled")
            self.start_btn.configure(state="disabled")

    # --------------------------------------------------------
    def refresh_from_config(self):
        pass  # nothing derived yet

    def _refresh_swatch(self):
        text = self.target_hex_var.get().strip()
        if is_valid_hex(text):
            self.swatch.config(bg=normalize_hex(text))
        else:
            self.swatch.config(bg="#ffffff")

    # --------------------------------------------------------
    def _on_confirm(self):
        if not MIXER_AVAILABLE:
            messagebox.showerror("Mixer unavailable",
                                 f"mixer.py could not be imported:\n\n{MIXER_IMPORT_ERROR}")
            return

        text = self.target_hex_var.get().strip()
        if not is_valid_hex(text):
            messagebox.showwarning(
                "Invalid hex code",
                "Please enter a valid 6-digit hex color code, e.g. #FF8B38 or FF8B38."
            )
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

        target = normalize_hex(text)
        self.target_hex_var.set(target)
        self._refresh_swatch()

        recipe = find_best_recipe(target)
        if recipe is None:
            messagebox.showerror("Recipe error", "Could not compute a recipe.")
            return
        self.current_recipe = recipe

        # Check coordinates are set for all involved vials
        missing = []
        for color_name in recipe.colors:
            c = self.app.cfg["coordinates"].get(color_name, {})
            if c.get("X", 0) == 0 and c.get("Y", 0) == 0 and c.get("Z_bottom", 0) == 0:
                missing.append(color_name)
        tgt = self.app.cfg["coordinates"].get("target", {})
        if tgt.get("X", 0) == 0 and tgt.get("Y", 0) == 0 and tgt.get("Z_bottom", 0) == 0:
            missing.append("target")
        if missing:
            if not messagebox.askyesno(
                "Coordinates look unset",
                "These vials appear to have no coordinates:\n\n"
                + ", ".join(missing) + "\n\nContinue anyway?"
            ):
                return

        # Show recipe
        fractions = recipe_to_fractions(recipe)
        total_cycles = sum(recipe.drops) * multiplier
        self.recipe_box.delete("1.0", tk.END)
        self.recipe_box.insert(tk.END, f"Target        : {target}\n")
        self.recipe_box.insert(tk.END, f"Multiplier    : ×{multiplier}\n")
        self.recipe_box.insert(tk.END, f"Total cycles  : {total_cycles} pipette actions\n\n")
        for key, (color, frac) in fractions.items():
            i = int(key.split()[1]) - 1
            cycles = recipe.drops[i] * multiplier
            self.recipe_box.insert(tk.END,
                f"{key}: {color:<10s} fraction={frac:.3f}  cycles={cycles}\n")

        # Build action plan
        self.current_action_list = self._build_action_plan(recipe, multiplier)
        self._refresh_action_box()
        self.start_btn.configure(state="normal")

    # --------------------------------------------------------
    def _build_action_plan(self, recipe, multiplier: int) -> list[dict]:
        """One aspirate + one dispense per pipette cycle.
        For a 3:1 recipe × multiplier 1 that's 3 cycles from color 1 then 1 cycle from color 2.

        Each cycle is:
            aspirate: XY to source -> Z down to source Z_bottom
                      -> P: rest -> full -> rest
                      -> Z up to z_travel
            dispense: XY to target -> Z down to target Z_bottom
                      -> P: rest -> full
                      -> Z up to z_travel
                      -> P: full -> rest
        """
        actions: list[dict] = []
        tgt = self.app.cfg["coordinates"]["target"]

        for color_idx, (color, base_drops) in enumerate(zip(recipe.colors, recipe.drops)):
            cycles = base_drops * multiplier
            src = self.app.cfg["coordinates"].get(color, {"X": 0, "Y": 0, "Z_bottom": 0})
            for c in range(cycles):
                cycle_label = f"[color {color_idx+1}: {color}, cycle {c+1}/{cycles}]"
                actions.append({
                    "kind": "aspirate",
                    "text": f"{cycle_label} Aspirate from {color}",
                    "src": src,
                })
                actions.append({
                    "kind": "dispense",
                    "text": f"{cycle_label} Dispense into target",
                    "tgt": tgt,
                })
        return actions

    def _refresh_action_box(self):
        self.action_box.delete("1.0", tk.END)
        for i, act in enumerate(self.current_action_list, start=1):
            self.action_box.insert(tk.END, f"{i:03d}: {act['text']}\n")

    # --------------------------------------------------------
    def _on_start(self):
        if not self.current_action_list:
            messagebox.showinfo("No plan", "Confirm a target first.")
            return
        if not self.app.ser or not self.app.ser.is_open:
            messagebox.showwarning("Not connected", "Connect to Arduino first.")
            return

        self.start_btn.configure(state="disabled")
        self.app.status.config(text="AUTO: running mix", foreground="blue")
        self.app.root.update()

        try:
            for i, act in enumerate(self.current_action_list):
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
                    messagebox.showerror(
                        "Auto mix stopped",
                        f"Action {i+1} failed:\n\n{act['text']}"
                    )
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
                            f"Mixing finished.\n\nTarget: {self.target_hex_var.get()}")
        self.start_btn.configure(state="normal")

    # --------------------------------------------------------
    # Physical procedures — one call per motion command so status
    # updates and the tracked position stay in sync.
    # --------------------------------------------------------
    def _coord_to_step_x(self, coord: int) -> int:
        return int(coord) * int(self.app.cfg["steps_per_coord_X"])

    def _coord_to_step_y(self, coord: int) -> int:
        return int(coord) * int(self.app.cfg["steps_per_coord_Y"])

    def _do_aspirate(self, src: dict) -> bool:
        """Aspirate procedure per point 3:
           a) XY move to source
           b) Z down to source Z_bottom
           c) P: rest -> full -> rest  (plunger cycle draws liquid)
           d) Z up to z_travel
        """
        x_step = self._coord_to_step_x(src["X"])
        y_step = self._coord_to_step_y(src["Y"])
        z_bottom = int(src["Z_bottom"])
        z_travel = int(self.app.cfg["z_travel"])
        p_rest = int(self.app.cfg["pipette_rest"])
        p_full = int(self.app.cfg["pipette_full"])

        # a) XY
        if not self.app.move_xy_absolute(x_step, y_step):
            return False
        time.sleep(DELAY)

        # b) Z down
        if not self.app.move_axis_absolute("Z", z_bottom):
            return False
        time.sleep(DELAY)

        # c) Plunger cycle: rest -> full -> rest
        if not self.app.move_axis_absolute("P", p_full):
            return False
        time.sleep(DELAY)
        if not self.app.move_axis_absolute("P", p_rest):
            return False
        time.sleep(DELAY)

        # d) Z up to travel
        if not self.app.move_axis_absolute("Z", z_travel):
            return False
        time.sleep(DELAY)
        return True

    def _do_dispense(self, tgt: dict) -> bool:
        """Dispense procedure per point 4:
           a) XY move to target
           b) Z down to target Z_bottom
           c) Push plunger to full  (dispense while tip is IN the liquid)
           d) Z up to z_travel  (with plunger still at full — prevents suck-back)
           e) Release plunger to rest  (now that tip is out of liquid)
        """
        x_step = self._coord_to_step_x(tgt["X"])
        y_step = self._coord_to_step_y(tgt["Y"])
        z_bottom = int(tgt["Z_bottom"])
        z_travel = int(self.app.cfg["z_travel"])
        p_rest = int(self.app.cfg["pipette_rest"])
        p_full = int(self.app.cfg["pipette_full"])

        # a) XY
        if not self.app.move_xy_absolute(x_step, y_step):
            return False
        time.sleep(DELAY)

        # b) Z down
        if not self.app.move_axis_absolute("Z", z_bottom):
            return False
        time.sleep(DELAY)

        # c) Push plunger to full (dispense)
        if not self.app.move_axis_absolute("P", p_full):
            return False
        time.sleep(DELAY)

        # d) Z up to travel — plunger stays at full
        if not self.app.move_axis_absolute("Z", z_travel):
            return False
        time.sleep(DELAY)

        # e) Release plunger to rest — tip is now clear of liquid
        if not self.app.move_axis_absolute("P", p_rest):
            return False
        time.sleep(DELAY)
        return True


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = UCASApp(root)
    root.mainloop()