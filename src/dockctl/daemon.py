"""The long-running process that owns the dock.

One device thread does all HID I/O (reads key events, pushes changed key images);
everything else (control socket, pollers, actions) only mutates state and marks
the display dirty. Clients talk JSON lines over a Unix socket.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import os
import signal
import socket
import subprocess
import threading
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from . import actions
from .config import CONFIG_DIR, CONFIG_PATH, Config, ConfigError, KeySpec
from .device import DeviceError, Dock, KeyEvent, Model
from .render import Face, render_face

SOCKET_PATH = Path(os.environ.get("DOCKCTL_SOCKET", CONFIG_DIR / "daemon.sock"))
LOG_PATH = CONFIG_DIR / "daemon.log"
KEEPALIVE_S = 10
RECONNECT_S = 2

log = logging.getLogger("dockctl")


@dataclass
class Overlay:
    face: Face
    expires: float | None = None  # monotonic deadline
    sticky: bool = False  # keep after a press
    tag: str = ""  # who put it there (e.g. a prompt id)


class Daemon:
    def __init__(self, config_path: Path = CONFIG_PATH, serial: str | None = None):
        self.config_path = config_path
        self.serial = serial
        self.lock = threading.RLock()
        self.dirty = threading.Event()
        self.stop = threading.Event()
        self.started = time.time()

        self.cfg: Config | None = None
        self.cfg_error = ""
        self.cfg_mtime = 0.0
        self.page = ""
        self.history: list[str] = []

        self.dock: Dock | None = None
        self.model: Model | None = None
        self.device_error = ""
        self.sent: dict[int, str] = {}  # key -> hash of what is on screen
        self.brightness: int | None = None
        self.brightness_sent: int | None = None

        self.overlays: dict[int, list[Overlay]] = {}  # per key, a stack: the last one is shown
        self.flashes: dict[int, tuple[str, float]] = {}
        self.polled: dict[tuple[str, int], dict[str, Any]] = {}
        self.poll_due: dict[tuple[str, int], float] = {}
        self.poll_running: set[tuple[str, int]] = set()

        self.subscribers: list[socket.socket] = []
        self.waiters: list[tuple[set[int] | None, threading.Event, list]] = []
        self.press_count = 0
        self.down: dict[int, dict[str, Any]] = {}  # key -> the press event, so its release reports the same page/label
        self.last_event: dict[str, Any] | None = None

    # ---- config --------------------------------------------------------
    def load_config(self, force: bool = False) -> None:
        try:
            mtime = self.config_path.stat().st_mtime
        except FileNotFoundError:
            mtime = 0.0
        if not force and mtime == self.cfg_mtime:
            return
        self.cfg_mtime = mtime
        try:
            cfg = Config(self.config_path, strict=True)
        except (ConfigError, Exception) as e:  # keep running on the last good config
            self.cfg_error = str(e)
            log.error("config error: %s", e)
            return
        with self.lock:
            first = self.cfg is None
            self.cfg = cfg
            self.cfg_error = ""
            if first or self.page not in cfg.pages():
                self.page = cfg.start_page
            self.brightness = cfg.brightness
            self.poll_due.clear()
            self.polled = {k: v for k, v in self.polled.items() if k[0] in cfg.pages()}
        log.info("config loaded (%d pages)", len(cfg.pages()))
        self.dirty.set()

    # ---- what each key should look like right now ----------------------
    def top_overlay(self, key: int) -> Overlay | None:
        stack = self.overlays.get(key)
        return stack[-1] if stack else None

    def set_stack(self, key: int, stack: list[Overlay]) -> None:
        if stack:
            self.overlays[key] = stack
        else:
            self.overlays.pop(key, None)

    def face_for(self, key: int) -> Face | None:
        with self.lock:
            ov = self.top_overlay(key)
            if ov:
                return ov.face
            spec = self.cfg.keys(self.page).get(key) if self.cfg else None
            if spec is None:
                return None
            face = spec.face
            dyn = self.polled.get((self.page, key))
            if dyn:
                face = replace(face, **{k: str(v) for k, v in dyn.items() if k in Face.__dataclass_fields__})
            fl = self.flashes.get(key)
            if fl:
                face = replace(face, border=fl[0])
            return face

    def snapshot(self) -> dict[str, Any]:
        n = self.model.total_keys if self.model else 18
        keys = {}
        for k in range(1, n + 1):
            f = self.face_for(k)
            spec = self.cfg.keys(self.page).get(k) if self.cfg else None
            entry: dict[str, Any] = {}
            if f:
                entry["face"] = asdict(f)
            if spec and spec.action:
                entry["action"] = {spec.action: spec.value}
            if k in self.overlays:
                entry["overlay"] = True
            if entry:
                keys[str(k)] = entry
        return {"page": self.page, "keys": keys}

    # ---- device thread -------------------------------------------------
    def connect(self) -> bool:
        try:
            self.dock = Dock.open_first(self.serial)
        except DeviceError as e:
            if str(e) != self.device_error:
                log.warning("%s", e)
            self.device_error = str(e)
            self.dock = None
            return False
        self.model = self.dock.model
        self.device_error = ""
        self.sent.clear()
        self.brightness_sent = None
        log.info("connected %s serial=%s firmware=%s", self.model.name, self.dock.serial, self.dock.firmware)
        self.broadcast({"event": "connected", "model": self.model.name, "serial": self.dock.serial})
        self.dirty.set()
        return True

    def disconnect(self, why: str) -> None:
        if self.dock:
            log.warning("device lost: %s", why)
            try:
                self.dock.close()
            except Exception:
                pass
        self.dock = None
        self.device_error = why
        self.broadcast({"event": "disconnected", "reason": why})

    def push(self) -> None:
        """Send every key whose face changed since the last push."""
        assert self.dock and self.model
        if self.brightness is not None and self.brightness != self.brightness_sent:
            self.dock.brightness(self.brightness)
            self.brightness_sent = self.brightness
        base = self.config_path.parent
        changed = False
        for key in range(1, self.model.total_keys + 1):
            px = self.model.key_size(key)
            face = self.face_for(key)
            sig = hashlib.sha1(repr(face).encode()).hexdigest()
            if self.sent.get(key) == sig:
                continue
            if face is None:
                img = render_face(Face(bg="#000000"), px)
            else:
                try:
                    img = render_face(face, px, base)
                except Exception as e:  # bad icon path etc: show the error on the key
                    log.error("key %d: %s", key, e)
                    img = render_face(Face(label="⚠ " + str(e)[:40], bg="#7f1d1d", font_size=10), px)
            self.dock.set_key_image(key, img, flush=False)
            self.sent[key] = sig
            changed = True
        if changed:
            self.dock.flush()

    def device_loop(self) -> None:
        last_keepalive = 0.0
        last_attempt = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            self.load_config()
            self.expire(now)
            self.schedule_polls(now)
            if self.dock is None:
                if now - last_attempt >= RECONNECT_S:
                    last_attempt = now
                    self.connect()
                if self.dock is None:
                    self.stop.wait(0.25)
                    continue
            try:
                if self.dirty.is_set():
                    self.dirty.clear()
                    self.push()
                if now - last_keepalive >= KEEPALIVE_S:
                    self.dock.keepalive()
                    last_keepalive = now
                ev = self.dock.read_event(timeout_ms=50)
                if ev:
                    self.on_key(ev)
            except (DeviceError, OSError, ValueError) as e:
                self.disconnect(str(e))
        if self.dock:
            self.dock.close()

    def expire(self, now: float) -> None:
        with self.lock:
            for k, stack in list(self.overlays.items()):
                live = [ov for ov in stack if ov.expires is None or ov.expires > now]
                if len(live) != len(stack):
                    self.set_stack(k, live)
                    self.dirty.set()
            for k, (_, until) in list(self.flashes.items()):
                if until <= now:
                    del self.flashes[k]
                    self.dirty.set()

    # ---- key handling --------------------------------------------------
    def on_key(self, ev: KeyEvent) -> None:
        if not ev.pressed and ev.key in self.down:
            evt = {**self.down.pop(ev.key), "event": "release", "ts": time.time()}
            self.last_event = evt
            self.broadcast(evt)
            return
        with self.lock:
            page = self.page
            spec = self.cfg.keys(page).get(ev.key) if self.cfg else None
            ov = self.top_overlay(ev.key)
        label = (ov.face.label if ov else spec.face.label if spec else "") or ""
        evt = {"event": "press" if ev.pressed else "release", "key": ev.key, "page": page, "label": label, "ts": time.time()}
        if ov:
            evt["overlay"] = ov.tag or True
        if spec and spec.action and not ov:
            evt["action"] = {spec.action: spec.value}
        if ev.pressed:
            self.press_count += 1
            self.down[ev.key] = evt
        self.last_event = evt
        self.broadcast(evt)
        self.notify_waiters(evt)
        if not ev.pressed:
            return
        if ov:
            if not ov.sticky:
                with self.lock:
                    self.set_stack(ev.key, self.overlays.get(ev.key, [])[:-1])
                self.dirty.set()
            return
        if spec:
            self.trigger(ev.key, spec)

    def trigger(self, key: int, spec: KeySpec) -> None:
        try:
            proc = actions.run_action(spec, on_page=self.goto_page, on_brightness=self.adjust_brightness)
        except Exception as e:
            log.error("key %d action failed: %s", key, e)
            self.flash(key, "#dc2626")
            return
        log.info("key %d -> %s", key, actions.describe(spec))
        if proc is not None and (spec.options.get("show_result") or spec.action != "run"):
            threading.Thread(target=self._watch, args=(key, proc, bool(spec.options.get("show_result"))), daemon=True).start()

    def _watch(self, key: int, proc: subprocess.Popen, show: bool) -> None:
        rc = proc.wait()
        err = proc.stderr.read().decode(errors="ignore").strip() if proc.stderr else ""
        if rc != 0:
            log.error("key %d action exited %d %s", key, rc, err[:300])
        if show or rc != 0:
            self.flash(key, "#16a34a" if rc == 0 else "#dc2626")
        self.broadcast({"event": "result", "key": key, "exit": rc})

    def flash(self, key: int, color: str, seconds: float = 1.5) -> None:
        with self.lock:
            self.flashes[key] = (color, time.monotonic() + seconds)
        self.dirty.set()

    def goto_page(self, target: str) -> None:
        with self.lock:
            if not self.cfg:
                return
            pages = self.cfg.pages()
            if target == "back":
                new = self.history.pop() if self.history else self.cfg.start_page
            elif target in ("next", "prev"):
                i = pages.index(self.page) if self.page in pages else 0
                new = pages[(i + (1 if target == "next" else -1)) % len(pages)]
            elif target in pages:
                new = target
            else:
                raise ValueError(f"no page {target!r}")
            if new != self.page and target != "back":
                self.history = (self.history + [self.page])[-20:]
            self.page = new
            for k in list(self.poll_due):
                if k[0] == new:
                    self.poll_due[k] = 0
        self.broadcast({"event": "page", "page": new})
        self.dirty.set()

    def adjust_brightness(self, value: str) -> None:
        with self.lock:
            cur = self.brightness if self.brightness is not None else 70
            v = str(value).strip()
            self.brightness = max(0, min(100, cur + int(v) if v[0] in "+-" else int(v)))
        self.dirty.set()

    # ---- polled (dynamic) keys ----------------------------------------
    def schedule_polls(self, now: float) -> None:
        with self.lock:
            if not self.cfg:
                return
            for key, spec in self.cfg.keys(self.page).items():
                cmd = spec.options.get("poll")
                if not cmd:
                    continue
                k = (self.page, key)
                if k in self.poll_running or self.poll_due.get(k, 0) > now:
                    continue
                self.poll_due[k] = now + float(spec.options.get("every", 60))
                self.poll_running.add(k)
                threading.Thread(target=self._poll, args=(k, cmd, spec.options.get("cwd", "~")), daemon=True).start()

    def _poll(self, k: tuple[str, int], cmd: str, cwd: str) -> None:
        try:
            r = subprocess.run(
                [actions.SHELL, "-lc", cmd], cwd=os.path.expanduser(cwd), capture_output=True, text=True, timeout=30
            )
            out = r.stdout.strip()
            if out.startswith("{"):
                try:
                    vals = json.loads(out)
                except json.JSONDecodeError:
                    vals = {"label": out}
            else:
                vals = {"label": out}
            if r.returncode != 0:
                vals.setdefault("border", "#dc2626")
        except subprocess.TimeoutExpired:
            vals = {"label": "timeout", "border": "#dc2626"}
        with self.lock:
            changed = self.polled.get(k) != vals
            self.polled[k] = vals
            self.poll_running.discard(k)
        if changed and k[0] == self.page:
            self.dirty.set()

    # ---- pub/sub -------------------------------------------------------
    def broadcast(self, evt: dict[str, Any]) -> None:
        line = (json.dumps(evt) + "\n").encode()
        with self.lock:
            subs = list(self.subscribers)
        for s in subs:
            try:
                s.sendall(line)
            except OSError:
                with self.lock:
                    if s in self.subscribers:
                        self.subscribers.remove(s)

    def notify_waiters(self, evt: dict[str, Any]) -> None:
        if evt["event"] != "press":
            return
        with self.lock:
            for keys, done, box in list(self.waiters):
                if keys is None or evt["key"] in keys:
                    box.append(evt)
                    done.set()
                    self.waiters.remove((keys, done, box))

    # ---- control socket ------------------------------------------------
    @staticmethod
    def accessibility() -> bool | None:
        """Whether keystroke actions (text, hotkey) are allowed for this process's launcher."""
        try:
            ax = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            ax.AXIsProcessTrusted.restype = ctypes.c_bool
            return bool(ax.AXIsProcessTrusted())
        except OSError:
            return None

    def status(self) -> dict[str, Any]:
        m = self.model
        return {
            "ok": True,
            "pid": os.getpid(),
            "uptime_s": int(time.time() - self.started),
            "connected": self.dock is not None,
            "device": {
                "model": m.name, "serial": self.dock.serial if self.dock else None,
                "firmware": self.dock.firmware if self.dock else None,
                "keys": m.total_keys, "cols": m.cols, "rows": m.rows, "side_keys": m.side_keys,
                "key_px": m.key_px, "side_px": m.side_px,
            } if m else None,
            "device_error": self.device_error or None,
            "config": str(self.config_path),
            "config_error": self.cfg_error or None,
            "page": self.page,
            "pages": self.cfg.pages() if self.cfg else [],
            "brightness": self.brightness,
            "overlays": sorted(self.overlays),
            "presses": self.press_count,
            "accessibility": self.accessibility(),
            "last_event": self.last_event,
        }

    def handle(self, req: dict[str, Any], conn: socket.socket) -> dict[str, Any] | None:
        cmd = req.get("cmd")
        if cmd == "status":
            return self.status()
        if cmd == "snapshot":
            return {"ok": True, **self.snapshot()}
        if cmd == "reload":
            self.load_config(force=True)
            with self.lock:
                self.sent.clear()
            self.dirty.set()
            return {"ok": not self.cfg_error, "error": self.cfg_error or None}
        if cmd == "page":
            self.goto_page(req["name"])
            return {"ok": True, "page": self.page}
        if cmd == "brightness":
            self.adjust_brightness(str(req["value"]))
            return {"ok": True, "brightness": self.brightness}
        if cmd == "overlay":
            ttl = req.get("ttl")
            face_fields = {k: v for k, v in req.get("face", {}).items() if k in Face.__dataclass_fields__}
            with self.lock:
                for key in req["keys"]:
                    base = self.face_for(int(key)) if req.get("merge") else None
                    face = replace(base, **face_fields) if base else Face(**face_fields)
                    self.overlays.setdefault(int(key), []).append(Overlay(
                        face=face,
                        expires=time.monotonic() + float(ttl) if ttl else None,
                        sticky=bool(req.get("sticky")),
                        tag=str(req.get("tag", "")),
                    ))
            self.dirty.set()
            return {"ok": True}
        if cmd == "clear_overlay":
            with self.lock:
                keys = req.get("keys") or list(self.overlays)
                tag = req.get("tag")
                for k in keys:
                    stack = self.overlays.get(int(k), [])
                    self.set_stack(int(k), [ov for ov in stack if tag and ov.tag != tag])
            self.dirty.set()
            return {"ok": True}
        if cmd == "press":  # simulate a physical press (testing / scripting)
            self.on_key(KeyEvent(key=int(req["key"]), pressed=True))
            return {"ok": True}
        if cmd == "wait":
            keys = set(int(k) for k in req["keys"]) if req.get("keys") else None
            done, box = threading.Event(), []
            with self.lock:
                self.waiters.append((keys, done, box))
            timeout = req.get("timeout")
            if not done.wait(float(timeout) if timeout else None):
                with self.lock:
                    if (keys, done, box) in self.waiters:
                        self.waiters.remove((keys, done, box))
                return {"ok": False, "error": "timeout"}
            return {"ok": True, **box[0]}
        if cmd == "subscribe":
            conn.sendall(b'{"ok": true, "subscribed": true}\n')
            with self.lock:
                self.subscribers.append(conn)
            return None  # connection stays open, broadcast() writes to it
        if cmd == "refresh":
            with self.lock:
                self.sent.clear()
            self.dirty.set()
            return {"ok": True}
        if cmd == "shutdown":
            self.stop.set()
            return {"ok": True}
        return {"ok": False, "error": f"unknown command {cmd!r}"}

    def serve_client(self, conn: socket.socket) -> None:
        try:
            f = conn.makefile("rb")
            line = f.readline()
            if not line:
                return
            try:
                req = json.loads(line)
                resp = self.handle(req, conn)
            except (KeyError, ValueError, TypeError, ConfigError) as e:
                resp = {"ok": False, "error": str(e)}
            if resp is None:
                # hold the thread until the subscriber goes away
                while not self.stop.is_set() and f.readline():
                    pass
                return
            conn.sendall((json.dumps(resp) + "\n").encode())
        except OSError:
            pass
        finally:
            with self.lock:
                if conn in self.subscribers:
                    self.subscribers.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    def socket_loop(self) -> None:
        SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
        if SOCKET_PATH.exists():
            SOCKET_PATH.unlink()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(SOCKET_PATH))
        os.chmod(SOCKET_PATH, 0o600)
        srv.listen(16)
        srv.settimeout(0.5)
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()
        srv.close()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass

    def run(self) -> None:
        self.load_config(force=True)
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        signal.signal(signal.SIGINT, lambda *_: self.stop.set())
        signal.signal(signal.SIGHUP, lambda *_: self.load_config(force=True))
        threading.Thread(target=self.socket_loop, daemon=True).start()
        log.info("dockctl daemon started (pid %d, socket %s)", os.getpid(), SOCKET_PATH)
        self.device_loop()
        log.info("dockctl daemon stopped")


def main(config_path: Path = CONFIG_PATH, serial: str | None = None, verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.getLogger("PIL").setLevel(logging.WARNING)
    if socket_alive():
        raise SystemExit("a dockctl daemon is already running (see `dockctl status`)")
    Daemon(config_path, serial).run()


def socket_alive() -> bool:
    if not SOCKET_PATH.exists():
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.connect(str(SOCKET_PATH))
        return True
    except OSError:
        return False
    finally:
        s.close()
