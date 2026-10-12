"""Check real Windows overlay properties; optionally capture the corner."""
import ctypes
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

SOURCE_DIR = Path(__file__).resolve().parents[1] / 'src'
sys.path.insert(0, str(SOURCE_DIR))

from PIL import Image, ImageTk

from loomly import INDICATOR_SIZE, LoomlyApp
from cursor_detect import ScrollCursorDetector


def check_cursor_detector(detector):
    assert len(detector.templates) == 11, "Bundle every Chromium panning cursor"
    user32 = ctypes.windll.user32
    user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.LoadCursorW.restype = ctypes.c_void_p
    for identifier in (32512, 32513, 32514, 32515, 32644, 32645, 32646, 32649):
        handle = user32.LoadCursorW(None, identifier)
        assert handle and not detector._classify(handle), "Normal cursors must not be Scroll"
    for path in (SOURCE_DIR / "cursor_templates").glob("pan_*.cur"):
        handle = user32.LoadImageW(None, str(path), 2, 0, 0, 0x10)
        assert handle
        try:
            assert detector._classify(handle), f"Scroll not recognized: {path.name}"
        finally:
            user32.DestroyCursor(handle)
            detector._classify.cache_clear()
    assert detector.is_scroll() in (True, False, None)


def check_middle_callbacks(app, callbacks, keyboard_callback):
    from pynput import keyboard, mouse

    # Exercise the exact callbacks registered with pynput, without clicking other apps.
    with patch("loomly.time.monotonic", return_value=100) as clock, \
            patch.object(app.cursor_detector, "is_scroll", return_value=True) as cursor_result:
        callbacks["on_click"](400, 300, mouse.Button.middle, True)
        callbacks["on_click"](400, 300, mouse.Button.middle, False)
        callbacks["on_move"](700, 300)
        app.tick()
        assert app.canvas.itemcget(app.triangle, "fill") == "#E53935"
        assert app.root.attributes("-alpha") == 0
        clock.return_value = 110
        callbacks["on_move"](700, 515)
        app.tick()
        assert app.root.attributes("-alpha") == 0, "Idle time must not dim retroactively"
        clock.return_value = 110.5
        app.tick()
        assert abs(app.root.attributes("-alpha") - 0.1) < 0.01
        keyboard_callback(keyboard.Key.esc)
        app.tick()
        assert app.canvas.itemcget(app.triangle, "fill") == "#159DE8"
        # A middle click during existing dimming is red, but a normal cursor cannot add dim.
        app.state.configure(app.settings)
        app.state.dim, app.state.last = 0.3, clock.return_value
        cursor_result.return_value = False
        callbacks["on_click"](400, 300, mouse.Button.middle, True)
        app.tick()
        assert app.canvas.itemcget(app.triangle, "fill") == "#E53935"
        callbacks["on_move"](400, 515)
        clock.return_value += 0.9
        app.tick()
        assert app.canvas.itemcget(app.triangle, "fill") == "#159DE8"
        assert abs(app.state.dim - 0.3) < 0.001
        cursor_result.return_value = True
        for cancel in (
            lambda: callbacks["on_click"](400, 300, mouse.Button.middle, True),
            lambda: callbacks["on_click"](400, 300, mouse.Button.left, True),
            lambda: callbacks["on_scroll"](400, 300, 0, 1),
            lambda: callbacks["on_scroll"](400, 300, 1, 0),
            lambda: keyboard_callback(keyboard.Key.down),
        ):
            app.state.configure(app.settings)
            callbacks["on_click"](400, 300, mouse.Button.middle, True)
            callbacks["on_click"](400, 300, mouse.Button.middle, False)
            cancel()
            assert app.state.middle_origin is None
        for y in (85, 515):
            app.state.configure(app.settings)
            callbacks["on_click"](400, 300, mouse.Button.middle, True)
            callbacks["on_move"](400, y)
            app.tick()  # Confirm the cursor before measuring the next second.
            clock.return_value += 1
            app.tick()
            assert app.root.attributes("-alpha") > 0.1
            callbacks["on_click"](400, y, mouse.Button.middle, False)
            assert app.state.middle_origin is None
    app.state.configure(app.settings)


def check_app(directory):
    from pynput import mouse, keyboard
    with patch("pynput.mouse.Listener", wraps=mouse.Listener) as mouse_listener, \
            patch("pynput.keyboard.Listener", wraps=keyboard.Listener) as keyboard_listener:
        app = LoomlyApp(data_dir=Path(directory) / "data")
    callbacks = mouse_listener.call_args.kwargs
    keyboard_callback = keyboard_listener.call_args.kwargs["on_press"]
    try:
        app.root.update()
        assert app.indicator.winfo_x() == 0
        assert app.indicator.winfo_y() == app.root.winfo_screenheight() - INDICATOR_SIZE
        assert app.indicator.winfo_width() == app.indicator.winfo_height() == 50
        assert app.canvas.find_all() == (app.triangle,), "Only the triangle should remain"
        style = ctypes.windll.user32.GetWindowLongW(app.overlay_hwnd, -20)
        assert style & 0x20, "Overlay must allow mouse clicks through"
        assert style & 0x08000000, "Overlay must not take focus"
        assert all(listener.running for listener in app.listeners)
        check_cursor_detector(app.cursor_detector)
        check_middle_callbacks(app, callbacks, keyboard_callback)
        for _ in range(4):
            app.state.register("up")
        app.tick()
        app.root.update()
        assert abs(app.root.attributes("-alpha") - 0.2) < 0.01
        for _ in range(36):
            app.state.register("up")
        app.tick()
        app.root.update()
        assert abs(app.root.attributes("-alpha") - 0.95) < 0.01
        app.toggle()
        app.tick()
        app.root.update()
        assert app.root.attributes("-alpha") == 0
        assert app.canvas.itemcget(app.triangle, "fill") == "#748A99"
        app.toggle()
        app.root.update()
        assert app.canvas.itemcget(app.triangle, "fill") == "#159DE8"
        app.canvas.event_generate("<Button-1>", x=10, y=40)
        app.root.update()
        dialog = app.settings_window
        assert dialog is not None and dialog.winfo_viewable()
        assert app.state.suspended and app.root.attributes("-alpha") == 0
        dialog.count_var.set("0")
        with patch("loomly.messagebox.showerror") as error:
            assert not dialog.save()
            assert error.called
        assert app.settings.trigger_count == 3
        dialog.count_var.set("2")
        dialog.step_var.set("35")
        dialog.max_var.set("95")
        source = Path(directory) / "custom-test.png"
        Image.new("RGB", (60, 40), "red").save(source)
        assert dialog.import_image(source)
        assert dialog.pending_image.size == (60, 40), "Keep the uncropped original"
        dialog.scale_var.set("0")
        with patch("loomly.messagebox.showerror") as error:
            assert not dialog.save()
            assert error.called
        dialog.scale_var.set("50")
        dialog.intensity_var.set("101")
        with patch("loomly.messagebox.showerror") as error:
            assert not dialog.save()
            assert error.called
        dialog.intensity_var.set("42")
        dialog.fade_var.set("-1")
        with patch("loomly.messagebox.showerror") as error:
            assert not dialog.save()
            assert error.called
        dialog.fade_var.set("2.5")
        app.root.update()
        assert dialog.preview_photo is not None
        preview = ImageTk.getimage(dialog.preview_photo)
        assert preview.getpixel((preview.width // 2, preview.height // 2))[:3] == (107, 0, 0)
        with patch("loomly.messagebox.showerror") as error:
            assert not dialog.import_image(Path(directory) / "missing.png")
            assert error.called
        if "--capture" in sys.argv:
            from PIL import ImageGrab
            app.root.update()
            time.sleep(0.2)
            x, y = dialog.winfo_rootx(), dialog.winfo_rooty()
            ImageGrab.grab(bbox=(x, y, x + dialog.winfo_width(), y + dialog.winfo_height()),
                           include_layered_windows=True).save(
                Path(__file__).with_name("settings-preview.png"))
        assert dialog.save()
        assert not app.state.suspended and app.settings_window is None
        assert app.settings.trigger_count == 2
        assert app.settings.dim_step == 0.35
        assert app.settings.background_scale == 0.5
        assert app.settings.image_intensity == 0.42
        assert app.settings.fade_duration == 2.5
        assert app.overlay_photo is not None
        assert (app.overlay_photo.width(), app.overlay_photo.height()) == app.screen_size
        overlay = ImageTk.getimage(app.overlay_photo)
        assert overlay.getpixel((0, 0))[:3] == (0, 0, 0)
        assert overlay.getpixel((app.screen_size[0] // 2, app.screen_size[1] // 2))[:3] == (107, 0, 0)
        for _ in range(3):
            app.state.register("up")
        app.tick()
        app.root.update()
        assert abs(app.root.attributes("-alpha") - 0.35) < 0.01
        source.unlink()
        assert app.store.load() == app.settings
        saved = app.settings
        app.close()
        app = LoomlyApp(data_dir=Path(directory) / "data")
        app.root.update()
        assert app.settings == saved and app.overlay_photo is not None
        app.open_settings()
        dialog = app.settings_window
        dialog.scale_var.set("150")
        dialog.intensity_var.set("80")
        dialog.fade_var.set("10")
        dialog.remove_image()
        dialog.cancel()
        assert app.settings == saved and app.overlay_photo is not None
        app.open_settings()
        dialog = app.settings_window
        assert dialog.scale_var.get() == "50"
        assert dialog.intensity_var.get() == "42"
        assert dialog.fade_var.get() == "2.5"
        dialog.scale_var.set("150")
        assert dialog.save()
        assert app.settings.background_scale == 1.5
        assert ImageTk.getimage(app.overlay_photo).getpixel((0, 0))[:3] == (107, 0, 0)
        app.open_settings()
        dialog = app.settings_window
        dialog.remove_image()
        assert dialog.save()
        assert app.settings.background == "" and app.overlay_photo is None
        if "--capture" in sys.argv:
            from PIL import ImageGrab
            time.sleep(0.2)
            height = app.root.winfo_screenheight()
            ImageGrab.grab(bbox=(0, height - INDICATOR_SIZE, INDICATOR_SIZE, height),
                           include_layered_windows=True).save(
                Path(__file__).with_name("corner-preview.png"))
        app.exit_requested.set()
        app.tick()
        assert app.closed
        print("Windows GUI checks passed: middle-button callbacks, dimming, image scaling, preview, validation, restart, cancel, and removal.")
    finally:
        app.close()


def verify():
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
    with tempfile.TemporaryDirectory() as directory:
        check_app(directory)


if __name__ == "__main__":
    verify()
