from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from PIL import Image
from loomly import DimState, Profiles, Settings, SettingsStore


class ProfileTests(unittest.TestCase):
    def test_legacy_configuration_is_preserved_in_custom(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            original = Settings(dim_step=0.1, fade_duration=1)
            store.save(original)
            profiles = store.load_profiles()
            self.assertEqual(profiles.active_mode, 'custom')
            self.assertEqual(profiles.custom, original)
            self.assertEqual(profiles.normal, Settings())

    def test_normal_cannot_be_modified(self):
        with self.assertRaises(ValueError):
            Profiles(normal=Settings(trigger_count=1)).validate()

    def test_modes_are_separate_across_save_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            custom = Settings(trigger_count=7, recover_delay=8, fade_duration=2,
                              audio_enabled=False, audio_reduction=0.4)
            profiles = store.save_profiles(Profiles(active_mode='custom', custom=custom))
            self.assertEqual(store.load_profiles(), profiles)
            switched = store.save_profiles(replace(profiles, active_mode='normal'))
            self.assertEqual(switched.current, Settings())
            self.assertEqual(store.load_profiles().custom, custom)

    def test_normal_save_keeps_custom_image_and_custom_removal_prunes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            saved = store.save_profiles(Profiles(active_mode='custom'),
                                        images={'custom': Image.new('RGB', (5, 5), 'red')},
                                        replaced=('custom',))
            image_path = store.directory / saved.custom.background
            store.save_profiles(replace(saved, active_mode='normal'))
            self.assertTrue(image_path.is_file())
            removed = store.save_profiles(saved, replaced=('custom',))
            self.assertEqual(removed.custom.background, '')
            self.assertFalse(image_path.exists())

    def test_failed_profile_save_keeps_configuration_and_previous_image(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            old = store.save_profiles(Profiles(active_mode='custom'),
                                      images={'custom': Image.new('RGB', (5, 5))}, replaced=('custom',))
            with patch('loomly.os.replace', side_effect=OSError('Disk full')):
                with self.assertRaises(OSError):
                    store.save_profiles(old, images={'custom': Image.new('RGB', (10, 10))},
                                        replaced=('custom',))
            self.assertEqual(store.load_profiles(), old)
            self.assertEqual(len(list(store.directory.glob('background-*.png'))), 1)
            self.assertEqual(list(store.directory.glob('*.tmp')), [])

    def test_unknown_schema_uses_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(directory)
            store.path.write_text(json.dumps({'version': 99}), encoding='utf-8')
            self.assertEqual(store.load_profiles(), Profiles())


class RecoveryTests(unittest.TestCase):
    def test_custom_delay_and_fade_are_independent(self):
        state = DimState(Settings(recover_delay=8, fade_duration=2))
        state.dim, state.last = 0.8, 10
        self.assertEqual(state.recover(17), 0.8)
        self.assertAlmostEqual(state.recover(19), 0.4)
        self.assertEqual(state.recover(20), 0)

    def test_zero_delay_and_duration_clear_immediately(self):
        state = DimState(Settings(recover_delay=0, fade_duration=0))
        state.dim, state.last = 0.5, 10
        self.assertEqual(state.recover(10), 0)

    def test_scroll_during_fade_starts_a_fresh_wait(self):
        state = DimState(Settings(trigger_count=1, recover_delay=2, fade_duration=4))
        state.dim, state.last = 0.8, 10
        self.assertAlmostEqual(state.recover(14), 0.4)
        state.register('up', 14)
        state.register('up', 14.1)
        self.assertAlmostEqual(state.recover(15), 0.6)
        self.assertAlmostEqual(state.recover(18.1), 0.3)

    def test_new_fields_reject_invalid_values(self):
        for settings in (Settings(recover_delay=-1), Settings(recover_delay=61),
                         Settings(recover_delay=float('nan')), Settings(audio_enabled=1),
                         Settings(audio_reduction=float('nan')), Settings(audio_reduction=1.1)):
            with self.subTest(settings=settings), self.assertRaises(ValueError):
                settings.validate()


if __name__ == '__main__':
    unittest.main()
