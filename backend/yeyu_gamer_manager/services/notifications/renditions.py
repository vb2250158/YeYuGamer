"""Mail-size renditions of verified screenshot artifacts.

The seal resolver verifies the original artifact bytes (hash, size, path,
window).  A 2560x1440 PNG game frame is 1-4 MB, so a seven-game round cannot
fit its evidence into one SMTP message.  This module produces a bounded JPEG
rendition of an already-verified frame for the wire only; the artifact on disk
and the seal reference stay untouched.

Only GDI+ (shipped with Windows) is used, through ctypes, so the packaged
Manager does not need an imaging dependency.  Any failure returns ``None`` and
the caller falls back to the original bytes or omits the frame.
"""

from __future__ import annotations

import ctypes
import os
import tempfile
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class MailRendition:
    content: bytes
    content_type: str
    width: int
    height: int


_JPEG_ENCODER_CLSID = (
    0x557CF401,
    0x1A04,
    0x11D3,
    (0x9A, 0x73, 0x00, 0x00, 0xF8, 0x1E, 0xF3, 0x2E),
)
_ENCODER_QUALITY_GUID = (
    0x1D5BE4B5,
    0xFA4A,
    0x452D,
    (0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB),
)
_ENCODER_PARAMETER_VALUE_TYPE_LONG = 4
_PIXEL_FORMAT_24BPP_RGB = 0x00021808
_INTERPOLATION_HIGH_QUALITY_BICUBIC = 7
_UNIT_PIXEL = 2


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def build(cls, parts: tuple) -> "_GUID":
        value = cls()
        value.Data1, value.Data2, value.Data3 = parts[0], parts[1], parts[2]
        value.Data4 = (ctypes.c_ubyte * 8)(*parts[3])
        return value


class _GdiplusStartupInput(ctypes.Structure):
    _fields_ = [
        ("GdiplusVersion", ctypes.c_uint32),
        ("DebugEventCallback", ctypes.c_void_p),
        ("SuppressBackgroundThread", wintypes.BOOL),
        ("SuppressExternalCodecs", wintypes.BOOL),
    ]


class _EncoderParameter(ctypes.Structure):
    _fields_ = [
        ("Guid", _GUID),
        ("NumberOfValues", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
        ("Value", ctypes.c_void_p),
    ]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncoderParameter * 1)]


def _target_size(width: int, height: int, max_width: int, max_height: int) -> tuple[int, int]:
    if width <= 0 or height <= 0:
        raise ValueError("invalid source dimensions")
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def render_mail_image(
    source_path: Path,
    *,
    max_width: int = 1600,
    max_height: int = 900,
    quality: int = 82,
) -> MailRendition | None:
    """Return a bounded JPEG rendition of a verified PNG/JPEG frame, or None."""

    if os.name != "nt":
        return None
    if not 1 <= quality <= 100:
        raise ValueError("quality must be within 1..100")
    try:
        gdiplus = ctypes.WinDLL("gdiplus")
    except OSError:
        return None

    gdiplus.GdiplusStartup.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(_GdiplusStartupInput),
        ctypes.c_void_p,
    ]
    gdiplus.GdiplusStartup.restype = ctypes.c_int
    gdiplus.GdiplusShutdown.argtypes = [ctypes.c_void_p]
    gdiplus.GdipCreateBitmapFromFile.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipCreateBitmapFromFile.restype = ctypes.c_int
    gdiplus.GdipGetImageWidth.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gdiplus.GdipGetImageHeight.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
    gdiplus.GdipCreateBitmapFromScan0.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    gdiplus.GdipGetImageGraphicsContext.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    gdiplus.GdipSetInterpolationMode.argtypes = [ctypes.c_void_p, ctypes.c_int]
    gdiplus.GdipGraphicsClear.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    gdiplus.GdipDrawImageRectI.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
    ]
    gdiplus.GdipSaveImageToFile.argtypes = [
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(_EncoderParameters),
    ]
    gdiplus.GdipSaveImageToFile.restype = ctypes.c_int
    gdiplus.GdipDeleteGraphics.argtypes = [ctypes.c_void_p]
    gdiplus.GdipDisposeImage.argtypes = [ctypes.c_void_p]

    token = ctypes.c_void_p()
    startup = _GdiplusStartupInput(1, None, False, False)
    if gdiplus.GdiplusStartup(ctypes.byref(token), ctypes.byref(startup), None) != 0:
        return None
    source = ctypes.c_void_p()
    target = ctypes.c_void_p()
    graphics = ctypes.c_void_p()
    output_path: str | None = None
    try:
        if gdiplus.GdipCreateBitmapFromFile(str(source_path), ctypes.byref(source)) != 0:
            return None
        width = ctypes.c_uint()
        height = ctypes.c_uint()
        gdiplus.GdipGetImageWidth(source, ctypes.byref(width))
        gdiplus.GdipGetImageHeight(source, ctypes.byref(height))
        target_width, target_height = _target_size(
            int(width.value), int(height.value), max_width, max_height
        )
        if (
            gdiplus.GdipCreateBitmapFromScan0(
                target_width,
                target_height,
                0,
                _PIXEL_FORMAT_24BPP_RGB,
                None,
                ctypes.byref(target),
            )
            != 0
        ):
            return None
        if gdiplus.GdipGetImageGraphicsContext(target, ctypes.byref(graphics)) != 0:
            return None
        gdiplus.GdipSetInterpolationMode(graphics, _INTERPOLATION_HIGH_QUALITY_BICUBIC)
        gdiplus.GdipGraphicsClear(graphics, 0xFF000000)
        if (
            gdiplus.GdipDrawImageRectI(
                graphics, source, 0, 0, target_width, target_height
            )
            != 0
        ):
            return None

        quality_value = ctypes.c_long(quality)
        parameters = _EncoderParameters()
        parameters.Count = 1
        parameters.Parameter[0].Guid = _GUID.build(_ENCODER_QUALITY_GUID)
        parameters.Parameter[0].NumberOfValues = 1
        parameters.Parameter[0].Type = _ENCODER_PARAMETER_VALUE_TYPE_LONG
        parameters.Parameter[0].Value = ctypes.cast(
            ctypes.pointer(quality_value), ctypes.c_void_p
        )
        encoder = _GUID.build(_JPEG_ENCODER_CLSID)
        handle, output_path = tempfile.mkstemp(prefix="yeyu-mail-", suffix=".jpg")
        os.close(handle)
        if (
            gdiplus.GdipSaveImageToFile(
                target, output_path, ctypes.byref(encoder), ctypes.byref(parameters)
            )
            != 0
        ):
            return None
        content = Path(output_path).read_bytes()
        if not content.startswith(b"\xff\xd8\xff"):
            return None
        return MailRendition(content, "image/jpeg", target_width, target_height)
    except Exception:
        return None
    finally:
        if graphics:
            gdiplus.GdipDeleteGraphics(graphics)
        if target:
            gdiplus.GdipDisposeImage(target)
        if source:
            gdiplus.GdipDisposeImage(source)
        gdiplus.GdiplusShutdown(token)
        if output_path:
            try:
                os.unlink(output_path)
            except OSError:
                pass
