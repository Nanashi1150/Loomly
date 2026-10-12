import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from audio_control import AudioDucker


class FakeEndpoint:
    def __init__(self, volume=0.8, device_id="speakers"):
        self.id = device_id
        self.volume = volume
        self.muted = False
        self.fail = False
        self.writes = []

    def get_volume(self):
        if self.fail:
            raise OSError("Disconnected")
        return self.volume

    def set_volume(self, value):
        if self.fail:
            raise OSError("Disconnected")
        self.volume = value
        self.writes.append(value)


class AudioTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = FakeEndpoint()
        self.audio = AudioDucker(lambda: self.endpoint)

    def test_volume_tracks_dim_and_returns_to_original(self):
        self.audio.update(0.5, True, now=1)
        self.assertAlmostEqual(self.endpoint.volume, 0.4)
        self.audio.update(0.25, True, now=1.1)
        self.assertAlmostEqual(self.endpoint.volume, 0.6)
        self.audio.update(0, True, now=1.2)
        self.assertAlmostEqual(self.endpoint.volume, 0.8)

    def test_disable_or_restore_returns_baseline(self):
        for action in (lambda: self.audio.update(0.7, False, now=2), self.audio.restore):
            self.audio.update(0.7, True, now=1)
            action()
            self.assertAlmostEqual(self.endpoint.volume, 0.8)

    def test_reduction_strength_does_not_compound_and_mute_is_untouched(self):
        self.endpoint.muted = True
        for now in (1, 1.1, 1.2, 1.3):
            self.audio.update(0.5, True, 0.5, now=now)
        self.assertAlmostEqual(self.endpoint.volume, 0.6)
        self.assertEqual(len(self.endpoint.writes), 1)
        self.audio.restore()
        self.assertTrue(self.endpoint.muted)

    def test_manual_volume_change_wins_even_when_paused_immediately(self):
        self.audio.update(0.5, True, now=1)
        self.endpoint.volume = 0.3
        self.audio.restore()
        self.assertEqual(self.endpoint.volume, 0.3)

    def test_manual_change_blocks_ducking_until_a_new_cycle(self):
        self.audio.update(0.5, True, now=1)
        self.endpoint.volume = 0.3
        self.audio.update(0.8, True, now=1.1)
        self.audio.update(0.1, True, now=1.2)
        self.assertEqual(self.endpoint.volume, 0.3)
        self.audio.update(0, True, now=1.3)
        self.audio.update(0.5, True, now=1.4)
        self.assertAlmostEqual(self.endpoint.volume, 0.15)
        self.audio.restore()
        self.assertAlmostEqual(self.endpoint.volume, 0.3)

    def test_device_switch_restores_old_device_and_uses_new_baseline(self):
        old = self.endpoint
        self.audio.update(0.5, True, now=1)
        self.endpoint = FakeEndpoint(0.6, "headphones")
        self.audio.update(0.5, True, now=2.1)
        self.assertAlmostEqual(old.volume, 0.8)
        self.assertAlmostEqual(self.endpoint.volume, 0.3)
        self.audio.restore()
        self.assertAlmostEqual(self.endpoint.volume, 0.6)

    def test_disconnect_is_nonfatal_and_restoration_retries(self):
        self.audio.update(0.5, True, now=1)
        self.endpoint.fail = True
        self.audio.restore()
        self.assertTrue(self.audio.error)
        self.endpoint.fail = False
        self.audio.update(0, False, now=2)
        self.assertAlmostEqual(self.endpoint.volume, 0.8)
        self.assertFalse(self.audio.error)


if __name__ == '__main__':
    unittest.main()
