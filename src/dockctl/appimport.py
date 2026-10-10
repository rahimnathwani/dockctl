"""Import layouts from the vendor Stream Dock app's profiles (~/Library/Application Support/HotSpot/StreamDock)."""

from __future__ import annotations

import configparser
import json
import shutil
from pathlib import Path
from typing import Any

from .config import Config

APP_DATA = Path.home() / "Library" / "Application Support" / "HotSpot" / "StreamDock"
COLS = 5

# macOS virtual key codes (ANSI) -> names dockctl's hotkey understands
VK = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z", 7: "x", 8: "c", 9: "v", 11: "b", 12: "q", 13: "w",
    14: "e", 15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3", 21: "4", 22: "6", 23: "5", 24: "=", 25: "9",
    26: "7", 27: "-", 28: "8", 29: "0", 30: "]", 31: "o", 32: "u", 33: "[", 34: "i", 35: "p", 36: "return",
    37: "l", 38: "j", 39: "'", 40: "k", 41: ";", 42: "\\", 43: ",", 44: "/", 45: "n", 46: "m", 47: ".",
    48: "tab", 49: "space", 50: "`", 51: "delete", 53: "escape", 96: "f5", 97: "f6", 98: "f7", 99: "f3",
    100: "f8", 101: "f9", 103: "f11", 109: "f10", 111: "f12", 118: "f4", 120: "f2", 122: "f1",
    115: "home", 116: "pageup", 117: "forwarddelete", 119: "end", 121: "pagedown",
    123: "left", 124: "right", 125: "down", 126: "up",
}


def _active_profile_names() -> set[str]:
    ini = APP_DATA / "config" / "StreamDockConfig.plist"  # it is really an INI file
    cp = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        cp.read_string(ini.read_text(errors="ignore"))
    except (OSError, configparser.Error):
        return set()
    return {cp[s]["sdProfileName"] for s in cp.sections() if "sdProfileName" in cp[s]}


def find_profiles(serial: str | None = None) -> list[dict[str, Any]]:
    """Profiles for the 293/AKP153 family; the one the app last used for this serial sorts first."""
    root = APP_DATA / "profiles"
    if not root.exists():
        return []
    active = _active_profile_names()
    out = []
    for d in sorted(root.glob("*.sdProfile")):
        try:
            m = json.loads((d / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            continue
        dev = str(m.get("DeviceUUID", ""))
        if "153" not in dev:  # only the 293 / AKP153 family has the 5x3 grid
            continue
        out.append({
            "path": d,
            "name": m.get("Name") or d.stem,
            "device": dev,
            "pages": len(m.get("Pages", {}).get("Pages", [])) or 1,
            "active": d.name in active,
            "serial": m.get("DeviceSerialNumber"),
        })
    out.sort(key=lambda p: (not (p["active"] and serial and p["serial"] == serial), not p["active"], p["name"]))
    return out


def _pos_to_key(pos: str) -> int | None:
    try:
        x, y = (int(v) for v in pos.split(","))
    except ValueError:
        return None
    if not (0 <= x < COLS and 0 <= y < 3):
        return None
    return y * COLS + x + 1


def _hotkey(settings: dict) -> str | None:
    for hk in settings.get("Hotkeys", []):
        code = hk.get("VKeyCode", -1)
        if code is None or code < 0 or code not in VK:
            continue
        mods = [m for m, f in (("ctrl", "KeyCtrl"), ("alt", "KeyOption"), ("shift", "KeyShift"), ("cmd", "KeyCmd")) if hk.get(f)]
        return "+".join(mods + [VK[code]])
    return None


def _convert(action: dict, page_dir: Path, icon_dir: Path | None) -> tuple[dict | None, str]:
    uuid = action.get("UUID", "")
    s = action.get("Settings") or {}
    state = (action.get("States") or [{}])[action.get("State", 0) or 0] if action.get("States") else {}
    out: dict[str, Any] = {}
    title = (state.get("Title") or "").strip()
    if title:
        out["label"] = title
    img = state.get("Image") or ""
    if img and img.lower().endswith((".png", ".jpg", ".jpeg", ".gif")):
        src = page_dir / "Images" / img
        if not src.exists():
            src = page_dir / img
        if src.exists():
            dst = (icon_dir / src.name) if icon_dir else src
            if icon_dir:
                icon_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            out["image"] = str(dst)  # vendor key images are full-key artwork

    if uuid == "com.hotspot.streamdock.system.website":
        out["open"] = str(s.get("path", "")).strip()
    elif uuid == "com.hotspot.streamdock.system.open":
        out["open"] = str(s.get("path", "")).strip()
        out.setdefault("icon", f"app:{out['open']}" if out["open"].endswith(".app") else "")
        if not out["icon"]:
            del out["icon"]
        if "label" not in out:
            out["label"] = Path(out["open"]).stem
    elif uuid == "com.hotspot.streamdock.system.text":
        out["text"] = str(s.get("pastedText", ""))
        if s.get("isSendingEnter"):
            out["enter"] = True
    elif uuid == "com.hotspot.streamdock.system.hotkey":
        hk = _hotkey(s)
        if not hk:
            return None, "hotkey with no recognisable key"
        out["hotkey"] = hk
        out.setdefault("label", hk)
    elif uuid in ("com.hotspot.streamdock.page.change", "com.hotspot.streamdock.profile.rotate"):
        out["page"] = "next"
        out.setdefault("label", "›")
    elif uuid == "com.hotspot.streamdock.touchbar.playpause":
        out.update(hotkey="f8", label=out.get("label", "⏯"))
    elif uuid == "com.hotspot.streamdock.touchbar.nexttrack":
        out.update(hotkey="f9", label=out.get("label", "⏭"))
    elif uuid == "com.hotspot.streamdock.touchbar.previoustrack":
        out.update(hotkey="f7", label=out.get("label", "⏮"))
    else:
        return None, f"unsupported action {uuid}"
    return out, ""


def import_profile(profile: dict[str, Any], cfg: Config, icon_dir: Path, dry_run: bool = False) -> list[str]:
    root: Path = profile["path"]
    manifest = json.loads((root / "manifest.json").read_text())
    page_ids = manifest.get("Pages", {}).get("Pages") or [root.name]
    base = "".join(c if c.isalnum() else "-" for c in profile["name"].lower()).strip("-") or "imported"
    names = [base if i == 0 else f"{base}-{i + 1}" for i in range(len(page_ids))]
    report = []
    for i, pid in enumerate(page_ids):
        page_dir = root if pid == root.name else root / "profiles" / pid
        try:
            m = json.loads((page_dir / "manifest.json").read_text())
        except (OSError, json.JSONDecodeError):
            report.append(f"skip page {pid}: unreadable")
            continue
        acts = m.get("Actions", {})
        if not acts:
            report.append(f"page {names[i]}: empty, skipped")
            continue
        if names[i] not in cfg.pages():
            cfg.add_page(names[i])
        for pos, action in sorted(acts.items()):
            key = _pos_to_key(pos)
            where = f"page {names[i]} key {key or pos}"
            if key is None:
                report.append(f"{where}: side-column widget ({action.get('Name') or action.get('UUID')}), skipped")
                continue
            vals, why = _convert(action, page_dir, None if dry_run else icon_dir / names[i])
            if vals is None:
                report.append(f"{where}: {why}, skipped")
                continue
            cfg.set_key(names[i], key, vals, replace=True)
            report.append(f"{where}: " + ", ".join(f"{k}={v!r}" for k, v in vals.items()))
    imported = [n for n in names if n in cfg.pages()]
    current = manifest.get("Pages", {}).get("Current")
    start = names[page_ids.index(current)] if current in page_ids else None
    if start not in imported:
        start = imported[0] if imported else None
    if start and (not cfg.doc.get("start_page") or cfg.doc.get("start_page") not in cfg.pages() or cfg.doc.get("start_page") == "main"):
        cfg.set_top("start_page", start)
        report.append(f"start page: {start} (the page the app was showing)")
    return report
