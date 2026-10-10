"""Run the action bound to a key. Everything is fire-and-forget except where a result is reported back."""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Callable

from .config import KeySpec

SHELL = os.environ.get("SHELL", "/bin/zsh")

# AppleScript key codes for keys that `keystroke` can't express
KEY_CODES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "backspace": 51, "escape": 53, "esc": 53,
    "forwarddelete": 117, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64,
    "f18": 79, "f19": 80, "f20": 90,
}
MODIFIERS = {
    "cmd": "command down", "command": "command down", "shift": "shift down",
    "alt": "option down", "opt": "option down", "option": "option down",
    "ctrl": "control down", "control": "control down", "fn": "function down",
}


def hotkey_script(combo: str) -> str:
    """'cmd+shift+4' -> AppleScript for System Events."""
    parts = [p.strip().lower() for p in re.split(r"\+", combo) if p.strip()]
    if not parts:
        raise ValueError("empty hotkey")
    *mods, key = parts
    bad = [m for m in mods if m not in MODIFIERS]
    if bad:
        raise ValueError(f"unknown modifier(s) {bad}; use cmd/shift/alt/ctrl/fn")
    using = f" using {{{', '.join(MODIFIERS[m] for m in mods)}}}" if mods else ""
    if key in KEY_CODES:
        return f'tell application "System Events" to key code {KEY_CODES[key]}{using}'
    if len(key) != 1:
        raise ValueError(f"unknown key {key!r}; use a single character or one of {sorted(KEY_CODES)}")
    return f'tell application "System Events" to keystroke "{_as_escape(key)}"{using}'


def _as_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _osascript(script: str) -> subprocess.Popen:
    return subprocess.Popen(["osascript", "-e", script], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _open_target(target: str) -> list[str]:
    t = os.path.expanduser(target)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", t) or t.startswith("mailto:"):
        return ["open", t]
    if t.endswith(".app") or Path(t).exists():
        return ["open", t]
    # bare app name, e.g. "Safari"
    return ["open", "-a", t]


def type_text(text: str, enter: bool = False) -> subprocess.Popen:
    """Type text by pasting it: robust for unicode, restores the clipboard afterwards."""
    press_return = 'delay 0.05\ntell application "System Events" to key code 36' if enter else ""
    script = f"""
set oldClip to the clipboard
set the clipboard to "{_as_escape(text)}"
tell application "System Events" to keystroke "v" using {{command down}}
{press_return}
delay 0.2
set the clipboard to oldClip
"""
    return _osascript(script)


def run_action(
    spec: KeySpec,
    *,
    on_page: Callable[[str], None],
    on_brightness: Callable[[str], None],
    on_result: Callable[[int], None] | None = None,
) -> subprocess.Popen | None:
    """Kick off the key's action. Returns the child process (if any) so the caller can watch it."""
    a, v = spec.action, spec.value
    if a is None:
        return None
    if a == "page":
        on_page(str(v))
        return None
    if a == "brightness":
        on_brightness(str(v))
        return None
    if a == "run":
        cwd = os.path.expanduser(spec.options.get("cwd", "~"))
        return subprocess.Popen(
            [SHELL, "-lc", str(v)],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    if a == "open":
        return subprocess.Popen(_open_target(str(v)), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if a == "shortcut":
        return subprocess.Popen(["shortcuts", "run", str(v)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    if a == "applescript":
        return _osascript(str(v))
    if a == "hotkey":
        return _osascript(hotkey_script(str(v)))
    if a == "text":
        return type_text(str(v), bool(spec.options.get("enter")))
    raise ValueError(f"unknown action {a}")


def describe(spec: KeySpec) -> str:
    if not spec.action:
        return "(no action — press is only reported to listeners)"
    v = spec.value
    if spec.action == "run":
        return f"run: {v}" + (f"  (cwd {spec.options['cwd']})" if "cwd" in spec.options else "")
    return f"{spec.action}: {shlex.quote(str(v)) if spec.action != 'page' else v}"
