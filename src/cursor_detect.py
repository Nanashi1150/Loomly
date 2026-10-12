"""Recognize Chromium panning cursors using their actual Windows-rendered pixels.

Templates are from Chromium, under the included BSD license. No screen capture
or cursor changes are needed. Unsupported or unreadable cursors are not approved.
"""
import ctypes
from ctypes import wintypes
from functools import lru_cache
from pathlib import Path


class CursorInfo(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hCursor", wintypes.HANDLE), ("ptScreenPos", wintypes.POINT)]


class BitmapHeader(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("width", wintypes.LONG),
                ("height", wintypes.LONG), ("planes", wintypes.WORD),
                ("bits", wintypes.WORD), ("compression", wintypes.DWORD),
                ("image_size", wintypes.DWORD), ("xppm", wintypes.LONG),
                ("yppm", wintypes.LONG), ("used", wintypes.DWORD),
                ("important", wintypes.DWORD)]


def matches_scroll_cursor(pixels, templates):
    # Two backgrounds include the transparency mask, not just the visible ink.
    return any(len(pixels) == len(template) and
               sum((a - b) ** 2 for a, b in zip(pixels, template)) / len(pixels) <= 12 ** 2
               for template in templates) if pixels else False


class ScrollCursorDetector:
    def __init__(self):
        self.user32, self.gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
        self.user32.GetCursorInfo.argtypes = [ctypes.POINTER(CursorInfo)]
        self.user32.GetCursorInfo.restype = wintypes.BOOL
        self.user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR,
                                          wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.user32.LoadImageW.restype = wintypes.HANDLE
        self.user32.DestroyCursor.argtypes = [wintypes.HANDLE]
        self.user32.DrawIconEx.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                         wintypes.HANDLE, ctypes.c_int, ctypes.c_int,
                                         wintypes.UINT, wintypes.HBRUSH, wintypes.UINT]
        self.user32.DrawIconEx.restype = wintypes.BOOL
        self.gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi32.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi32.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.POINTER(BitmapHeader),
                                               wintypes.UINT, ctypes.POINTER(ctypes.c_void_p),
                                               wintypes.HANDLE, wintypes.DWORD]
        self.gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        self.gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
        self.gdi32.SelectObject.restype = wintypes.HANDLE
        self.gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
        self.gdi32.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi32.GdiFlush.argtypes = []
        self.templates = []
        for path in sorted((Path(__file__).parent / "cursor_templates").glob("pan_*.cur")):
            handle = self.user32.LoadImageW(None, str(path), 2, 0, 0, 0x10)
            if handle:
                try:
                    pixels = self.render(handle)
                    if pixels:
                        self.templates.append(pixels)
                finally:
                    self.user32.DestroyCursor(handle)

    def render(self, handle):
        dc = self.gdi32.CreateCompatibleDC(None)
        if not dc:
            return None
        bitmap, previous = None, None
        try:
            header = BitmapHeader(size=ctypes.sizeof(BitmapHeader), width=32,
                                  height=-32, planes=1, bits=32)
            address = ctypes.c_void_p()
            bitmap = self.gdi32.CreateDIBSection(dc, ctypes.byref(header), 0,
                                                ctypes.byref(address), None, 0)
            if not bitmap or not address.value:
                return None
            previous = self.gdi32.SelectObject(dc, bitmap)
            if not previous or previous == ctypes.c_void_p(-1).value:
                previous = None
                return None
            output = bytearray()
            for background in (0, 255):
                ctypes.memset(address, background, 32 * 32 * 4)
                if not self.user32.DrawIconEx(dc, 0, 0, handle, 32, 32, 0, None, 3):
                    return None
                self.gdi32.GdiFlush()
                bgra = ctypes.string_at(address, 32 * 32 * 4)
                for index in range(0, len(bgra), 4):
                    output.extend(bgra[index:index + 3])
            return bytes(output)
        finally:
            if previous:
                self.gdi32.SelectObject(dc, previous)
            if bitmap:
                self.gdi32.DeleteObject(bitmap)
            self.gdi32.DeleteDC(dc)

    @lru_cache(maxsize=64)
    def _classify(self, handle):
        pixels = self.render(handle)
        return matches_scroll_cursor(pixels, self.templates) if pixels else None

    def is_scroll(self):
        info = CursorInfo(cbSize=ctypes.sizeof(CursorInfo))
        if not self.templates or not self.user32.GetCursorInfo(ctypes.byref(info)):
            return None
        if not info.flags & 1 or not info.hCursor:
            return None
        return self._classify(info.hCursor)
