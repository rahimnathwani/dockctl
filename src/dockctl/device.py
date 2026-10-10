"""HID driver for Mirabox/AJAZZ "293" family stream docks (AJAZZ AKP153, AKP153E, AKP153R).

Wire protocol (output reports, no report ID, so hidapi gets a leading 0x00):
    "CRT\\0\\0" + command bytes, zero padded to the packet size.
Key images are JPEG, announced with a BAT command and then streamed raw in
packet-sized chunks; STP makes the device show what it has received.
"""

from __future__ import annotations

import io
import os
import threading
import time
from dataclasses import dataclass

import hid
from PIL import Image

USAGE_PAGE = 0xFFA0
PREFIX = b"CRT\x00\x00"


@dataclass(frozen=True)
class Model:
    name: str
    vid: int
    pid: int
    packet: int  # output report payload: 512 on v1 firmware, 1024 on v3
    key_px: int  # main key image size
    release_events: bool  # v3 reports press and release; v1 only press
    tested: bool = False
    cols: int = 5
    rows: int = 3
    side_keys: int = 3  # the narrow column on the right, numbered 16-18 top to bottom
    side_px: int = 82
    rotate: int = 90  # the panel is mounted sideways: rotate 90° counter-clockwise before sending

    @property
    def lcd_keys(self) -> int:
        return self.cols * self.rows

    @property
    def total_keys(self) -> int:
        return self.lcd_keys + self.side_keys

    def position(self, key: int) -> tuple[int, int]:
        """1-based (row, col); the side column is col cols+1."""
        if key > self.lcd_keys:
            return key - self.lcd_keys, self.cols + 1
        r, c = divmod(key - 1, self.cols)
        return r + 1, c + 1

    def key_at(self, row: int, col: int) -> int:
        if not (1 <= row <= self.rows and 1 <= col <= self.cols + (1 if self.side_keys else 0)):
            raise ValueError(f"row,col must be within 1-{self.rows},1-{self.cols + (1 if self.side_keys else 0)}")
        if col == self.cols + 1:
            return self.lcd_keys + row
        return (row - 1) * self.cols + col

    def key_size(self, key: int) -> int:
        return self.side_px if key > self.lcd_keys else self.key_px

    def device_id(self, key: int) -> int:
        """Logical key (1-15 row-major from top-left, 16-18 side column) -> device key id."""
        if key > self.lcd_keys:
            return 0x10 + (key - self.lcd_keys - 1)
        r, c = divmod(key - 1, self.cols)
        return (self.cols - 1 - c) * self.rows + r + 1

    def logical_key(self, device_id: int) -> int | None:
        if 1 <= device_id <= self.lcd_keys:
            c = self.cols - 1 - (device_id - 1) // self.rows
            r = (device_id - 1) % self.rows
            return r * self.cols + c + 1
        if 0x10 <= device_id < 0x10 + self.side_keys:
            return self.lcd_keys + device_id - 0x10 + 1
        return None


# The 293 family numbers keys column by column from the top-right (the panel is
# mounted rotated). dockctl numbers them row-major from the top-left:
#
#     1  2  3  4  5  | 16
#     6  7  8  9 10  | 17
#    11 12 13 14 15  | 18
MODELS = {
    (0x0300, 0x3010): Model("AKP153E", 0x0300, 0x3010, 1024, 95, True, tested=True),
    (0x0300, 0x3011): Model("AKP153R", 0x0300, 0x3011, 1024, 95, True),
    (0x6603, 0x1014): Model("HSV293SV3", 0x6603, 0x1014, 1024, 95, True),
    (0x0300, 0x1010): Model("AKP153E", 0x0300, 0x1010, 512, 85, False, side_px=85),
    (0x0300, 0x1020): Model("AKP153R", 0x0300, 0x1020, 512, 85, False, side_px=85),
    (0x5548, 0x6674): Model("AKP153", 0x5548, 0x6674, 512, 85, False, side_px=85),
    (0x5548, 0x6670): Model("HSV293S", 0x5548, 0x6670, 512, 85, False, side_px=85),
}
DEFAULT_MODEL = MODELS[(0x0300, 0x3010)]


class DeviceError(RuntimeError):
    pass


@dataclass
class KeyEvent:
    key: int  # logical key number
    pressed: bool
    raw: bytes = b""


def find() -> list[dict]:
    """All supported docks currently plugged in (one entry per device)."""
    seen, out = set(), []
    for d in hid.enumerate(0, 0):
        if (d["vendor_id"], d["product_id"]) in MODELS and d["usage_page"] == USAGE_PAGE and d["usage"] == 1 \
                and d["path"] not in seen:
            seen.add(d["path"])
            out.append(d)
    return out


class Dock:
    def __init__(self, info: dict):
        self.info = info
        self.model = MODELS[(info["vendor_id"], info["product_id"])]
        self.serial = info.get("serial_number") or ""
        self._h = hid.device()
        self._lock = threading.RLock()
        self.firmware = ""
        self._open = False

    # ---- lifecycle -----------------------------------------------------
    @classmethod
    def open_first(cls, serial: str | None = None) -> "Dock":
        if os.environ.get("DOCKCTL_FAKE"):
            return FakeDock(DEFAULT_MODEL)
        devs = find()
        if serial:
            devs = [d for d in devs if d["serial_number"] == serial]
        if not devs:
            raise DeviceError("no AJAZZ/Mirabox dock found (is it plugged in?)")
        dock = cls(devs[0])
        dock.open()
        return dock

    def open(self, brightness: int | None = None) -> None:
        try:
            self._h.open_path(self.info["path"])
        except OSError as e:
            raise DeviceError(
                f"could not open {self.model.name}: {e}. Another program (the Stream Dock app or a "
                "running `dockctl daemon`) probably has it — quit it or use the daemon."
            ) from None
        self._open = True
        self._h.set_nonblocking(False)
        self.firmware = self._read_firmware()
        # Same start-up the vendor app does; CONNECT takes the device out of its idle "demo" mode.
        self.wake()
        if brightness is not None:
            self.brightness(brightness)
        self.clear_all()
        self.keepalive()

    def _read_firmware(self) -> str:
        try:
            raw = bytes(self._h.get_input_report(0, 513))
        except (OSError, ValueError):
            return ""
        text = raw.lstrip(b"\x00").split(b"\x00")[0].decode("ascii", errors="ignore")
        return text if text.isprintable() else ""

    def close(self, goodbye: bool = True) -> None:
        """Release the device. goodbye=True puts it back to its idle logo screen and sleeps the panel."""
        with self._lock:
            if not self._open:
                return
            try:
                if goodbye:
                    self.command(b"CLE\x00\x00\x44\x43")
                    self.sleep()
            except DeviceError:
                pass
            finally:
                self._h.close()
                self._open = False

    def __enter__(self) -> "Dock":
        return self

    def __exit__(self, *exc) -> None:
        self.close(goodbye=False)

    # ---- low level -----------------------------------------------------
    def _write(self, payload: bytes) -> None:
        n = self.model.packet
        if len(payload) > n:
            raise ValueError("payload larger than packet")
        with self._lock:
            try:
                written = self._h.write(b"\x00" + payload + b"\x00" * (n - len(payload)))
            except (OSError, ValueError) as e:
                raise DeviceError(f"write failed: {e}") from None
        if written < 0:
            raise DeviceError(f"write failed: {self._h.error()}")

    def command(self, body: bytes) -> None:
        self._write(PREFIX + body)

    # ---- commands ------------------------------------------------------
    def wake(self) -> None:
        self.command(b"DIS")

    def keepalive(self) -> None:
        self.command(b"CONNECT")

    def brightness(self, percent: int) -> None:
        self.command(b"LIG\x00\x00" + bytes([max(0, min(100, int(percent)))]))

    def flush(self) -> None:
        self.command(b"STP")

    def sleep(self) -> None:
        self.command(b"HAN")

    def clear_key(self, key: int) -> None:
        self.command(b"CLE\x00\x00\x00" + bytes([self.model.device_id(key)]))
        self.flush()

    def clear_all(self) -> None:
        self.command(b"CLE\x00\x00\x00\xff")
        self.flush()

    def encode(self, img: Image.Image, key: int = 1) -> bytes:
        m = self.model
        px = m.key_size(key)
        img = img.convert("RGB")
        if img.size != (px, px):
            img = img.resize((px, px), Image.LANCZOS)
        if m.rotate:
            img = img.rotate(m.rotate, expand=True)
        for quality in (92, 85, 75, 60):  # stay under ~10 KB per key, which every firmware accepts
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=quality, subsampling=0 if quality >= 85 else 2)
            if buf.tell() <= 10240:
                break
        return buf.getvalue()

    def set_key_jpeg(self, key: int, jpeg: bytes, flush: bool = True) -> None:
        m = self.model
        if not 1 <= key <= m.total_keys:
            raise ValueError(f"key must be 1-{m.total_keys}")
        with self._lock:
            self.command(b"BAT" + len(jpeg).to_bytes(4, "big") + bytes([m.device_id(key)]))
            for i in range(0, len(jpeg), m.packet):
                self._write(jpeg[i : i + m.packet])
            if flush:
                self.flush()

    def set_key_image(self, key: int, img: Image.Image, flush: bool = True) -> None:
        self.set_key_jpeg(key, self.encode(img, key), flush)

    # ---- input ---------------------------------------------------------
    def read_event(self, timeout_ms: int = 1000) -> KeyEvent | None:
        """Block up to timeout_ms for a key report. Returns None on timeout or non-key reports."""
        try:
            data = bytes(self._h.read(512, timeout_ms))
        except (OSError, ValueError) as e:
            raise DeviceError(f"read failed: {e}") from None
        if not data:
            return None
        return self.parse_input(data)

    def parse_input(self, data: bytes) -> KeyEvent | None:
        if len(data) < 11 or not data.startswith(b"ACK"):
            return None
        dev_id, state = data[9], data[10]
        if dev_id in (0x00, 0xFF):  # 0xFF acknowledges a write, 0x00 is "no key"
            return None
        key = self.model.logical_key(dev_id)
        if key is None:
            return None
        pressed = state == 0x01 or not self.model.release_events  # v1 firmware only reports presses
        return KeyEvent(key=key, pressed=pressed, raw=data[:16])


class FakeDock(Dock):
    """Stands in for hardware when DOCKCTL_FAKE=1: accepts everything, never reports presses."""

    def __init__(self, model: Model):
        self.info = {"vendor_id": model.vid, "product_id": model.pid, "serial_number": "FAKE"}
        self.model = model
        self.serial = "FAKE"
        self.firmware = "fake"
        self._lock = threading.RLock()
        self.writes = 0

    def open(self, brightness: int | None = None) -> None:
        pass

    def close(self, goodbye: bool = True) -> None:
        pass

    def _write(self, payload: bytes) -> None:
        self.writes += 1

    def read_event(self, timeout_ms: int = 1000) -> KeyEvent | None:
        time.sleep(timeout_ms / 1000)
        return None
