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
        ):
            if type(value) not in (int, float) or not lower <= value <= upper:
                raise ValueError(f"{label}ต้องอยู่ระหว่าง {lower}–{upper}")
        if not isinstance(self.background, str) or not isinstance(self.background_name, str):
            raise ValueError("ข้อมูลภาพไม่ถูกต้อง")
        if self.background and (
            Path(self.background).name != self.background
            or not self.background.startswith("background-")
            or not self.background.endswith(".png")
        ):
            raise ValueError("ชื่อไฟล์ภาพไม่ถูกต้อง")
        return self


class SettingsStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.path = self.directory / "settings.json"

    def load(self):
        try:
            return Settings(**json.loads(self.path.read_text(encoding="utf-8"))).validate()
        except (OSError, ValueError, TypeError):
            return Settings()

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
            if self.dim > 0 and now - self.last > RECOVER_DELAY:
                if self.fade_start is None:
                    self.fade_start, self.fade_level = self.last + RECOVER_DELAY, self.dim
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
    def __init__(self, data_dir=None):
        from pynput import keyboard, mouse

        base_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[1]
        self.store = SettingsStore(data_dir if data_dir is not None else base_dir / "data")
        self.settings = self.store.load()
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
        saved = self.store.save(settings, image=image, replace_image=replace_image)
        self.settings = saved
        self.state.configure(saved)
        self.refresh_background()
        self.root.attributes("-alpha", 0.0)

    def open_settings(self, event=None):
        if self.settings_window is not None and self.settings_window.winfo_exists():
            self.settings_window.lift()
            self.settings_window.focus_force()
            return
        self.state.suspend(True)
        self.root.attributes("-alpha", 0.0)
        self.settings_window = SettingsDialog(self)

    def finish_settings(self):
        self.state.suspend(False)
        self.settings_window = None

    def toggle(self, event=None):
        enabled = self.state.toggle()
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
        self.root.attributes("-alpha", self.state.recover())
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
        self.count_var = tk.StringVar(value=str(app.settings.trigger_count))
        self.step_var = tk.StringVar(value=str(round(app.settings.dim_step * 100)))
        self.max_var = tk.StringVar(value=str(round(app.settings.max_dim * 100)))
        self.scale_var = tk.StringVar(value=f"{app.settings.background_scale * 100:g}")
        self.intensity_var = tk.StringVar(value=f"{app.settings.image_intensity * 100:g}")
        self.fade_var = tk.StringVar(value=f"{app.settings.fade_duration:g}")
        self.image_name = app.settings.background_name
        self.pending_image = None
        self.image_dirty = False
        self.preview_photo = None
        body = ttk.Frame(self, padding=16)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="ตั้งค่าการหรี่หน้าจอ", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(body, text="หน้าจอจะสว่างตามปกติระหว่างตั้งค่า", font=("Segoe UI", 10)).pack(anchor="w", pady=(2, 8))
        form = ttk.Frame(body)
        form.pack(fill="x")
        for row, (label, variable, upper) in enumerate((
            ("จำนวนครั้งก่อนเริ่มหรี่", self.count_var, 100),
            ("หรี่เพิ่มแต่ละครั้ง (%)", self.step_var, 95),
            ("ระดับการหรี่สูงสุด (%)", self.max_var, 95),
        )):
            ttk.Label(form, text=label, font=("Segoe UI", 11)).grid(row=row, column=0, sticky="w", pady=6)
            ttk.Spinbox(form, from_=1, to=upper, textvariable=variable,
                        width=10, font=("Segoe UI", 11)).grid(row=row, column=1, sticky="e", padx=(28, 0), pady=6)
        form.columnconfigure(0, weight=1)
        self.threshold_label = ttk.Label(body, font=("Segoe UI", 10))
        self.threshold_label.pack(anchor="w", pady=(4, 8))
        self.count_var.trace_add("write", self.update_threshold_hint)
        self.update_threshold_hint()
        ttk.Separator(body).pack(fill="x", pady=(0, 8))
        ttk.Label(body, text="ภาพพื้นหลังขณะหรี่", font=("Segoe UI", 12, "bold")).pack(anchor="w")
        self.preview = tk.Canvas(body, width=440, height=100, bg="#18242D", highlightthickness=0)
        self.preview.pack(fill="x", pady=(8, 6))
        self.name_label = ttk.Label(body, wraplength=440, font=("Segoe UI", 10))
        self.name_label.pack(anchor="w")
        image_buttons = ttk.Frame(body)
        image_buttons.pack(fill="x", pady=(8, 8))
        ttk.Button(image_buttons, text="นำเข้าภาพ…", command=self.choose_image).pack(side="left")
        ttk.Button(image_buttons, text="นำภาพออก", command=self.remove_image).pack(side="left", padx=8)
        image_size = ttk.Frame(body)
        image_size.pack(fill="x", pady=(0, 8))
        ttk.Label(image_size, text="ขนาดภาพ (%)", font=("Segoe UI", 11)).pack(side="left")
        ttk.Spinbox(image_size, from_=10, to=200, increment=5,
                    textvariable=self.scale_var, width=10).pack(side="right")
        self.scale_var.trace_add("write", self.update_preview)
        for label, variable, upper, increment in (
            ("ความเข้มภาพ (%)", self.intensity_var, 100, 5),
            ("เวลาภาพจาง (วินาที)", self.fade_var, 60, 0.5),
        ):
            row = ttk.Frame(body)
            row.pack(fill="x", pady=(0, 6))
            ttk.Label(row, text=label, font=("Segoe UI", 11)).pack(side="left")
            ttk.Spinbox(row, from_=0, to=upper, increment=increment,
                        textvariable=variable, width=10).pack(side="right")
        self.intensity_var.trace_add("write", self.update_preview)
        ttk.Label(body, text="ขนาด 100% = เต็มจอ • ความเข้ม 0% = ดำ, 100% = ภาพเดิม\nหลังหยุดเลื่อน 3 วินาทีจะจางตามเวลาที่ตั้ง • 0 วินาที = หายทันที",
                  font=("Segoe UI", 10), wraplength=440).pack(anchor="w", pady=(0, 8))
        buttons = ttk.Frame(body)
        buttons.pack(fill="x")
        self.pause_button = ttk.Button(buttons, command=self.toggle_enabled)
        self.pause_button.pack(side="left")
        self.update_pause_label()
        ttk.Button(buttons, text="บันทึก", command=self.save).pack(side="right")
        ttk.Button(buttons, text="ยกเลิก", command=self.cancel).pack(side="right", padx=8)
        if app.settings.background:
            try:
                self.pending_image = load_background(app.store.directory / app.settings.background)
            except (OSError, ValueError, Image.DecompressionBombError):
                self.image_name = "ไม่พบภาพที่บันทึกไว้ — จะใช้พื้นหลังดำ"
        self.update_preview()
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        self.geometry(f"+{max(0, (app.screen_size[0] - width) // 2)}+{max(0, (app.screen_size[1] - height) // 2)}")
        self.grab_set()
        self.lift()
        self.focus_force()

    def update_threshold_hint(self, *args):
        try:
            count = int(self.count_var.get())
            self.threshold_label.configure(text=f"เลื่อนทิศเดียวกันครบ {count} ครั้ง → เริ่มหรี่ในครั้งที่ {count + 1}")
        except ValueError:
            self.threshold_label.configure(text="กรอกจำนวนครั้งเป็นจำนวนเต็ม 1–100")

    def update_pause_label(self):
        self.pause_button.configure(text="พักการหรี่" if self.app.state.enabled else "เปิดการหรี่")

    def toggle_enabled(self):
        self.app.toggle()
        self.update_pause_label()

    def update_preview(self, *args):
        try:
            scale = float(self.scale_var.get()) / 100
            intensity = float(self.intensity_var.get()) / 100
            Settings(background_scale=scale, image_intensity=intensity).validate()
        except ValueError:
            return  # Allow temporarily empty/invalid text while editing; save validates it.
        self.preview.delete("all")
        self.preview_photo = None
        if self.pending_image is not None:
            width, height = self.app.screen_size
            factor = min(440 / width, 100 / height)
            preview_size = (max(1, round(width * factor)), max(1, round(height * factor)))
            image = render_background(self.pending_image, preview_size, scale)
            image = ImageEnhance.Brightness(image).enhance(intensity)
            self.preview_photo = ImageTk.PhotoImage(image, master=self)
            self.preview.create_image(220, 50, image=self.preview_photo)
        else:
            self.preview.create_text(220, 50, text="ไม่มีภาพ — ใช้พื้นหลังดำ", fill="white", font=("Segoe UI", 11))
        self.name_label.configure(text=self.image_name or "ยังไม่ได้เลือกภาพ")

    def choose_image(self):
        path = filedialog.askopenfilename(parent=self, title="เลือกภาพพื้นหลัง", filetypes=[
            ("Image files", "*.png *.jpg *.jpeg *.webp *.bmp *.gif *.tif *.tiff"),
            ("All files", "*.*"),
        ])
        if path:
            self.import_image(path)

    def import_image(self, path):
        try:
            image = load_background(path)
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            messagebox.showerror("เปิดภาพไม่ได้", f"กรุณาเลือกไฟล์ภาพที่รองรับ\n{error}", parent=self)
            return False
        self.pending_image = image
        self.image_name = Path(path).name
        self.image_dirty = True
        self.update_preview()
        return True

    def remove_image(self):
        self.pending_image = None
        self.image_name = ""
        self.image_dirty = True
        self.update_preview()

    def save(self):
        try:
            settings = Settings(trigger_count=int(self.count_var.get()),
                                dim_step=float(self.step_var.get()) / 100,
                                max_dim=float(self.max_var.get()) / 100,
                                background=self.app.settings.background,
                                background_name=self.image_name,
                                background_scale=float(self.scale_var.get()) / 100,
                                fade_duration=float(self.fade_var.get()),
                                image_intensity=float(self.intensity_var.get()) / 100).validate()
            self.app.apply_settings(settings, image=self.pending_image, replace_image=self.image_dirty)
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
            app.root.after(750, app.close)
        app.run()
    finally:
        kernel32.CloseHandle(mutex)


if __name__ == "__main__":
    main()
