Panning cursor templates from Chromium, retrieved 2026-10-11:
https://chromium.googlesource.com/chromium/src/+/main/ui/resources/cursors/

Mapping to Scroll cursor types:
https://chromium.googlesource.com/chromium/src/+/main/ui/base/win/win_cursor_factory.cc

The original BSD license is in LICENSE.chromium. The .cur files are unmodified.
They are rendered by Windows and compared against the global cursor from
GetCursorInfo. Comparing on black and white backgrounds preserves transparency
information. Unknown shapes are rejected; this does not detect actual page motion.
