"""
UCAS Control Program V4
=======================
Adds an "Automatic" mode alongside the original manual sequence builder.

Manual mode (unchanged behavior): user builds a jog sequence by axis, runs it.

Automatic mode:
  1. User inputs vial coordinates (base colors + target) via Settings menu.
  2. User types a target hex color on the main auto screen.
  3. Program validates hex, shows the target color swatch.
  4. User sets target volume (mL).
  5. Program computes recipe (using `mixer.suggest_recipes`) and shows:
       - the recipe (colors + fractions)
       - the planned action list (each pipette / move step)
  6. User clicks "Start Mixing". The machine visits each source vial,
     aspirates the fraction of target volume, dispenses into the target
     vial, and finally announces completion.

Dependencies:
  - pyserial
  - mixer.py (from earlier work — must expose BASE_COLORS and suggest_recipes)
"""

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

# Delay between completed commands (seconds)
DELAY = 0.3

# Maximum time allowed for ONE motion command to finish.
# Firmware sends <ok> only AFTER motion is complete.
COMMAND_TIMEOUT = 120

# Persistent config file for vial coordinates + calibration.
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "ucas_config.json",
)

# ------------------------------------------------------------
# Mixer import. Guarded so a missing mixer.py doesn't kill the
# whole app — auto mode simply won't be usable until it's fixed.
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
    """True if s looks like a 6-digit hex color, with or without leading '#'."""
    return bool(HEX_RE.match(s.strip()))


def normalize_hex(s: str) -> str:
    """Return the hex string in canonical '#rrggbb' lowercase form."""
    s = s.strip().lstrip("#").lower()
    return f"#{s}"


def default_config() -> dict:
    """Fresh config with zeroed coordinates for every known color + target."""
    coords = {}
    color_names = list(BASE_COLORS.keys()) if BASE_COLORS else [
        "yellow", "orange", "pink", "red", "green", "blue", "violet", "black"
    ]
    for name in color_names:
        coords[name] = {"X": 0, "Y": 0, "Z": 0}
    coords["target"] = {"X": 0, "Y": 0, "Z": 0}

    return {
        "coordinates": coords,
        # Steps required for the pipette motor per mL of liquid.
        # User MUST calibrate this against their actual pipette.
        "steps_per_ml": 1000,
        # Z clearance height (steps) — how far to lift Z between vials
        # so the tip clears vial rims when travelling.
        "z_travel_offset": 2000,
    }


def load_config() -> dict:
    """Load config from disk; if missing/corrupt, return defaults."""
    if not os.path.exists(CONFIG_PATH):
        return default_config()
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        # Merge in any new default keys the file might be missing
        base = default_config()
        for k, v in base.items():
            cfg.setdefault(k, v)
        # Ensure every base color has an entry
        for name in (BASE_COLORS or {}):
            cfg["coordinates"].setdefault(name, {"X": 0, "Y": 0, "Z": 0})
        cfg["coordinates"].setdefault("target", {"X": 0, "Y": 0, "Z": 0})
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
    """Escalate from n=2 colors upward until ΔE < delta_e_ok, capped at max_colors.

    Returns a Recipe from mixer.suggest_recipes, or None if the mixer is not
    available. Mirrors the logic in the user's test.py.
    """
    if not MIXER_AVAILABLE:
        return None

    best = None
    n = 2
    while n <= max_colors:
        result = suggest_recipes(
            target_hex,
            n_max=n,
            total_drops_max=total_drops_max,
            top_k=1,
        )
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
    """Convert a mixer Recipe into the {color n: [name, fraction]} dict
    used by the user's test.py."""
    out = defaultdict(dict)
    total = sum(recipe.drops)
    for i, (c, d) in enumerate(zip(recipe.colors, recipe.drops)):
        out[f"color {i+1}"] = [c, d / total]
    return out


# ============================================================
# COORDINATES / SETTINGS DIALOG
# ============================================================

class SettingsDialog(tk.Toplevel):
    """Modal window for editing vial coordinates and calibration."""

    def __init__(self, parent, cfg: dict, on_save):
        super().__init__(parent)
        self.title("Settings — Vial coordinates & calibration")
        self.transient(parent)
        self.grab_set()
        self.cfg = cfg
        self.on_save = on_save

        self.entries: dict[str, dict[str, tk.Entry]] = {}

        pad = {"padx": 6, "pady": 3}

        # ---- Coordinates table ----
        coord_frame = ttk.LabelFrame(self, text="Vial coordinates (absolute steps)")
        coord_frame.pack(fill="both", expand=True, padx=10, pady=8)

        ttk.Label(coord_frame, text="Vial").grid(row=0, column=0, **pad)
        for col, axis in enumerate(("X", "Y", "Z"), start=1):
            ttk.Label(coord_frame, text=axis, width=10, anchor="center").grid(
                row=0, column=col, **pad
            )

        for row_i, name in enumerate(cfg["coordinates"].keys(), start=1):
            ttk.Label(coord_frame, text=name).grid(row=row_i, column=0, sticky="w", **pad)
            self.entries[name] = {}
            for col_i, axis in enumerate(("X", "Y", "Z"), start=1):
                e = ttk.Entry(coord_frame, width=10, justify="right")
                e.insert(0, str(cfg["coordinates"][name].get(axis, 0)))
                e.grid(row=row_i, column=col_i, **pad)
                self.entries[name][axis] = e

        # ---- Calibration ----
        cal_frame = ttk.LabelFrame(self, text="Calibration")
        cal_frame.pack(fill="x", padx=10, pady=8)

        ttk.Label(cal_frame, text="Pipette steps per mL:").grid(
            row=0, column=0, sticky="w", **pad
        )
        self.steps_per_ml_entry = ttk.Entry(cal_frame, width=12, justify="right")
        self.steps_per_ml_entry.insert(0, str(cfg.get("steps_per_ml", 1000)))
        self.steps_per_ml_entry.grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(cal_frame, text="Z travel offset (steps):").grid(
            row=1, column=0, sticky="w", **pad
        )
        self.z_travel_entry = ttk.Entry(cal_frame, width=12, justify="right")
        self.z_travel_entry.insert(0, str(cfg.get("z_travel_offset", 2000)))
        self.z_travel_entry.grid(row=1, column=1, sticky="w", **pad)

        ttk.Label(
            cal_frame,
            text=(
                "Coordinates are ABSOLUTE, relative to the machine's origin.\n"
                "Before running auto mode, jog the machine to (0,0,0) and press\n"
                "'Set current position as origin' on the auto panel."
            ),
            foreground="gray",
        ).grid(row=2, column=0, columnspan=3, sticky="w", **pad)

        # ---- Buttons ----
        btn_frame = ttk.Frame(self)
        btn_frame.pack(fill="x", padx=10, pady=8)
        ttk.Button(btn_frame, text="Save", command=self._save).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="right", padx=4)

    def _save(self):
        try:
            for name, ax_entries in self.entries.items():
                for axis, e in ax_entries.items():
                    self.cfg["coordinates"][name][axis] = int(e.get())
            self.cfg["steps_per_ml"] = int(self.steps_per_ml_entry.get())
            self.cfg["z_travel_offset"] = int(self.z_travel_entry.get())
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
    """Root controller. Owns the serial connection, mode switching,
    and both mode frames (manual + automatic)."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("UCAS Control Program V4")
        self.root.geometry("1100x760")

        self.ser: serial.Serial | None = None

        # Software-tracked absolute position (steps).
        # The firmware itself only knows relative moves, so we track here.
        self.current_position = {"X": 0, "Y": 0, "Z": 0, "P": 0}

        # Config (coordinates + calibration)
        self.cfg = load_config()

        # Fonts
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

        # Start in manual mode by default
        self.mode_var.set("manual")
        self._show_mode("manual")

    # --------------------------------------------------------
    # Menu bar
    # --------------------------------------------------------
    def _build_menu(self):
        menubar = tk.Menu(self.root)
        settings_menu = tk.Menu(menubar, tearoff=0)
        settings_menu.add_command(label="Vial coordinates & calibration...", command=self._open_settings)
        settings_menu.add_separator()
        settings_menu.add_command(label="Export config...", command=self._export_config)
        settings_menu.add_command(label="Import config...", command=self._import_config)
        menubar.add_cascade(label="Settings", menu=settings_menu)
        self.root.config(menu=menubar)

    def _open_settings(self):
        SettingsDialog(self.root, self.cfg, on_save=self._on_settings_saved)

    def _on_settings_saved(self):
        # Give auto frame a chance to refresh anything derived from cfg
        if hasattr(self, "auto_frame") and self.auto_frame is not None:
            self.auto_frame.refresh_from_config()

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
    # Top bar: connect / disconnect / status
    # --------------------------------------------------------
    def _build_top_bar(self):
        bar = ttk.Frame(self.root)
        bar.pack(fill="x", padx=10, pady=8)

        ttk.Button(bar, text="Connect", command=self.connect, style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Disconnect", command=self.disconnect, style="Big.TButton", width=11).pack(side="left", padx=4)
        ttk.Button(bar, text="Ping", command=self.ping, style="Big.TButton", width=7).pack(side="left", padx=4)

        self.status = ttk.Label(bar, text="Disconnected", style="Status.TLabel", foreground="gray")
        self.status.pack(side="left", padx=15)

    # --------------------------------------------------------
    # Mode switch radiobuttons
    # --------------------------------------------------------
    def _build_mode_switch(self):
        frame = ttk.Frame(self.root)
        frame.pack(fill="x", padx=10, pady=(0, 6))

        ttk.Label(frame, text="Mode:", style="Big.TLabel").pack(side="left", padx=(0, 10))

        self.mode_var = tk.StringVar(value="manual")
        ttk.Radiobutton(frame, text="Manual", variable=self.mode_var,
                        value="manual", command=lambda: self._show_mode("manual")).pack(side="left", padx=4)
        ttk.Radiobutton(frame, text="Automatic", variable=self.mode_var,
                        value="auto", command=lambda: self._show_mode("auto")).pack(side="left", padx=4)

    # --------------------------------------------------------
    # Container that holds either the manual or auto frame
    # --------------------------------------------------------
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
        else:
            self.auto_frame.pack(fill="both", expand=True)
            self.auto_frame.refresh_from_config()

    # ========================================================
    # SERIAL CONNECTION
    # ========================================================
    def connect(self):
        try:
            if self.ser and self.ser.is_open:
                self.ser.close()
            self.ser = serial.Serial(PORT, BAUD, timeout=5)
            time.sleep(2)   # Arduino Due may reset when serial opens
            self.ser.reset_input_buffer()
            self.status.config(text=f"Connected to {PORT}", foreground="green")
            print("Connected to", PORT)
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
        print("Serial disconnected")

    # ========================================================
    # LOW-LEVEL MOTION (shared by both modes)
    # ========================================================
    def send_motion(self, cmd: str) -> bool:
        """Send a single motion command like 'M X1000' and wait for <ok>.
        Also updates our software position tracker if command parses as M."""
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
                # Update software position from the deltas in the command
                self._apply_move_to_position(cmd)
            return ok
        except Exception as e:
            print("Serial error:", e)
            self.status.config(text="Serial communication error", foreground="red")
            messagebox.showerror("Serial error", str(e))
            return False

    def _apply_move_to_position(self, cmd: str):
        """Parse an 'M X1000 Y-500' style command and update current_position."""
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
                        print("Arduino error:", line)
                        return False
                time.sleep(0.01)
                self.root.update()
            except Exception as e:
                print("Serial read error:", e)
                return False
        print(f"Timeout: Arduino did not return <ok> within {COMMAND_TIMEOUT}s.")
        return False

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


# ============================================================
# MANUAL MODE (behavior preserved from V3)
# ============================================================

class ManualModeFrame(ttk.Frame):
    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app
        self.sequence: list[str] = []
        self.step_size = tk.IntVar(value=1000)

        pad = {"padx": 8, "pady": 6}

        # --- Step size ---
        row = 0
        ttk.Label(self, text="Step size:", style="Big.TLabel").grid(row=row, column=0, **pad)
        ttk.Entry(self, textvariable=self.step_size, font=app.big_font, width=10).grid(row=row, column=1, **pad)
        ttk.Label(self, text="1 – 10000 steps", style="Normal.TLabel").grid(row=row, column=2, columnspan=2, sticky="w")

        # --- Axis buttons ---
        row = 1
        for axis in ["X", "Y", "Z", "P"]:
            ttk.Label(self, text=f"{axis} axis", style="Big.TLabel").grid(row=row, column=0, **pad)
            ttk.Button(self, text="←  −", command=lambda a=axis: self.add_move(a, -1),
                       style="Big.TButton", width=8).grid(row=row, column=1, **pad)
            ttk.Button(self, text="+  →", command=lambda a=axis: self.add_move(a, 1),
                       style="Big.TButton", width=8).grid(row=row, column=2, **pad)
            row += 1

        # --- Stored sequence ---
        ttk.Label(self, text="Stored sequence:", style="Big.TLabel").grid(row=row, column=0, sticky="nw", **pad)
        self.sequence_box = tk.Text(self, height=12, width=65, font=("Consolas", 13))
        self.sequence_box.grid(row=row, column=1, columnspan=4, **pad)
        self.sequence_box.tag_configure("running", background="yellow")
        row += 1

        # --- Sequence controls ---
        ttk.Button(self, text="START", command=self.run_sequence, style="Big.TButton", width=12
                   ).grid(row=row, column=1, **pad)
        ttk.Button(self, text="Run Last", command=self.run_last, style="Big.TButton", width=10
                   ).grid(row=row, column=2, **pad)
        ttk.Button(self, text="Clear", command=self.clear_sequence, style="Big.TButton", width=10
                   ).grid(row=row, column=3, **pad)

    def get_step_size(self):
        try:
            value = int(self.step_size.get())
            if value < 1 or value > 10000:
                raise ValueError
            return value
        except Exception:
            messagebox.showerror("Invalid step size", "Step size must be between 1 and 10000.")
            return None

    def add_move(self, axis, direction):
        step = self.get_step_size()
        if step is None:
            return
        value = step * direction
        self.sequence.append(f"M {axis}{value}")
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

        for index, cmd in enumerate(self.sequence):
            self.sequence_box.tag_remove("running", "1.0", tk.END)
            line_number = index + 1
            self.sequence_box.tag_add("running", f"{line_number}.0", f"{line_number}.end")
            self.sequence_box.see(f"{line_number}.0")
            self.app.status.config(text=f"Running {index+1}/{len(self.sequence)}: <{cmd}>", foreground="blue")
            self.app.root.update()

            if not self.app.send_motion(cmd):
                self.app.status.config(text=f"STOPPED at command {index+1}: <{cmd}>", foreground="red")
                messagebox.showerror("Sequence stopped",
                                     f"Command {index+1} failed:\n\n<{cmd}>\n\nArduino did not return <ok>.")
                return
            time.sleep(DELAY)

        self.sequence_box.tag_remove("running", "1.0", tk.END)
        self.app.status.config(text="Sequence complete", foreground="green")
        print("\n--- UCAS SEQUENCE COMPLETE ---\n")

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
    def __init__(self, parent, app: UCASApp):
        super().__init__(parent)
        self.app = app

        self.target_hex_var = tk.StringVar()
        self.volume_var = tk.StringVar(value="1.0")
        self.current_recipe = None       # mixer.Recipe
        self.current_action_list: list[dict] = []

        pad = {"padx": 8, "pady": 6}

        # ---- Left column: input controls ----
        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nw", padx=8, pady=8)

        ttk.Label(left, text="Target color (hex):", style="Big.TLabel").grid(row=0, column=0, sticky="w", **pad)
        self.hex_entry = ttk.Entry(left, textvariable=self.target_hex_var, font=app.big_font, width=12)
        self.hex_entry.grid(row=0, column=1, sticky="w", **pad)
        self.hex_entry.bind("<KeyRelease>", lambda e: self._refresh_swatch())

        ttk.Label(left, text="Target volume (mL):", style="Big.TLabel").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(left, textvariable=self.volume_var, font=app.big_font, width=12
                  ).grid(row=1, column=1, sticky="w", **pad)

        # Target color swatch
        ttk.Label(left, text="Target preview:", style="Normal.TLabel").grid(row=2, column=0, sticky="w", **pad)
        self.swatch = tk.Canvas(left, width=140, height=60, bg="#ffffff", highlightthickness=1,
                                highlightbackground="black")
        self.swatch.grid(row=2, column=1, sticky="w", **pad)

        # Confirm + Start buttons
        ttk.Button(left, text="Confirm target", command=self._on_confirm, style="Big.TButton", width=16
                   ).grid(row=3, column=0, columnspan=2, sticky="w", **pad)
        self.start_btn = ttk.Button(left, text="START mixing", command=self._on_start,
                                    style="Big.TButton", width=16, state="disabled")
        self.start_btn.grid(row=4, column=0, columnspan=2, sticky="w", **pad)

        ttk.Button(left, text="Set current position as origin",
                   command=self._reset_origin, style="Big.TButton", width=28
                   ).grid(row=5, column=0, columnspan=2, sticky="w", **pad)

        self.origin_label = ttk.Label(left, text="Origin not set", style="Normal.TLabel", foreground="orange")
        self.origin_label.grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        # ---- Right column: recipe + action list ----
        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=8, pady=8)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        ttk.Label(right, text="Computed recipe:", style="Big.TLabel").pack(anchor="w")
        self.recipe_box = tk.Text(right, height=6, width=60, font=("Consolas", 12))
        self.recipe_box.pack(fill="x", pady=4)

        ttk.Label(right, text="Planned actions (highlight = currently running):",
                  style="Big.TLabel").pack(anchor="w", pady=(8, 0))
        self.action_box = tk.Text(right, height=15, width=60, font=("Consolas", 12))
        self.action_box.pack(fill="both", expand=True, pady=4)
        self.action_box.tag_configure("running", background="yellow")
        self.action_box.tag_configure("done", foreground="gray")

        # If the mixer isn't importable, block auto mode with a clear message
        if not MIXER_AVAILABLE:
            self.recipe_box.insert(tk.END,
                f"⚠  mixer.py could not be imported.\n"
                f"Automatic mode is unavailable until the following is fixed:\n\n"
                f"  {MIXER_IMPORT_ERROR}\n\n"
                f"Manual mode still works normally.")
            self.hex_entry.configure(state="disabled")
            self.start_btn.configure(state="disabled")

    # --------------------------------------------------------
    def refresh_from_config(self):
        """Called when settings change — nothing derived here yet, but
        provided as a hook."""
        pass

    def _reset_origin(self):
        """Declare the current machine position to be (0,0,0). Absolute
        coordinates from the settings dialog are relative to this."""
        for axis in ("X", "Y", "Z", "P"):
            self.app.current_position[axis] = 0
        self.origin_label.config(text="Origin set at machine's current position",
                                 foreground="green")

    def _refresh_swatch(self):
        """Live update of the color preview as the user types."""
        text = self.target_hex_var.get().strip()
        if is_valid_hex(text):
            self.swatch.config(bg=normalize_hex(text))
        else:
            self.swatch.config(bg="#ffffff")

    # --------------------------------------------------------
    def _on_confirm(self):
        """Validate hex + compute recipe + populate the plan."""
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
            volume_ml = float(self.volume_var.get())
            if volume_ml <= 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning("Invalid volume", "Target volume must be a positive number of mL.")
            self.start_btn.configure(state="disabled")
            return

        target = normalize_hex(text)
        self.target_hex_var.set(target)
        self._refresh_swatch()

        # Compute recipe (test.py logic, wrapped in helper)
        recipe = find_best_recipe(target)
        if recipe is None:
            messagebox.showerror("Recipe error", "Could not compute a recipe.")
            return
        self.current_recipe = recipe

        # Check every color in the recipe has coordinates set (non-zero)
        missing_coords = []
        for color_name in recipe.colors:
            coords = self.app.cfg["coordinates"].get(color_name, {})
            if all(coords.get(a, 0) == 0 for a in ("X", "Y", "Z")):
                missing_coords.append(color_name)
        tgt_coords = self.app.cfg["coordinates"].get("target", {})
        if all(tgt_coords.get(a, 0) == 0 for a in ("X", "Y", "Z")):
            missing_coords.append("target")
        if missing_coords:
            if not messagebox.askyesno(
                "Coordinates look unset",
                "The following vials appear to have no coordinates set:\n\n"
                + ", ".join(missing_coords)
                + "\n\nContinue anyway?"
            ):
                return

        # Show recipe
        fractions = recipe_to_fractions(recipe)
        self.recipe_box.delete("1.0", tk.END)
        self.recipe_box.insert(tk.END, f"Target       : {target}\n")
        self.recipe_box.insert(tk.END, f"Total volume : {volume_ml} mL\n\n")
        for key, (color, frac) in fractions.items():
            vol = frac * volume_ml
            self.recipe_box.insert(tk.END, f"{key}: {color:<10s} fraction={frac:.3f}  volume={vol:.3f} mL\n")

        # Build the action plan
        self.current_action_list = self._build_action_plan(recipe, volume_ml)
        self._refresh_action_box()
        self.start_btn.configure(state="normal")

    # --------------------------------------------------------
    def _build_action_plan(self, recipe, volume_ml: float) -> list[dict]:
        """Produce a list of high-level actions with human-readable text and
        the corresponding motion commands. Every entry is:

            {"text": str, "commands": [str, ...]}
        """
        cfg = self.app.cfg
        steps_per_ml = cfg["steps_per_ml"]
        z_offset = cfg["z_travel_offset"]
        total_drops = sum(recipe.drops)

        actions: list[dict] = []

        def move_absolute(target_x, target_y, target_z, description) -> dict:
            """Emit motion commands to go from current tracked pos to (x,y,z)
            using Z-safe routing: lift Z, move XY, lower Z."""
            # NB: commands here are strings; we ACTUALLY compute deltas at
            # execution time to account for prior actions in the same plan.
            return {
                "text": description,
                "target": (target_x, target_y, target_z),
                "kind": "move_absolute",
                "z_offset": z_offset,
            }

        def pipette_action(steps: int, description: str) -> dict:
            direction = "aspirate" if steps > 0 else "dispense"
            return {
                "text": description,
                "kind": "pipette",
                "commands": [f"M P{steps}"],
            }

        tgt = cfg["coordinates"]["target"]

        for i, (color, drops) in enumerate(zip(recipe.colors, recipe.drops)):
            fraction = drops / total_drops
            vol_ml = fraction * volume_ml
            steps = int(round(vol_ml * steps_per_ml))
            src = cfg["coordinates"].get(color, {"X": 0, "Y": 0, "Z": 0})

            actions.append(move_absolute(
                src["X"], src["Y"], src["Z"],
                f"[{i+1}] Move to {color} vial  ({src['X']}, {src['Y']}, {src['Z']})"
            ))
            actions.append(pipette_action(
                steps,
                f"[{i+1}] Aspirate {vol_ml:.3f} mL of {color}  (P +{steps} steps)"
            ))
            actions.append(move_absolute(
                tgt["X"], tgt["Y"], tgt["Z"],
                f"[{i+1}] Move to target vial  ({tgt['X']}, {tgt['Y']}, {tgt['Z']})"
            ))
            actions.append(pipette_action(
                -steps,
                f"[{i+1}] Dispense into target  (P -{steps} steps)"
            ))

        return actions

    def _refresh_action_box(self):
        self.action_box.delete("1.0", tk.END)
        for i, act in enumerate(self.current_action_list, start=1):
            self.action_box.insert(tk.END, f"{i:02d}: {act['text']}\n")

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
                # Highlight current action
                self.action_box.tag_remove("running", "1.0", tk.END)
                line = i + 1
                self.action_box.tag_add("running", f"{line}.0", f"{line}.end")
                self.action_box.see(f"{line}.0")
                self.app.status.config(text=f"AUTO {i+1}/{len(self.current_action_list)}: {act['text']}",
                                       foreground="blue")
                self.app.root.update()

                # Execute
                commands = self._materialize_commands(act)
                for cmd in commands:
                    if not self.app.send_motion(cmd):
                        messagebox.showerror(
                            "Auto mix stopped",
                            f"Action {i+1} failed:\n\n{act['text']}\n\n"
                            f"Command <{cmd}> did not return <ok>."
                        )
                        self.app.status.config(text=f"AUTO STOPPED at action {i+1}", foreground="red")
                        self.start_btn.configure(state="normal")
                        return
                    time.sleep(DELAY)

                # Mark done
                self.action_box.tag_remove("running", f"{line}.0", f"{line}.end")
                self.action_box.tag_add("done", f"{line}.0", f"{line}.end")
        except Exception as e:
            messagebox.showerror("Auto mix error", str(e))
            self.app.status.config(text="AUTO error", foreground="red")
            self.start_btn.configure(state="normal")
            return

        self.app.status.config(text="AUTO complete", foreground="green")
        messagebox.showinfo("Mixing complete",
                            f"Mixing finished.\n\nTarget: {self.target_hex_var.get()}\n"
                            f"Volume: {self.volume_var.get()} mL")
        self.start_btn.configure(state="normal")

    def _materialize_commands(self, action: dict) -> list[str]:
        """Convert an abstract action to concrete relative-motion commands
        based on the current software position. For 'move_absolute' we
        route Z-up then XY then Z-down for safety."""
        if action["kind"] == "pipette":
            return action["commands"]

        if action["kind"] == "move_absolute":
            tx, ty, tz = action["target"]
            cur = self.app.current_position
            z_offset = action["z_offset"]

            cmds = []
            # 1) Lift Z to safe travel height (relative to current Z)
            #    We lift by z_offset above the current Z.
            if z_offset != 0:
                cmds.append(f"M Z{z_offset}")
            # 2) XY move to target
            dx = tx - cur["X"]
            dy = ty - cur["Y"]
            xy_cmd_parts = []
            if dx != 0:
                xy_cmd_parts.append(f"X{dx}")
            if dy != 0:
                xy_cmd_parts.append(f"Y{dy}")
            if xy_cmd_parts:
                cmds.append("M " + " ".join(xy_cmd_parts))
            # 3) Lower Z to target Z (from current Z + z_offset)
            #    After step 1 our tracked Z will be cur.Z + z_offset,
            #    so the delta to reach tz is: tz - (cur.Z + z_offset)
            new_z_after_lift = cur["Z"] + z_offset
            dz = tz - new_z_after_lift
            if dz != 0:
                cmds.append(f"M Z{dz}")
            return cmds

        return []


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    root = tk.Tk()
    app = UCASApp(root)
    root.mainloop()