"""Icon asset and Windows taskbar identity.

The icon is a committed binary produced by ``tools/make_icon.ps1``. These tests
guard the properties the app depends on, which a regeneration could silently
break.
"""

from __future__ import annotations

import struct
import sys

import pytest

from transcriptor_prime import (
    APP_USER_MODEL_ID,
    ICON_PATH,
    LOGO_PATH,
    __version__,
)

# Sizes written by tools/make_icon.ps1. 16-48 cover the taskbar and Explorer at
# 100-200% scaling; 128 and 256 cover the large icon views.
EXPECTED_SIZES = {16, 20, 24, 32, 40, 48, 64, 128, 256}

#: Entries at or above this size are PNG-compressed; smaller ones are BMP.
PNG_FROM = 128

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def read_icon_directory(path):
    """Parse an .ico into a list of (width, height, bpp, payload) tuples."""
    data = path.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", data, 0)
    assert reserved == 0, "malformed .ico header"
    assert kind == 1, f"expected an icon (type 1), got type {kind}"

    entries = []
    for index in range(count):
        offset = 6 + (16 * index)
        width, height, _colors, _res, _planes, bpp, size, data_offset = struct.unpack_from(
            "<BBBBHHII", data, offset
        )
        payload = data[data_offset : data_offset + size]
        assert len(payload) == size, f"entry {index} is truncated"
        entries.append((width or 256, height or 256, bpp, payload))
    return entries


class TestIconFile:
    def test_icon_is_committed(self):
        assert ICON_PATH.exists(), (
            f"{ICON_PATH} is missing - run tools/make_icon.ps1 to regenerate it"
        )

    def test_contains_every_expected_size(self):
        sizes = {width for width, _h, _bpp, _p in read_icon_directory(ICON_PATH)}
        assert sizes == EXPECTED_SIZES

    def test_entries_are_square_and_32_bit(self):
        for width, height, bpp, _payload in read_icon_directory(ICON_PATH):
            assert width == height, f"{width}x{height} entry is not square"
            assert bpp == 32, f"{width}px entry is {bpp}-bit, expected 32"

    def test_small_entries_are_bmp_and_large_ones_are_png(self):
        """The conventional Windows split, and what keeps the file small.

        Every shell surface reads BMP entries, so the sizes that actually get
        used day to day stay BMP. Storing 128 and 256 as PNG instead is what
        takes the file from roughly 370 KB down to under 60 KB.
        """
        for width, _h, _bpp, payload in read_icon_directory(ICON_PATH):
            if width >= PNG_FROM:
                assert payload.startswith(PNG_SIGNATURE), f"{width}px should be PNG"
            else:
                assert not payload.startswith(PNG_SIGNATURE), f"{width}px should be BMP"
                header_size = struct.unpack_from("<I", payload, 0)[0]
                assert header_size == 40, (
                    f"{width}px entry has a {header_size}-byte header, "
                    "expected a 40-byte BITMAPINFOHEADER"
                )

    def test_bmp_height_is_doubled_for_the_and_mask(self):
        """An ICO's DIB stores colour data and mask stacked, so height is 2x."""
        for width, height, _bpp, payload in read_icon_directory(ICON_PATH):
            if width >= PNG_FROM:
                continue
            dib_width, dib_height = struct.unpack_from("<ii", payload, 4)
            assert dib_width == width
            assert dib_height == height * 2

    def test_file_stays_small(self):
        """Guards against a regeneration that writes every entry as BMP."""
        size_kb = ICON_PATH.stat().st_size / 1024
        assert size_kb < 100, f"icon has grown to {size_kb:.0f} KB"

    def test_logo_png_is_committed(self):
        assert LOGO_PATH.exists()
        assert LOGO_PATH.read_bytes().startswith(PNG_SIGNATURE)


class TestTaskbarIdentity:
    def test_app_user_model_id_excludes_the_version(self):
        """Windows keys pinned taskbar buttons off this string.

        Embedding the version would orphan a user's pinned icon on every
        upgrade.
        """
        assert __version__ not in APP_USER_MODEL_ID

    def test_app_user_model_id_is_a_dotted_identifier(self):
        assert "." in APP_USER_MODEL_ID
        assert " " not in APP_USER_MODEL_ID
        # Windows rejects anything longer than 128 characters.
        assert 0 < len(APP_USER_MODEL_ID) <= 128

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only shell API")
    def test_setting_the_id_takes_effect_on_this_process(self):
        import ctypes

        from transcriptor_prime.app import _set_app_user_model_id

        _set_app_user_model_id()

        buffer = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.GetCurrentProcessExplicitAppUserModelID(
            ctypes.byref(buffer)
        )
        assert result == 0, f"GetCurrentProcessExplicitAppUserModelID failed: {result}"
        try:
            assert buffer.value == APP_USER_MODEL_ID
        finally:
            ctypes.windll.ole32.CoTaskMemFree(buffer)

    def test_setting_the_id_is_a_no_op_off_windows(self, monkeypatch):
        from transcriptor_prime.app import _set_app_user_model_id

        monkeypatch.setattr("transcriptor_prime.app.sys.platform", "linux")
        _set_app_user_model_id()  # must not raise
