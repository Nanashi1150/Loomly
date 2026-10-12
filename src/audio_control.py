"""Reduce Windows output volume without changing mute or overriding manual edits."""
import time


class WindowsEndpoint:
    def __init__(self, device):
        self.id = device.id
        self.volume = device.EndpointVolume

    def get_volume(self):
        return float(self.volume.GetMasterVolumeLevelScalar())

    def set_volume(self, value):
        self.volume.SetMasterVolumeLevelScalar(max(0.0, min(1.0, value)), None)


def default_endpoint():
    from pycaw.pycaw import AudioUtilities
    device = AudioUtilities.GetSpeakers()
    if device is None:
        raise OSError("No audio output device")
    return WindowsEndpoint(device)


class AudioDucker:
    def __init__(self, endpoint_factory=default_endpoint):
        self.endpoint_factory = endpoint_factory
        self.endpoint = None
        self.baseline = None
        self.last_applied = None
        self.manual_override = False
        self.next_device_check = 0.0
        self.next_retry = 0.0
        self.error = ""

    def _restore(self):
        if self.endpoint is not None and self.baseline is not None:
            current = self.endpoint.get_volume()
            if (not self.manual_override and self.last_applied is not None
                    and abs(current - self.last_applied) <= 0.003):
                self.endpoint.set_volume(self.baseline)
        self.baseline = self.last_applied = None
        self.manual_override = False

    def restore(self):
        try:
            self._restore()
            self.error = ""
        except Exception:
            # Retain the baseline so a subsequent tick can retry after a device recovers.
            self.error = "ควบคุมเสียงไม่ได้ — ตรวจอุปกรณ์เสียง"

    def update(self, dim, enabled, reduction=1.0, now=None):
        now = time.monotonic() if now is None else now
        if now < self.next_retry:
            return
        try:
            if not enabled or dim <= 0 or reduction <= 0:
                self._restore()
                self.error = ""
                return
            if self.endpoint is None or now >= self.next_device_check:
                candidate = self.endpoint_factory()
                if self.endpoint is None or candidate.id != self.endpoint.id:
                    self._restore()
                    self.endpoint = candidate
                self.next_device_check = now + 1.0
            current = self.endpoint.get_volume()
            if self.baseline is None:
                self.baseline = current
            elif self.last_applied is not None and abs(current - self.last_applied) > 0.003:
                # Manual volume changes take priority until this dimming cycle ends.
                self.manual_override = True
            if not self.manual_override:
                target = self.baseline * (1.0 - max(0.0, min(1.0, dim)) * reduction)
                if abs(current - target) > 0.001:
                    self.endpoint.set_volume(target)
                self.last_applied = self.endpoint.get_volume()
            self.error = ""
        except Exception:
            self.error = "ควบคุมเสียงไม่ได้ — ตรวจอุปกรณ์เสียง"
            self.next_retry = now + 2.0
