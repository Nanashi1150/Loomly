import unittest
from unittest.mock import patch

from pathlib import Path
import tempfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from PIL import Image

from loomly import (AUTOSCROLL_DEAD_ZONE, AUTOSCROLL_DISTANCE,
                          DimState, Settings, SettingsStore, load_background,
                          prepare_background, render_background)
from cursor_detect import matches_scroll_cursor


def start_middle(state, now):
    state.middle_click(300, True, now=now)
    state.observe_cursor(True, now=now)


class DimStateTests(unittest.TestCase):
    def test_middle_click_latches_and_horizontal_band_does_not_dim(self):
        state = DimState()
        start_middle(state, now=10)
        self.assertEqual(state.indicator_color(), "#E53935")
        state.middle_click(300, False, now=10.1)
        state.move(300 + AUTOSCROLL_DEAD_ZONE, now=10.1)
        self.assertEqual(state.recover(now=11), 0)
        self.assertEqual(state.middle_origin, 300)
        state.middle_click(350, True, now=12)
        state.middle_click(350, False, now=12.1)
        self.assertIsNone(state.middle_origin)
        self.assertEqual(state.indicator_color(), "#159DE8")

    def test_middle_distance_controls_continuous_dimming_in_both_directions(self):
        for direction in (-1, 1):
            with self.subTest(direction=direction):
                state = DimState()
                start_middle(state, now=10)
                state.middle_click(300, False, now=10)
                state.move(300 + direction * (AUTOSCROLL_DEAD_ZONE + AUTOSCROLL_DISTANCE / 2), now=10)
                self.assertAlmostEqual(state.recover(now=11), 0.1)
                # No new move event is needed while native autoscroll continues.
                self.assertAlmostEqual(state.recover(now=12), 0.2)
                state.move(300 + direction * (AUTOSCROLL_DEAD_ZONE + AUTOSCROLL_DISTANCE * 3), now=12)
                self.assertAlmostEqual(state.recover(now=13), 0.4)
                self.assertAlmostEqual(state.recover(now=30), 0.95)
                state.move(300, now=30)
                self.assertAlmostEqual(state.recover(now=33.1), 0.931)

    def test_middle_drag_release_stops_and_recovers_after_delay(self):
        state = DimState()
        start_middle(state, now=10)
        state.move(300 + AUTOSCROLL_DEAD_ZONE + AUTOSCROLL_DISTANCE, now=10)
        self.assertAlmostEqual(state.recover(now=11), 0.2)
        state.middle_click(515, False, now=11)
        self.assertIsNone(state.middle_origin)
        self.assertAlmostEqual(state.recover(now=13), 0.2)
        self.assertAlmostEqual(state.recover(now=14.1), 0.196)

    def test_middle_mode_resets_for_cancel_wheel_pause_settings(self):
        for action in (lambda s: s.cancel_middle(now=11), lambda s: s.register("up", 11),
                       lambda s: s.toggle(), lambda s: s.suspend(True),
                       lambda s: s.configure(Settings())):
            with self.subTest(action=action):
                state = DimState()
                start_middle(state, now=10)
                action(state)
                self.assertIsNone(state.middle_origin)
                self.assertFalse(state.middle_pressed)
        for action in (lambda s: s.toggle(), lambda s: s.suspend(True)):
            state = DimState()
            action(state)
            start_middle(state, now=10)
            state.move(600, now=11)
            self.assertIsNone(state.middle_origin)
            self.assertEqual(state.recover(now=11), 0)

    def test_middle_movement_does_not_apply_new_speed_to_previous_time(self):
        state = DimState()
        start_middle(state, now=10)
        state.middle_click(300, False, now=10)
        state.move(515, now=20)
        self.assertEqual(state.dim, 0)  # Ten idle seconds must not cause instant dimming.
        self.assertAlmostEqual(state.recover(now=20.5), 0.1)
        state.move(300, now=21)
        self.assertAlmostEqual(state.dim, 0.2)  # Includes last half-second before returning.
        self.assertAlmostEqual(state.recover(now=23), 0.2)
        self.assertAlmostEqual(state.recover(now=24.1), 0.196)

    def test_middle_drag_is_cancelled_even_if_returned_to_origin_before_release(self):
        state = DimState()
        start_middle(state, now=10)
        state.move(515, now=10)
        state.move(300, now=11)
        state.middle_click(300, False, now=11)
        self.assertIsNone(state.middle_origin)
        self.assertAlmostEqual(state.dim, 0.2)

    def test_middle_cancel_accounts_for_final_interval(self):
        state = DimState()
        start_middle(state, now=10)
        state.move(515, now=10)
        state.cancel_middle(now=11)
        self.assertAlmostEqual(state.dim, 0.2)
        self.assertIsNone(state.middle_origin)

    def test_fourth_event_dims_and_starts_new_group(self):
        state = DimState()
        for now in (10, 10.1, 10.2):
            state.register("up", now)
        self.assertEqual(state.dim, 0)
        state.register("up", 10.3)
        self.assertAlmostEqual(state.dim, 0.2)
        for now in (10.4, 10.5, 10.6):
            state.register("up", now)
        self.assertAlmostEqual(state.dim, 0.2)
        state.register("up", 10.7)
        self.assertAlmostEqual(state.dim, 0.4)

    def test_direction_change_and_timeout_reset_streak(self):
        for next_direction, next_time in (("down", 10.3), ("up", 13)):
            with self.subTest(direction=next_direction):
                state = DimState()
                for now in (10, 10.1, 10.2):
                    state.register("up", now)
                state.register(next_direction, next_time)
                self.assertEqual(state.dim, 0)
                self.assertEqual(state.count, 1)

    def test_dim_is_capped(self):
        state = DimState()
        for index in range(40):
            state.register("up", 10 + index * 0.1)
        self.assertEqual(state.dim, 0.95)

    def test_custom_threshold_step_and_cap(self):
        state = DimState(Settings(trigger_count=1, dim_step=0.3, max_dim=0.85))
        state.register("up", 10)
        self.assertEqual(state.dim, 0)
        state.register("up", 10.1)
        self.assertAlmostEqual(state.dim, 0.3)
        for index in range(10):
            state.register("up", 10.2 + index * 0.1)
        self.assertAlmostEqual(state.dim, 0.85)

    def test_editing_ignores_input_and_keeps_pause_state(self):
        state = DimState()
        state.suspend(True)
        for index in range(8):
            state.register("up", 10 + index * 0.1)
        self.assertEqual(state.dim, 0)
        self.assertEqual(state.count, 0)
        state.toggle()
        state.suspend(False)
        self.assertFalse(state.enabled)
        state.configure(Settings(trigger_count=2))
        self.assertEqual(state.count, 0)

    def test_recovery_waits_and_never_goes_negative(self):
        state = DimState()
        state.dim, state.last = 0.015, 10
        with patch("loomly.time.monotonic", return_value=12.9):
            self.assertEqual(state.recover(), 0.015)
        with patch("loomly.time.monotonic", return_value=13.1):
            self.assertAlmostEqual(state.recover(), 0.0147)
            self.assertAlmostEqual(state.recover(), 0.0147)
        self.assertEqual(state.recover(now=18), 0)

    def test_fade_duration_is_elapsed_time_not_tick_count(self):
        state = DimState(Settings(fade_duration=2))
        state.dim, state.last = 0.8, 10
        self.assertEqual(state.recover(now=13), 0.8)
        self.assertAlmostEqual(state.recover(now=14), 0.4)
        self.assertAlmostEqual(state.recover(now=14), 0.4)
        self.assertEqual(state.recover(now=15), 0)
        instant = DimState(Settings(fade_duration=0))
        instant.dim, instant.last = 0.8, 10
        self.assertEqual(instant.recover(now=13.01), 0)

    def test_middle_pending_is_red_but_does_not_dim_without_scroll_cursor(self):
        for result in (False, None):
            state = DimState()
            state.middle_click(300, True, now=20)
            self.assertEqual(state.indicator_color(), "#E53935")
            state.move(515, now=20)
            state.observe_cursor(result, now=20.5)
            self.assertEqual(state.recover(now=20.5), 0)
            state.observe_cursor(result, now=20.9)
            self.assertIsNone(state.middle_origin)
            self.assertEqual(state.indicator_color(), "#159DE8")

    def test_confirmed_cursor_loss_stops_dimming_and_cancels(self):
        state = DimState()
        start_middle(state, now=10)
        state.move(515, now=10)
        self.assertAlmostEqual(state.recover(now=11), 0.2)
        state.observe_cursor(False, now=11)
        self.assertAlmostEqual(state.recover(now=11.1), 0.2)
        state.observe_cursor(False, now=11.3)
        self.assertIsNone(state.middle_origin)

    def test_delayed_cursor_confirmation_does_not_dim_retroactively(self):
        state = DimState()
        state.middle_click(300, True, now=20)
        state.move(515, now=20)
        state.observe_cursor(True, now=20.5)
        self.assertEqual(state.recover(now=20.5), 0)
        self.assertAlmostEqual(state.recover(now=21), 0.1)

    def test_pause_clears_dim_and_ignores_input(self):
        state = DimState()
        for index in range(4):
            state.register("up", 10 + index * 0.1)
        self.assertFalse(state.toggle())
        for index in range(8):
            state.register("up", 11 + index * 0.1)
        self.assertEqual(state.dim, 0)
        self.assertTrue(state.toggle())
        state.register("up", 12)
        self.assertEqual(state.count, 1)


class SettingsTests(unittest.TestCase):
    def test_invalid_values_are_rejected(self):
        for settings in (Settings(trigger_count=0), Settings(trigger_count=2.5),
                         Settings(max_dim=0.96), Settings(dim_step=0),
                         Settings(dim_step=0.8, max_dim=0.5), Settings(max_dim=float("nan")),
                         Settings(background_scale=0.09), Settings(background_scale=2.01),
                         Settings(background_scale=float("nan")), Settings(background_scale=True),
                         Settings(fade_duration=-1), Settings(fade_duration=61),
                         Settings(fade_duration=float("nan")), Settings(image_intensity=1.01),
                         Settings(image_intensity=-0.1), Settings(image_intensity=True),
                         Settings(background="../outside.png")):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()

    def test_fade_and_intensity_are_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            settings = Settings(fade_duration=2.5, image_intensity=0.6)
            store.save(settings)
            self.assertEqual(store.load(), settings)


    def test_background_scale_centers_image_and_preserves_aspect_ratio(self):
        image = Image.new("RGB", (40, 20), "red")
        small = render_background(image, (80, 60), 0.5)
        self.assertEqual(small.size, (80, 60))
        self.assertEqual(small.getpixel((0, 0)), (0, 0, 0))
        self.assertEqual(small.getpixel((40, 30)), (255, 0, 0))
        self.assertEqual(small.getbbox(), (10, 15, 70, 45))
        full = render_background(image, (80, 60))
        self.assertEqual(full.getbbox(), (0, 0, 80, 60))
        # Enlargement crops symmetrically rather than stretching.
        stripes = Image.new("RGB", (100, 100), "blue")
        stripes.paste("red", (25, 0, 75, 100))
        zoom = render_background(stripes, (100, 100), 2)
        self.assertEqual(zoom.getpixel((50, 50)), (255, 0, 0))
        self.assertGreater(zoom.getpixel((5, 50))[0], 250)

    def test_original_image_and_scale_survive_restart_and_source_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "original.png"
            Image.new("RGB", (40, 20), "red").save(path)
            store = SettingsStore(Path(directory) / "data")
            saved = store.save(Settings(background_scale=0.5), load_background(path), replace_image=True)
            path.unlink()
            self.assertEqual(store.load().background_scale, 0.5)
            self.assertEqual(load_background(store.directory / saved.background).size, (40, 20))
            store.save(Settings(background=saved.background, background_scale=1.5))
            self.assertEqual(store.load().background_scale, 1.5)

    def test_legacy_settings_without_scale_still_load(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            store.path.write_text('{"trigger_count": 7}', encoding="utf-8")
            self.assertEqual(store.load().trigger_count, 7)
            self.assertEqual(store.load().background_scale, 1.0)

    def test_settings_and_image_survive_original_file_removal(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.png"
            Image.new("RGB", (40, 20), "red").save(source)
            image = prepare_background(source, (80, 60))
            self.assertEqual(image.size, (80, 60))
            store = SettingsStore(Path(directory) / "data")
            saved = store.save(Settings(trigger_count=7, dim_step=0.1, max_dim=0.95,
                                        background_name=source.name), image, replace_image=True)
            source.unlink()
            self.assertEqual(store.load(), saved)
            self.assertTrue((store.directory / saved.background).is_file())
            saved = store.save(saved, replace_image=True)
            self.assertEqual(saved.background, "")
            self.assertEqual(store.load().background, "")

    def test_cancelled_or_failed_save_preserves_configuration(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            original = store.save(Settings())
            with patch("loomly.os.replace", side_effect=OSError("Disk error")):
                with self.assertRaises(OSError):
                    store.save(Settings(trigger_count=8), Image.new("RGB", (5, 5)), replace_image=True)
            self.assertEqual(store.load(), original)
            self.assertEqual(list(Path(directory).glob("background-*.png")), [])
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_damaged_configuration_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            store.path.write_text("{broken", encoding="utf-8")
            self.assertEqual(store.load(), Settings())


class CursorMatchingTests(unittest.TestCase):
    def test_cursor_matching_accepts_template_and_rejects_different_shape(self):
        template = bytes([0, 255, 0, 255] * 32)
        self.assertTrue(matches_scroll_cursor(template, [template]))
        self.assertFalse(matches_scroll_cursor(bytes([255, 0, 255, 0] * 32), [template]))
        self.assertFalse(matches_scroll_cursor(None, [template]))
        self.assertFalse(matches_scroll_cursor(template, []))


if __name__ == "__main__":
    unittest.main()
