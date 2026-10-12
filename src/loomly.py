"""Windows scroll-dimming overlay with a blue corner indicator."""

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass, replace
import json
import os
from pathlib import Path
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import uuid

from PIL import Image, ImageEnhance, ImageOps, ImageTk

TRIGGER_COUNT = 3
STREAK_TIMEOUT = 2.0
DIM_STEP = 0.2
MAX_DIM = 0.95
RECOVER_DELAY = 3.0
TICK_MS = 50
INDICATOR_SIZE = 50
BACKGROUND_BRIGHTNESS = 0.15
AUTOSCROLL_DEAD_ZONE = 15  # Vertical pixels around the middle-click origin.
AUTOSCROLL_DISTANCE = 200  # Pixels beyond the dead zone for full dimming rate.
CURSOR_CONFIRM_TIMEOUT = 0.8
CURSOR_LOST_TIMEOUT = 0.2


@dataclass(frozen=True)
class Settings:
    trigger_count: int = TRIGGER_COUNT
    dim_step: float = DIM_STEP
    max_dim: float = MAX_DIM
    background: str = ""
    background_name: str = ""
    background_scale: float = 1.0
    fade_duration: float = 5.0
    image_intensity: float = BACKGROUND_BRIGHTNESS
    recover_delay: float = RECOVER_DELAY
    audio_enabled: bool = True
    audio_reduction: float = 1.0

    def validate(self):
        if type(self.trigger_count) is not int or not 1 <= self.trigger_count <= 100:
            raise ValueError("จำนวนครั้งต้องเป็นจำนวนเต็มระหว่าง 1–100")
        for value, label in ((self.dim_step, "ระดับที่หรี่เพิ่ม"), (self.max_dim, "ระดับสูงสุด")):
            if type(value) not in (int, float) or not 0.01 <= value <= MAX_DIM:
                raise ValueError(f"{label}ต้องอยู่ระหว่าง 1–95%")
        if self.dim_step > self.max_dim:
            raise ValueError("ระดับที่หรี่เพิ่มต้องไม่เกินระดับสูงสุด")
        if type(self.background_scale) not in (int, float) or not 0.1 <= self.background_scale <= 2.0:
            raise ValueError("ขนาดภาพต้องอยู่ระหว่าง 10–200%")
        for value, lower, upper, label in (
            (self.fade_duration, 0, 60, "เวลาภาพจาง (วินาที)"),
            (self.image_intensity, 0, 1, "ความเข้มภาพ (0–100%)"),
            (self.recover_delay, 0, 60, "เวลารอก่อนคืนความสว่าง (วินาที)"),
            (self.audio_reduction, 0, 1, "ระดับลดเสียง (0–100%)"),
        ):
            if type(value) not in (int, float) or not lower <= value <= upper:
                raise ValueError(f"{label}ต้องอยู่ระหว่าง {lower}–{upper}")
        if type(self.audio_enabled) is not bool:
            raise ValueError("ค่าการลดเสียงไม่ถูกต้อง")
        if not isinstance(self.background, str) or not isinstance(self.background_name, str):
            raise ValueError("ข้อมูลภาพไม่ถูกต้อง")
        if self.background and (
            Path(self.background).name != self.background
            or not self.background.startswith("background-")
            or not self.background.endswith(".png")
        ):
            raise ValueError("ชื่อไฟล์ภาพไม่ถูกต้อง")
        return self


@dataclass(frozen=True)
class Profiles:
    active_mode: str = "normal"
    normal: Settings = Settings()
    custom: Settings = Settings()

    @property
    def current(self):
        return getattr(self, self.active_mode)

    def validate(self):
        if self.active_mode not in ("normal", "custom"):
            raise ValueError("โหมดไม่ถูกต้อง")
        self.normal.validate()
        self.custom.validate()
        if self.normal != Settings():
            raise ValueError("Normal ใช้ค่ามาตรฐานตายตัว")
        return self


class SettingsStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "settings.json"

    def load(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if "version" in payload:
                return self.load_profiles().current
            return Settings(**payload).validate()
        except (OSError, ValueError, TypeError):
            return Settings()

    def load_profiles(self):
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if "version" in payload:
                if payload["version"] != 2:
                    raise ValueError("Unsupported settings version")
                return Profiles(active_mode=payload["active_mode"],
                                normal=Settings(**payload["normal"]),
                                custom=Settings(**payload["custom"])).validate()
            legacy = Settings(**payload).validate()
            # Existing user choices belong to Custom; Normal always uses factory defaults.
            return Profiles(active_mode="custom", custom=legacy).validate()
        except (OSError, ValueError, TypeError, KeyError):
            return Profiles()

    def save_profiles(self, profiles, images=None, replaced=()):
        profiles.validate()
        images = images or {}
        if any(mode not in ("normal", "custom") for mode in replaced):
            raise ValueError("โหมดไม่ถูกต้อง")
        self.directory.mkdir(parents=True, exist_ok=True)
        created = []
        temporary = self.directory / f"settings-{uuid.uuid4().hex}.tmp"
        try:
            for mode in replaced:
                image = images.get(mode)
                if mode == "normal" and image is not None:
                    raise ValueError("Normal ใช้พื้นหลังดำเท่านั้น")
                name = f"background-{uuid.uuid4().hex}.png" if image is not None else ""
                settings = replace(getattr(profiles, mode), background=name,
                                   background_name=getattr(profiles, mode).background_name if name else "")
                profiles = replace(profiles, **{mode: settings})
                if name:
                    path = self.directory / name
                    created.append(path)
                    image.save(path, format="PNG")
            profiles.validate()
            payload = {"version": 2, **asdict(profiles)}
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temporary, self.path)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        # Only prune this app's generated images after the new configuration is durable.
        referenced = {profiles.normal.background, profiles.custom.background}
        for path in self.directory.glob("background-*.png"):
            if path.name not in referenced:
                try:
                    path.unlink()
                except OSError:
                    pass
        return profiles

    def save(self, settings, image=None, replace_image=False):
        settings.validate()
        self.directory.mkdir(parents=True, exist_ok=True)
        new_path = None
        if replace_image:
            name = f"background-{uuid.uuid4().hex}.png" if image is not None else ""
            settings = replace(settings, background=name,
                               background_name=settings.background_name if name else "")
            if name:
                new_path = self.directory / name
        temporary = self.directory / f"settings-{uuid.uuid4().hex}.tmp"
        try:
            if new_path:
                image.save(new_path, format="PNG")
            temporary.write_text(json.dumps(asdict(settings), ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            os.replace(temporary, self.path)
        except Exception:
            if new_path:
                new_path.unlink(missing_ok=True)
            raise
        finally:
            temporary.unlink(missing_ok=True)
        return settings


def load_background(path):
    """Keep the original dimensions so later resizing does not lose image content."""
    with Image.open(path) as source:
        image = ImageOps.exif_transpose(source).convert("RGBA")
        base = Image.new("RGBA", image.size, "black")
        base.alpha_composite(image)
        return base.convert("RGB")


def render_background(image, size, scale=1.0):
    """Center a proportional image; 100% covers the screen, smaller sizes expose black."""
    Settings(background_scale=scale).validate()
    width, height = size
    factor = max(width / image.width, height / image.height) * scale
    # Render directly into the screen-sized output, including for panoramic images.
    return image.transform(size, Image.Transform.AFFINE,
                           (1 / factor, 0, (image.width - width / factor) / 2,
                            0, 1 / factor, (image.height - height / factor) / 2),
                           resample=Image.Resampling.BICUBIC, fillcolor="black")


def prepare_background(path, size, scale=1.0):
    return render_background(load_background(path), size, scale)


class DimState:
    def __init__(self, settings=None):
        self.lock = threading.Lock()
        self.direction = None
        self.count = 0
        self.last = 0.0
        self.dim = 0.0
        self.enabled = True
        self.suspended = False
        self.settings = (settings or Settings()).validate()
        self.middle_origin = None
        self.middle_y = None
        self.middle_pressed = False
        self.middle_dragged = False
        self.middle_updated = None
        self.middle_confirmed = False
        self.middle_started = None
        self.cursor_lost = None
        self.fade_start = None
        self.fade_level = 0.0

    def _cancel_middle(self):
        self.middle_origin = self.middle_y = self.middle_updated = None
        self.middle_pressed = self.middle_dragged = False
        self.middle_confirmed = False
        self.middle_started = self.cursor_lost = None

    def needs_cursor_check(self):
        with self.lock:
            return self.middle_origin is not None

    def observe_cursor(self, is_scroll, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.middle_origin is None:
                return
            if is_scroll is True:
                if not self.middle_confirmed:
                    self.middle_updated = now
                self.middle_confirmed = True
                self.cursor_lost = None
            else:
                # Unknown/unreadable cursors must not allow continued dimming.
                self.middle_confirmed = False
                self.middle_updated = now
                if self.cursor_lost is None:
                    self.cursor_lost = now
                timeout = (CURSOR_CONFIRM_TIMEOUT if self.middle_started is not None
                           else CURSOR_LOST_TIMEOUT)
                since = self.middle_started if self.middle_started is not None else self.cursor_lost
                if now - since >= timeout:
                    self._cancel_middle()
            if self.middle_confirmed:
                self.middle_started = None

    def _advance_middle(self, now):
        if self.middle_origin is None:
            return
        elapsed = max(0.0, now - self.middle_updated)
        self.middle_updated = now
        distance = max(0, abs(self.middle_y - self.middle_origin) - AUTOSCROLL_DEAD_ZONE)
        if distance and self.middle_confirmed:
            speed = min(1.0, distance / AUTOSCROLL_DISTANCE)
            self.dim = min(self.settings.max_dim,
                           self.dim + self.settings.dim_step * speed * elapsed)
            self.last = now
            self.fade_start = None

    def cancel_middle(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self._advance_middle(now)
            self._cancel_middle()

    def middle_click(self, y, pressed, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.enabled or self.suspended:
                return
            self._advance_middle(now)
            if pressed:
                if self.middle_origin is not None:
                    self._cancel_middle()
                else:
                    self.middle_origin = self.middle_y = y
                    self.middle_pressed = True
                    self.middle_dragged = False
                    self.middle_updated = now
                    self.middle_started = now
                    self.direction, self.count = None, 0
            elif self.middle_pressed:
                # A stationary click latches; releasing a middle-button drag ends it.
                self.middle_dragged |= abs(y - self.middle_origin) > AUTOSCROLL_DEAD_ZONE
                self.middle_pressed = False
                self.middle_y = y
                if self.middle_dragged:
                    self._cancel_middle()

    def move(self, y, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if self.middle_origin is not None:
                # Accumulate using the previous position, never the new one retroactively.
                self._advance_middle(now)
                self.middle_y = y
                if self.middle_pressed and abs(y - self.middle_origin) > AUTOSCROLL_DEAD_ZONE:
                    self.middle_dragged = True

    def indicator_color(self):
        with self.lock:
            if not self.enabled:
                return "#748A99"
            return "#E53935" if self.middle_origin is not None else "#159DE8"

    def register(self, direction, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            if not self.enabled or self.suspended:
                return
            self._advance_middle(now)
            self._cancel_middle()
            if direction != self.direction or now - self.last > STREAK_TIMEOUT:
                self.direction, self.count = direction, 0
            self.last = now
            self.fade_start = None
            if self.count >= self.settings.trigger_count:
                self.dim = min(self.settings.max_dim, self.dim + self.settings.dim_step)
                self.count = 0
            else:
                self.count += 1

    def recover(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self._advance_middle(now)
            if self.dim > 0 and now - self.last >= self.settings.recover_delay:
                if self.fade_start is None:
                    self.fade_start, self.fade_level = self.last + self.settings.recover_delay, self.dim
                progress = ((now - self.fade_start) / self.settings.fade_duration
                            if self.settings.fade_duration else 1.0)
                self.dim = self.fade_level * max(0.0, 1.0 - progress)
            return self.dim

    def toggle(self):
        with self.lock:
            self.enabled = not self.enabled
            self.fade_start = None
            self._cancel_middle()
            self.direction, self.count, self.last, self.dim = None, 0, 0.0, 0.0
            return self.enabled

    def suspend(self, suspended):
        with self.lock:
            self.suspended = suspended
            self.fade_start = None
            self._cancel_middle()
            self.direction, self.count, self.last, self.dim = None, 0, 0.0, 0.0

    def configure(self, settings):
        settings.validate()
        with self.lock:
            self.settings = settings
            self.fade_start = None
            self._cancel_middle()
            self.direction, self.count, self.last, self.dim = None, 0, 0.0, 0.0


def configure_window(window, click_through=False):
    window.update_idletasks()
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.GetWindowLongW.restype = wintypes.LONG
    user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.LONG]
    style = user32.GetWindowLongW(hwnd, -20)
    # Layered, tool window (hide from taskbar), and no activation.
    style |= 0x80000 | 0x80 | 0x08000000
    if click_through:
        style |= 0x20
    user32.SetWindowLongW(hwnd, -20, style)
    return hwnd


class LoomlyApp:
    def __init__(self, data_dir=None, audio=None):
        from pynput import keyboard, mouse

        base_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        self.store = SettingsStore(data_dir if data_dir is not None else base_dir / "data")
        self.profiles = self.store.load_profiles()
        self.settings = self.profiles.current
        from audio_control import AudioDucker
        self.audio = audio if audio is not None else AudioDucker()
        self.state = DimState(self.settings)
        from cursor_detect import ScrollCursorDetector
        self.cursor_detector = ScrollCursorDetector()
        self.settings_window = None
        self.overlay_photo = None
        self.exit_requested = threading.Event()
        self.closed = False
        self.tick_job = None
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Loomly")
        self.root.overrideredirect(True)
        self.root.configure(bg="black")
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        width, height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        self.screen_size = (width, height)
        self.overlay_canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, bd=0)
        self.overlay_canvas.pack(fill="both", expand=True)
        self.refresh_background()
        self.root.geometry(f"{width}x{height}+0+0")
        self.root.deiconify()
        self.overlay_hwnd = configure_window(self.root, click_through=True)

        self.indicator = tk.Toplevel(self.root)
        self.indicator.overrideredirect(True)
        self.indicator.attributes("-topmost", True)
        self.indicator.attributes("-transparentcolor", "#ff00ff")
        size = INDICATOR_SIZE
        self.indicator.geometry(f"{size}x{size}+0+{height - size}")
        self.canvas = tk.Canvas(self.indicator, width=size, height=size,
                                bg="#ff00ff", highlightthickness=0, bd=0)
        self.canvas.pack()
        self.triangle = self.canvas.create_polygon(
            0, 0, size, size, 0, size, fill="#159DE8", outline="")
        self.indicator_hwnd = configure_window(self.indicator)
        self.canvas.bind("<Button-1>", self.open_settings)
        self.canvas.bind("<Button-3>", self.show_menu)
        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="Pause dimming", command=self.toggle)
        self.menu.add_command(label="Settings…", command=self.open_settings)
        self.menu.add_separator()
        self.menu.add_command(label="Exit (F12)", command=self.close)

        def on_scroll(x, y, dx, dy):
            if dy:
                self.state.register("up" if dy > 0 else "down")
            elif dx:
                self.state.cancel_middle()

        def on_press(key):
            if key == keyboard.Key.up:
                self.state.register("up")
            elif key == keyboard.Key.down:
                self.state.register("down")
            elif key == keyboard.Key.f12:
                # Tk calls belong exclusively to the main thread.
                self.exit_requested.set()
            elif key == keyboard.Key.esc:
                self.state.cancel_middle()

        def on_click(x, y, button, pressed):
            if button == mouse.Button.middle:
                self.state.middle_click(y, pressed)
            elif pressed:
                self.state.cancel_middle()

        def on_move(x, y):
            self.state.move(y)

        self.listeners = [mouse.Listener(on_scroll=on_scroll, on_click=on_click, on_move=on_move),
                          keyboard.Listener(on_press=on_press)]
        for listener in self.listeners:
            listener.start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.tick()

    def refresh_background(self):
        self.overlay_canvas.delete("all")
        self.overlay_photo = None
        if self.settings.background:
            try:
                image = prepare_background(self.store.directory / self.settings.background, self.screen_size,
                                           self.settings.background_scale)
                image = ImageEnhance.Brightness(image).enhance(self.settings.image_intensity)
                self.overlay_photo = ImageTk.PhotoImage(image, master=self.root)
                self.overlay_canvas.create_image(0, 0, anchor="nw", image=self.overlay_photo)
            except (OSError, ValueError, Image.DecompressionBombError):
                # Missing or damaged custom images fall back to the black overlay.
                self.overlay_photo = None

    def apply_settings(self, settings, image=None, replace_image=False):
        mode = self.profiles.active_mode
        profiles = replace(self.profiles, **{mode: settings})
        self.apply_profiles(profiles, images={mode: image}, replaced=(mode,) if replace_image else ())

    def apply_profiles(self, profiles, images=None, replaced=()):
        saved = self.store.save_profiles(profiles, images=images, replaced=replaced)
        self.audio.restore()
        self.profiles = saved
        self.settings = saved.current
        self.state.configure(self.settings)
        self.refresh_background()
        self.root.attributes("-alpha", 0.0)

    def open_settings(self, event=None):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        self.state.suspend(True)
        self.audio.restore()
        self.root.attributes("-alpha", 0.0)
        self.settings_window = SettingsDialog(self)

    def finish_settings(self):
        self.state.suspend(False)
        self.settings_window = None

    def toggle(self, event=None):
        enabled = self.state.toggle()
        self.audio.restore()
        self.root.attributes("-alpha", 0.0)
        self.canvas.itemconfigure(self.triangle, fill=self.state.indicator_color())
        self.menu.entryconfigure(0, label="Pause dimming" if enabled else "Resume dimming")

    def show_menu(self, event):
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def tick(self):
        if self.closed:
            return
        if self.tick_job is not None:
            self.root.after_cancel(self.tick_job)
            self.tick_job = None
        if self.exit_requested.is_set():
            self.close()
            return
        if self.state.needs_cursor_check():
            self.state.observe_cursor(self.cursor_detector.is_scroll())
        dim = self.state.recover()
        self.root.attributes("-alpha", dim)
        self.audio.update(dim, self.settings.audio_enabled, self.settings.audio_reduction)
        if self.settings_window is not None:
            self.settings_window.audio_status.configure(text=self.audio.error)
        self.canvas.itemconfigure(self.triangle, fill=self.state.indicator_color())
        # Keep the triangle above the dimming overlay without taking keyboard focus.
        user32 = ctypes.windll.user32
        user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND,
                                       ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
        user32.SetWindowPos(self.indicator_hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x13)
        self.tick_job = self.root.after(TICK_MS, self.tick)

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.audio.restore()
        if self.tick_job is not None:
            self.root.after_cancel(self.tick_job)
            self.tick_job = None
        for listener in self.listeners:
            listener.stop()
        self.root.destroy()

    def run(self):
        try:
            self.root.mainloop()
        finally:
            self.close()


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("Loomly — ตั้งค่า")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda event: self.cancel())
        self.loading = True
        self.edit_mode = app.profiles.active_mode
        self.drafts = {"normal": app.profiles.normal, "custom": app.profiles.custom}
        self.images = {"normal": None, "custom": None}
        self.changed_images = set()
        self.mode_var = tk.StringVar(value=self.edit_mode)
        self.count_var = tk.StringVar()
        self.step_var = tk.StringVar()
        self.max_var = tk.StringVar()
        self.delay_var = tk.StringVar()
        self.fade_var = tk.StringVar()
        self.scale_var = tk.StringVar()
        self.intensity_var = tk.StringVar()
        self.audio_var = tk.BooleanVar()
        self.audio_reduction_var = tk.StringVar()
        self.pending_image = None
        self.image_name = ""
        self.preview_photo = None
        self.image_warning = ""
        if app.profiles.custom.background:
            try:
                self.images["custom"] = load_background(app.store.directory / app.profiles.custom.background)
            except (OSError, ValueError, Image.DecompressionBombError):
                self.image_warning = "ไม่พบภาพที่บันทึกไว้ — ใช้พื้นหลังดำ"

        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="Loomly", font=("Segoe UI", 17, "bold")).pack(anchor="w")
        ttk.Label(body, text="พักการหรี่และคืนเสียงระหว่างตั้งค่า", font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 10))
        modes = ttk.Frame(body)
        modes.pack(fill="x")
        for value, label in (("normal", "Normal"), ("custom", "Custom")):
            ttk.Radiobutton(modes, text=label, value=value, variable=self.mode_var,
                            command=self.switch_mode).pack(side="left", padx=(0, 20))
        self.mode_hint = ttk.Label(body, wraplength=440)
        self.mode_hint.pack(anchor="w", pady=(6, 12))
        self.normal_summary = ttk.LabelFrame(body, text="ค่ามาตรฐาน", padding=14)
        ttk.Label(self.normal_summary,
                  text="พื้นหลังดำ • เริ่มหรี่ครั้งที่ 4\nหรี่เพิ่ม 20% • ความทึบสูงสุด 95%\nรอ 3 วินาที • คืนความสว่างใน 5 วินาที\nลดเสียง Windows ตามความทึบ",
                  justify="left", wraplength=410).pack(anchor="w")
        self.notebook = ttk.Notebook(body, width=440)
        self.notebook.pack(fill="both", expand=True)
        dim_tab = ttk.Frame(self.notebook, padding=12)
        sound_tab = ttk.Frame(self.notebook, padding=12)
        self.image_tab = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(dim_tab, text="การหรี่")
        self.notebook.add(sound_tab, text="เสียง")
        self.notebook.add(self.image_tab, text="ภาพพื้นหลัง")
        self.add_fields(dim_tab, (
            ("จำนวนครั้งก่อนเริ่มหรี่", self.count_var, 1, 100, 1),
            ("หรี่เพิ่มแต่ละครั้ง (%)", self.step_var, 1, 95, 1),
            ("ความทึบสูงสุด (%)", self.max_var, 1, 95, 1),
            ("รอก่อนคืนความสว่าง (วินาที)", self.delay_var, 0, 60, 0.5),
            ("เวลาคืนความสว่าง (วินาที)", self.fade_var, 0, 60, 0.5),
        ))
        self.threshold_label = ttk.Label(dim_tab, wraplength=410)
        self.threshold_label.pack(anchor="w", pady=(10, 4))
        ttk.Label(dim_tab, text="เวลาคืนความสว่าง 0 = คืนทันทีหลังช่วงรอ", wraplength=410).pack(anchor="w")
        ttk.Checkbutton(sound_tab, text="ลดเสียง Windows ตามความทึบ", variable=self.audio_var,
                        command=self.update_audio_controls).pack(anchor="w", pady=(0, 12))
        row = ttk.Frame(sound_tab)
        row.pack(fill="x")
        ttk.Label(row, text="ระดับลดเสียง (%)").pack(side="left")
        self.audio_spin = ttk.Spinbox(row, from_=0, to=100, increment=5,
                                     textvariable=self.audio_reduction_var, width=9)
        self.audio_spin.pack(side="right")
        ttk.Label(sound_tab, text="ระดับลดเสียง 100%: ความทึบ 50% จะลดเสียงลงครึ่งหนึ่ง\nคืนเสียงเมื่อจอสว่าง พัก หรือออกจากแอพ\nหากปรับเสียงเอง แอพจะพักการลดเสียงจนจอสว่าง",
                  wraplength=410, justify="left").pack(anchor="w", pady=(12, 0))
        self.audio_status = ttk.Label(sound_tab, text=app.audio.error, wraplength=410)
        self.audio_status.pack(anchor="w", pady=(8, 0))
        self.preview = tk.Canvas(self.image_tab, width=416, height=120,
                                 bg="#18242D", highlightthickness=0)
        self.preview.pack(fill="x", pady=(0, 6))
        self.name_label = ttk.Label(self.image_tab, wraplength=410)
        self.name_label.pack(anchor="w")
        image_buttons = ttk.Frame(self.image_tab)
        image_buttons.pack(fill="x", pady=8)
        ttk.Button(image_buttons, text="เลือกภาพ…", command=self.choose_image).pack(side="left")
        ttk.Button(image_buttons, text="นำภาพออก", command=self.remove_image).pack(side="left", padx=8)
        self.add_fields(self.image_tab, (
            ("ขนาดภาพ (%)", self.scale_var, 10, 200, 5),
            ("ความเข้มภาพ (%)", self.intensity_var, 0, 100, 5),
        ))
        ttk.Label(self.image_tab, text="ขนาด 100% = ครอบเต็มจอ • ความเข้ม 0% = ดำ", wraplength=410).pack(anchor="w", pady=(8, 0))
        buttons = self.buttons = ttk.Frame(body)
        buttons.pack(fill="x", pady=(14, 0))
        self.pause_button = ttk.Button(buttons, command=self.toggle_enabled)
        self.pause_button.pack(side="left")
        self.update_pause_label()
        ttk.Button(buttons, text="บันทึกและใช้โหมดนี้", command=self.save).pack(side="right")
        ttk.Button(buttons, text="ยกเลิก", command=self.cancel).pack(side="right", padx=8)
        self.count_var.trace_add("write", self.update_threshold_hint)
        self.scale_var.trace_add("write", self.update_preview)
        self.intensity_var.trace_add("write", self.update_preview)
        self.load_mode()
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{max(0, (app.screen_size[0] - width) // 2)}+{max(0, (app.screen_size[1] - height) // 2)}")
        self.grab_set()
        self.lift()
        self.focus_force()

    @staticmethod
    def add_fields(parent, fields):
        for label, variable, lower, upper, increment in fields:
            row = ttk.Frame(parent)
            row.pack(fill="x", pady=5)
            ttk.Label(row, text=label).pack(side="left")
            ttk.Spinbox(row, from_=lower, to=upper, increment=increment,
                        textvariable=variable, width=9).pack(side="right", padx=(16, 0))

    def load_mode(self):
        self.loading = True
        settings = self.drafts[self.edit_mode]
        for variable, value in (
            (self.count_var, settings.trigger_count), (self.step_var, settings.dim_step * 100),
            (self.max_var, settings.max_dim * 100), (self.delay_var, settings.recover_delay),
            (self.fade_var, settings.fade_duration), (self.scale_var, settings.background_scale * 100),
            (self.intensity_var, settings.image_intensity * 100),
            (self.audio_reduction_var, settings.audio_reduction * 100),
        ):
            variable.set(f"{value:g}")
        self.audio_var.set(settings.audio_enabled)
        self.pending_image = self.images[self.edit_mode]
        self.image_name = settings.background_name
        if self.edit_mode == "normal":
            self.notebook.select(0)
            self.notebook.hide(self.image_tab)
            self.notebook.pack_forget()
            self.normal_summary.pack(fill="x", before=self.buttons)
            self.mode_hint.configure(text="พร้อมใช้ทันที • ค่ามาตรฐานตายตัว")
        else:
            self.normal_summary.pack_forget()
            self.notebook.pack(fill="both", expand=True, before=self.buttons)
            self.notebook.add(self.image_tab, text="ภาพพื้นหลัง")
            self.mode_hint.configure(text="ปรับการหรี่ เวลา เสียง และภาพได้ • ไม่เปลี่ยนค่า Normal")
        self.loading = False
        self.update_threshold_hint()
        self.update_audio_controls()
        self.update_preview()

    def collect_settings(self):
        if self.edit_mode == "normal":
            return Settings()
        return replace(self.drafts[self.edit_mode],
                       trigger_count=int(self.count_var.get()),
                       dim_step=float(self.step_var.get()) / 100,
                       max_dim=float(self.max_var.get()) / 100,
                       background_name=self.image_name,
                       background_scale=float(self.scale_var.get()) / 100,
                       image_intensity=float(self.intensity_var.get()) / 100,
                       recover_delay=float(self.delay_var.get()),
                       fade_duration=float(self.fade_var.get()),
                       audio_enabled=self.audio_var.get(),
                       audio_reduction=float(self.audio_reduction_var.get()) / 100).validate()

    def switch_mode(self):
        target = self.mode_var.get()
        if target == self.edit_mode:
            return True
        try:
            self.drafts[self.edit_mode] = self.collect_settings()
        except ValueError as error:
            self.mode_var.set(self.edit_mode)
            messagebox.showerror("เปลี่ยนโหมดไม่ได้", str(error), parent=self)
            return False
        self.images[self.edit_mode] = self.pending_image
        self.edit_mode = target
        self.load_mode()
        self.update_idletasks()
        self.geometry("")
        return True

    def update_threshold_hint(self, *args):
        try:
            count = int(self.count_var.get())
            self.threshold_label.configure(text=f"เริ่มหรี่ในครั้งที่ {count + 1} เมื่อเลื่อนทิศเดียวกัน")
        except ValueError:
            self.threshold_label.configure(text="กรอกจำนวนครั้งเป็นจำนวนเต็ม 1–100")

    def update_audio_controls(self):
        self.audio_spin.configure(state="normal" if self.audio_var.get() else "disabled")

    def update_pause_label(self):
        self.pause_button.configure(text="พักการหรี่" if self.app.state.enabled else "เปิดการหรี่")

    def toggle_enabled(self):
        self.app.toggle()
        self.update_pause_label()

    def update_preview(self, *args):
        if self.loading:
            return
        try:
            scale = float(self.scale_var.get()) / 100
            intensity = float(self.intensity_var.get()) / 100
            Settings(background_scale=scale, image_intensity=intensity).validate()
        except ValueError:
            return
        self.preview.delete("all")
        self.preview_photo = None
        if self.pending_image is not None:
            width, height = self.app.screen_size
            factor = min(416 / width, 120 / height)
            preview_size = (max(1, round(width * factor)), max(1, round(height * factor)))
            image = render_background(self.pending_image, preview_size, scale)
            image = ImageEnhance.Brightness(image).enhance(intensity)
            self.preview_photo = ImageTk.PhotoImage(image, master=self)
            self.preview.create_image(208, 60, image=self.preview_photo)
        else:
            self.preview.create_text(208, 60, text="ไม่มีภาพ — ใช้พื้นหลังดำ", fill="white", font=("Segoe UI", 11))
        warning = self.image_warning if self.edit_mode == "custom" and self.pending_image is None else ""
        self.name_label.configure(text=warning or self.image_name or "ยังไม่ได้เลือกภาพ")

    def choose_image(self):
        path = filedialog.askopenfilename(parent=self, title="เลือกภาพพื้นหลัง", filetypes=[
            ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
            ("All files", "*.*"),
        ])
        if path:
            self.import_image(path)

    def import_image(self, path):
        if self.edit_mode != "custom":
            return False
        try:
            image = load_background(path)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            messagebox.showerror("เปิดภาพไม่ได้", f"กรุณาเลือกไฟล์ภาพที่รองรับ\n{error}", parent=self)
            return False
        self.pending_image = image
        self.image_name = Path(path).name
        self.image_warning = ""
        self.changed_images.add(self.edit_mode)
        self.update_preview()
        return True

    def remove_image(self):
        if self.edit_mode != "custom":
            return
        self.pending_image = None
        self.image_name = ""
        self.image_warning = ""
        self.changed_images.add(self.edit_mode)
        self.update_preview()

    def save(self):
        try:
            self.drafts[self.edit_mode] = self.collect_settings()
            self.images[self.edit_mode] = self.pending_image
            profiles = Profiles(active_mode=self.edit_mode, **self.drafts).validate()
            self.app.apply_profiles(profiles, images=self.images, replaced=self.changed_images)
        except (ValueError, OSError) as error:
            messagebox.showerror("บันทึกไม่ได้", str(error), parent=self)
            return False
        self.cancel()
        return True

    def cancel(self):
        self.grab_release()
        self.destroy()
        self.app.finish_settings()


def main():
    if sys.platform != "win32":
        raise SystemExit("Loomly requires Windows.")
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel32.CreateMutexW(None, False, "Local\\LoomlyCornerEye")
    if not mutex:
        raise ctypes.WinError()
    if kernel32.GetLastError() == 183:
        kernel32.CloseHandle(mutex)
        return
    try:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        app = LoomlyApp()
        if "--smoke-test" in sys.argv:
            if "--check-audio" in sys.argv:
                try:
                    from audio_control import default_endpoint
                    endpoint = default_endpoint()
                    before = endpoint.get_volume()
                    app.audio.update(0.1, True)
                    if app.audio.error or abs(endpoint.get_volume() - before * 0.9) > 0.01:
                        raise RuntimeError("Packaged audio control failed")
                finally:
                    app.audio.restore()
                if abs(endpoint.get_volume() - before) > 0.01:
                    raise RuntimeError("Packaged audio restoration failed")
            app.root.after(750, app.close)
        app.run()
    finally:
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
