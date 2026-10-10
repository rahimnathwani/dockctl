"""The config file: a TOML document that is the single source of truth for the layout.

Shape:

    brightness = 70
    start_page = "main"

    [style]                 # defaults applied to every key
    bg = "#111827"

    [pages.main.1]          # page "main", key 1 (top-left)
    label = "Build"
    icon = "🔨"
    run = "make -C ~/src/app"
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.items import Table

from .render import Face, parse_color

CONFIG_DIR = Path(os.environ.get("DOCKCTL_HOME", Path.home() / ".config" / "dockctl"))
CONFIG_PATH = Path(os.environ.get("DOCKCTL_CONFIG", CONFIG_DIR / "config.toml"))

FACE_FIELDS = {f.name for f in fields(Face)}
DISPLAY_ONLY_KEYS = range(16, 19)  # the narrow side column has screens but no switches
GLOBAL = "*"  # page name for [global] keys: shown on every page that doesn't set that key itself
ACTION_FIELDS = ("run", "open", "text", "hotkey", "page", "brightness", "shortcut", "applescript")
OPTION_FIELDS = ("cwd", "show_result", "enter", "poll", "every", "note")
KEY_FIELDS = FACE_FIELDS | set(ACTION_FIELDS) | set(OPTION_FIELDS)

STARTER = """\
# dockctl config — the single source of truth for your Stream Dock.
# Edit by hand or with `dockctl set`; the daemon reloads on save.
# Run `dockctl guide` for the full reference.
#
# The starter page drives Claude Code (or any terminal agent). Text keys paste into
# whichever window has focus and press Return. Rows: answers (green), requests (blue,
# purple), session (grey). Clear is the one key that throws work away, so it is red
# and as far from Yes as it can be.

brightness = 70
start_page = "claude"

[style]  # defaults for every key; any key can override
bg = "#18181b"
fg = "#fafafa"

# Row 1: answer the agent

[pages.claude.1]
label = "Yes"
icon = "✅"
bg = "#166534"
text = "yes"
enter = true

[pages.claude.2]
label = "Continue"
icon = "▶️"
bg = "#166534"
text = "continue"
enter = true

[pages.claude.3]
label = "Keep going"
icon = "🏃"
bg = "#166534"
text = "Great. Keep going."
enter = true

[pages.claude.4]
label = "Done"
icon = "👍"
bg = "#166534"
text = "done"
enter = true

[pages.claude.5]
label = "How's it going?"
icon = "💬"
bg = "#166534"
text = "how is it going?"
enter = true

# Row 2: ask for something

[pages.claude.6]
label = "Commit & push"
icon = "📦"
bg = "#1e3a8a"
text = "commit and push"
enter = true

[pages.claude.7]
label = "Commit to main"
icon = "🚀"
bg = "#1e3a8a"
text = "commit and push to main"
enter = true

[pages.claude.8]
label = "Make it excellent"
icon = "✨"
bg = "#5b21b6"
text = "what can we do to make this truly excellent?"
enter = true

[pages.claude.9]
label = "Run tests"
icon = "🧪"
bg = "#5b21b6"
text = "run the tests and fix any failures"
enter = true

[pages.claude.10]
label = "Stop"
icon = "✋"
bg = "#92400e"
hotkey = "escape"
note = "Esc interrupts Claude Code"

# Row 3: session. New/Resume need a shell prompt; the rest need the Claude prompt.

[pages.claude.11]
label = "New Claude"
icon = "✳️"
bg = "#374151"
text = "claude"
enter = true

[pages.claude.12]
label = "Resume Claude"
icon = "↩️"
bg = "#374151"
text = "claude --resume"
enter = true

[pages.claude.13]
label = "Compact"
icon = "🗜"
bg = "#374151"
text = "/compact"
enter = true

[pages.claude.14]
label = "Model"
icon = "🧠"
bg = "#374151"
text = "/model"
enter = true

[pages.claude.15]
label = "Clear context"
icon = "🧹"
bg = "#991b1b"
text = "/clear"
enter = true

# Side column (display only): shown on every page that doesn't set these keys itself

[global.16]
poll = "date '+%a%n%-d %b'"
every = 60

[global.17]
poll = "df -h / | awk 'NR==2{print \\"Disk\\\\n\\" $4 \\" free\\"}'"
every = 300

[global.18]
poll = "date +%H:%M"
every = 15
mono = true
"""


class ConfigError(ValueError):
    pass


@dataclass
class KeySpec:
    """One key on one page: how it looks and what it does."""

    face: Face
    action: str | None = None  # one of ACTION_FIELDS
    value: Any = None
    options: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def describe_action(self) -> str:
        if not self.action:
            return ""
        return f"{self.action}: {self.value}"


def validate_key(raw: dict[str, Any], where: str) -> None:
    unknown = set(raw) - KEY_FIELDS
    if unknown:
        raise ConfigError(f"{where}: unknown field(s) {sorted(unknown)}; allowed: {sorted(KEY_FIELDS)}")
    acts = [a for a in ACTION_FIELDS if a in raw]
    if len(acts) > 1:
        raise ConfigError(f"{where}: only one action allowed, got {acts}")
    for c in ("bg", "fg", "border"):
        if raw.get(c):
            try:
                parse_color(str(raw[c]))
            except ValueError as e:
                raise ConfigError(f"{where}: {e}") from None
    if "label_pos" in raw and raw["label_pos"] not in ("auto", "top", "middle", "bottom"):
        raise ConfigError(f"{where}: label_pos must be auto|top|middle|bottom")
    if "weight" in raw and raw["weight"] not in ("regular", "medium", "semibold", "bold", "heavy", "black"):
        raise ConfigError(f"{where}: weight must be regular|medium|semibold|bold|heavy|black")
    if "every" in raw and "poll" not in raw:
        raise ConfigError(f"{where}: 'every' needs 'poll'")


def check_display_only(key: int, raw: dict[str, Any], where: str) -> None:
    acts = [a for a in ACTION_FIELDS if raw.get(a) is not None]
    if key in DISPLAY_ONLY_KEYS and acts:
        raise ConfigError(f"{where}: keys 16-18 are display-only (no switch) — use them for labels, poll or overlays, not {acts[0]}")


class Config:
    def __init__(self, path: Path = CONFIG_PATH, strict: bool = False):
        self.path = path
        if path.exists():
            self.doc = tomlkit.parse(path.read_text())
        else:
            self.doc = tomlkit.document()
        if strict:
            self.validate()

    # ---- reading -------------------------------------------------------
    @property
    def brightness(self) -> int:
        return int(self.doc.get("brightness", 70))

    @property
    def start_page(self) -> str:
        sp = self.doc.get("start_page")
        if sp and sp in self.pages():
            return str(sp)
        pages = self.pages()
        return pages[0] if pages else "main"

    @property
    def style(self) -> dict[str, Any]:
        return dict(self.doc.get("style", {}))

    def pages(self) -> list[str]:
        return [str(p) for p in self.doc.get("pages", {}).keys()]

    def page_raw(self, page: str) -> dict[str, dict[str, Any]]:
        table = self.doc.get("global", {}) if page == GLOBAL else self.doc.get("pages", {}).get(page, {})
        return {str(k): dict(v) for k, v in table.items()}

    def is_global(self, page: str, key: int) -> bool:
        """True if what `page` shows on `key` comes from [global]."""
        return page != GLOBAL and str(key) not in self.page_raw(page) and str(key) in self.page_raw(GLOBAL)

    def key(self, page: str, key: int) -> KeySpec | None:
        raw = self.page_raw(page).get(str(key))
        if raw is None and page != GLOBAL:
            raw = self.page_raw(GLOBAL).get(str(key))
        if raw is None:
            return None
        return self.build_spec(raw)

    def build_spec(self, raw: dict[str, Any]) -> KeySpec:
        merged = {**self.style, **raw}
        face = Face(**{k: (str(v) if k not in ("font_size", "mono") else v) for k, v in merged.items() if k in FACE_FIELDS})
        action = next((a for a in ACTION_FIELDS if a in raw), None)
        return KeySpec(
            face=face,
            action=action,
            value=raw.get(action) if action else None,
            options={k: raw[k] for k in OPTION_FIELDS if k in raw},
            raw=raw,
        )

    def keys(self, page: str) -> dict[int, KeySpec]:
        """Every key shown on a page: its own keys over the [global] ones."""
        merged = {**(self.page_raw(GLOBAL) if page != GLOBAL else {}), **self.page_raw(page)}
        return {int(k): self.build_spec(raw) for k, raw in merged.items() if k.isdigit()}

    def validate(self, max_key: int | None = None) -> None:
        style = self.doc.get("style", {})
        if style:
            bad = set(style) - FACE_FIELDS
            if bad:
                raise ConfigError(f"[style]: only look fields allowed, got {sorted(bad)}")
        b = self.doc.get("brightness", 70)
        if not isinstance(b, int) or not 0 <= b <= 100:
            raise ConfigError("brightness must be an integer 0-100")
        tables = [(f"pages.{p}", keys) for p, keys in self.doc.get("pages", {}).items()]
        if "global" in self.doc:
            tables.append(("global", self.doc["global"]))
        for name, keys in tables:
            if not isinstance(keys, dict):
                raise ConfigError(f"[{name}] must be a table of keys")
            for k, raw in keys.items():
                where = f"[{name}.{k}]"
                if not str(k).isdigit():
                    raise ConfigError(f"{where}: key ids are numbers (1 = top-left)")
                if max_key and not 1 <= int(k) <= max_key:
                    raise ConfigError(f"{where}: key must be 1-{max_key}")
                if not isinstance(raw, dict):
                    raise ConfigError(f"{where}: must be a table")
                validate_key(dict(raw), where)
                check_display_only(int(k), dict(raw), where)
                target = raw.get("page")
                if target and target not in ("next", "prev", "back") and str(target) not in self.doc["pages"]:
                    raise ConfigError(f"{where}: page {target!r} does not exist")

    # ---- writing -------------------------------------------------------
    def _pages_table(self) -> Table:
        if "pages" not in self.doc:
            self.doc["pages"] = tomlkit.table(is_super_table=True)
        return self.doc["pages"]

    def ensure_page(self, page: str) -> Table:
        if page == GLOBAL:
            if "global" not in self.doc:
                self.doc["global"] = tomlkit.table(is_super_table=True)
            return self.doc["global"]
        pages = self._pages_table()
        if page not in pages:
            pages[page] = tomlkit.table()  # not a super table: an empty page must still be written out
        return pages[page]

    def set_key(self, page: str, key: int, values: dict[str, Any], replace: bool = False) -> dict[str, Any]:
        """Merge (or replace) fields on a key. A value of None deletes that field."""
        where = f"[pages.{page}.{key}]"
        validate_key({k: v for k, v in values.items() if v is not None}, where)
        check_display_only(key, values, where)
        tbl = self.ensure_page(page)
        k = str(key)
        if replace or k not in tbl:
            tbl[k] = tomlkit.table()
        cur = tbl[k]
        if any(a in values and values[a] is not None for a in ACTION_FIELDS):
            for a in ACTION_FIELDS:
                if a in cur and a not in values:
                    del cur[a]
        for name, v in values.items():
            if v is None:
                if name in cur:
                    del cur[name]
            else:
                cur[name] = v
        return dict(cur)

    def delete_key(self, page: str, key: int) -> bool:
        tbl = self.doc.get("global") if page == GLOBAL else self.doc.get("pages", {}).get(page)
        if tbl is not None and str(key) in tbl:
            del tbl[str(key)]
            return True
        return False

    def move_key(self, page: str, src: int, dst: int, swap: bool = True) -> None:
        tbl = self.ensure_page(page)
        a, b = tbl.get(str(src)), tbl.get(str(dst))
        if a is None:
            raise ConfigError(f"key {src} on page {page!r} is empty")
        a_copy = tomlkit.parse(tomlkit.dumps({"x": a}))["x"]
        b_copy = tomlkit.parse(tomlkit.dumps({"x": b}))["x"] if b is not None else None
        del tbl[str(src)]
        if b is not None:
            del tbl[str(dst)]
        tbl[str(dst)] = a_copy
        if b_copy is not None and swap:
            tbl[str(src)] = b_copy

    def add_page(self, page: str, copy_from: str | None = None) -> None:
        if page in self.pages():
            raise ConfigError(f"page {page!r} already exists")
        tbl = self.ensure_page(page)
        if copy_from:
            for k, v in self.doc["pages"][copy_from].items():
                tbl[k] = tomlkit.parse(tomlkit.dumps({"x": v}))["x"]

    def delete_page(self, page: str) -> None:
        if page not in self.pages():
            raise ConfigError(f"no page {page!r}")
        del self.doc["pages"][page]

    def rename_page(self, old: str, new: str) -> None:
        if old not in self.pages():
            raise ConfigError(f"no page {old!r}")
        if new in self.pages():
            raise ConfigError(f"page {new!r} already exists")
        data = tomlkit.parse(tomlkit.dumps({"x": self.doc["pages"][old]}))["x"]
        del self.doc["pages"][old]
        self.doc["pages"][new] = data
        for p in self.pages():
            for v in self.doc["pages"][p].values():
                if v.get("page") == old:
                    v["page"] = new
        if self.doc.get("start_page") == old:
            self.doc["start_page"] = new

    def set_top(self, name: str, value: Any) -> None:
        if value is None:
            self.doc.pop(name, None)
        else:
            self.doc[name] = value

    def set_style(self, values: dict[str, Any]) -> None:
        if "style" not in self.doc:
            self.doc["style"] = tomlkit.table()
        for k, v in values.items():
            if k not in FACE_FIELDS:
                raise ConfigError(f"style field {k!r} not allowed")
            if v is None:
                self.doc["style"].pop(k, None)
            else:
                self.doc["style"][k] = v

    def save(self) -> None:
        self.validate()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".toml.tmp")
        tmp.write_text(tomlkit.dumps(self.doc))
        tmp.replace(self.path)

    def to_dict(self) -> dict[str, Any]:
        return self.doc.unwrap()


def init_config(path: Path = CONFIG_PATH, force: bool = False) -> bool:
    if path.exists() and not force:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER)
    return True
