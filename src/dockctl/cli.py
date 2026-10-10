"""dockctl command line."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

import click

from . import client, launchd
from .config import CONFIG_PATH, GLOBAL, Config, ConfigError, init_config
from .daemon import LOG_PATH
from .device import DEFAULT_MODEL, MODELS, DeviceError, Dock, find
from .guide import GUIDE
from .render import Face, chunk_text, fitted_size, render_face, render_preview

VENDOR_APP = "Stream Dock AJAZZ"

EXIT_ERR, EXIT_USAGE, EXIT_NO_DAEMON, EXIT_TIMEOUT = 1, 2, 3, 124


# ---- helpers ---------------------------------------------------------------
def out(obj: Any, as_json: bool, text: str | None = None) -> None:
    if as_json:
        click.echo(json.dumps(obj, indent=2, ensure_ascii=False, default=str))
    elif text is not None:
        click.echo(text)


def fail(msg: str, code: int = EXIT_ERR) -> None:
    click.echo(f"error: {msg}", err=True)
    sys.exit(code)


def parse_key(value: str, m=None) -> int:
    m = m or DEFAULT_MODEL
    value = value.strip()
    if re.fullmatch(r"\d+,\d+", value):
        r, c = (int(x) for x in value.split(","))
        try:
            return m.key_at(r, c)
        except ValueError as e:
            raise click.BadParameter(str(e)) from None
    if not value.isdigit() or not 1 <= int(value) <= m.total_keys:
        raise click.BadParameter(f"key must be 1-{m.total_keys} (16-18 = narrow side column) or ROW,COL")
    return int(value)


def global_opt(f):
    return click.option("-g", "--global", "global_", is_flag=True,
                        help="Use the [global] keys, shown on every page that doesn't set that key.")(f)


def pick_page(cfg: Config, page: str | None, global_: bool) -> str:
    return GLOBAL if global_ else (page or cfg.start_page)


class KeyType(click.ParamType):
    name = "KEY"

    def convert(self, value, param, ctx):
        if isinstance(value, int):
            return value
        try:
            return parse_key(value)
        except click.BadParameter as e:
            self.fail(str(e), param, ctx)


KEY = KeyType()


def load_cfg(ctx: click.Context) -> Config:
    try:
        return Config(ctx.obj["config"])
    except ConfigError as e:
        fail(f"{ctx.obj['config']}: {e}")
        raise


def notify_daemon() -> bool:
    """Ask a running daemon to reload; returns False if none is running."""
    try:
        r = client.request("reload", _timeout=5)
        if not r.get("ok"):
            click.echo(f"warning: daemon rejected config: {r.get('error')}", err=True)
        return True
    except client.NotRunning:
        return False


def daemon_req(cmd: str, _timeout: float | None = 10, **kw) -> dict:
    try:
        r = client.request(cmd, _timeout=_timeout, **kw)
    except client.NotRunning as e:
        fail(str(e), EXIT_NO_DAEMON)
        raise
    if not r.get("ok") and r.get("error") != "timeout":
        fail(r.get("error", "daemon error"))
    return r


def vendor_app_running() -> bool:
    return subprocess.run(["pgrep", "-f", f"{VENDOR_APP}.app/Contents/MacOS/"], capture_output=True).returncode == 0


def face_dict(face: Face) -> dict[str, Any]:
    return {k: v for k, v in face.__dict__.items() if v not in ("", 0, False) and not (k == "weight" and v == "semibold") and not (k == "label_pos" and v == "auto")}


# ---- root ------------------------------------------------------------------
@click.group(context_settings={"help_option_names": ["-h", "--help"], "max_content_width": 100})
@click.option("--config", "config_path", type=click.Path(path_type=Path), default=CONFIG_PATH, show_default=True,
              envvar="DOCKCTL_CONFIG", help="Config file.")
@click.version_option(package_name="dockctl")
@click.pass_context
def cli(ctx: click.Context, config_path: Path) -> None:
    """Control an AJAZZ / Mirabox Stream Dock without the vendor app.

    Keys are numbered 1-15 row-major from the top-left (or ROW,COL). The config
    file is the source of truth; the daemon shows it and runs actions.
    Start with `dockctl guide` (full reference) or `dockctl doctor`.
    """
    ctx.ensure_object(dict)
    ctx.obj["config"] = config_path


@cli.command()
def guide() -> None:
    """Print the full reference (config format, actions, agent recipes)."""
    click.echo(GUIDE)


@cli.command()
@click.option("--force", is_flag=True, help="Overwrite an existing config.")
@click.pass_context
def init(ctx: click.Context, force: bool) -> None:
    """Create a starter config."""
    path = ctx.obj["config"]
    if init_config(path, force):
        click.echo(f"wrote {path}")
    else:
        click.echo(f"{path} already exists (use --force to overwrite)")


@cli.group("config")
def config_group() -> None:
    """Config file helpers."""


@config_group.command("path")
@click.pass_context
def config_path_cmd(ctx: click.Context) -> None:
    """Print the config file path."""
    click.echo(ctx.obj["config"])


@config_group.command("show")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def config_show(ctx: click.Context, as_json: bool) -> None:
    """Print the config (TOML, or JSON with --json)."""
    cfg = load_cfg(ctx)
    if as_json:
        out(cfg.to_dict(), True)
    else:
        click.echo(ctx.obj["config"].read_text() if ctx.obj["config"].exists() else "")


@cli.command()
@click.pass_context
def validate(ctx: click.Context) -> None:
    """Check the config for errors."""
    cfg = load_cfg(ctx)
    try:
        cfg.validate(max_key=DEFAULT_MODEL.total_keys)
    except ConfigError as e:
        fail(str(e))
    n = sum(len(cfg.page_raw(p)) for p in cfg.pages())
    click.echo(f"ok: {len(cfg.pages())} page(s), {n} key(s)")


@cli.command()
@click.pass_context
def edit(ctx: click.Context) -> None:
    """Open the config in $EDITOR, validate it, and reload the daemon."""
    path = ctx.obj["config"]
    init_config(path)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.call([*editor.split(), str(path)])
    ctx.invoke(validate)
    notify_daemon()


# ---- inspecting ------------------------------------------------------------
def key_row(cfg: Config, page: str, k: int) -> dict[str, Any]:
    spec = cfg.key(page, k)
    if spec is None:
        return {"key": k}
    d: dict[str, Any] = {"key": k, **{f: spec.raw[f] for f in spec.raw}}
    return d


def grid_text(cfg: Config, page: str, m) -> str:
    w = 12
    lines = []
    ncols = m.cols + (1 if m.side_keys else 0)
    sep = "+" + "+".join(["-" * w] * m.cols) + ("++" + "-" * w + "+" if m.side_keys else "+")
    lines.append(sep)
    for r in range(m.rows):
        top, mid = [], []
        for c in range(ncols):
            k = m.key_at(r + 1, c + 1)
            spec = cfg.key(page, k)
            label = ((spec.raw.get("label") or spec.raw.get("icon") or ("poll" if spec.raw.get("poll") else "")) if spec else "").replace("\n", " ")
            act = (spec.action or "") if spec else ""
            top.append(f"{k:>2} {label}"[:w].ljust(w))
            mid.append(f"   {act}"[:w].ljust(w))
        if m.side_keys:
            lines.append("|" + "|".join(top[:-1]) + "||" + top[-1] + "|")
            lines.append("|" + "|".join(mid[:-1]) + "||" + mid[-1] + "|")
        else:
            lines.append("|" + "|".join(top) + "|")
            lines.append("|" + "|".join(mid) + "|")
        lines.append(sep)
    return "\n".join(lines)


@cli.command("ls")
@click.option("--page", "-p", help="Page (default: the daemon's current page, else start_page).")
@click.option("--all", "all_pages", is_flag=True, help="Every page.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def ls_cmd(ctx: click.Context, page: str | None, all_pages: bool, as_json: bool) -> None:
    """Show the keys on a page as a grid and a table."""
    cfg = load_cfg(ctx)
    m = DEFAULT_MODEL
    current = None
    try:
        current = client.request("status", _timeout=2).get("page")
    except (client.NotRunning, OSError, RuntimeError):
        pass
    pages = cfg.pages() if all_pages else [page or current or cfg.start_page]
    for p in pages:
        if p not in cfg.pages() and cfg.pages():
            fail(f"no page {p!r}; pages: {', '.join(cfg.pages())}")
    if as_json:
        out({"current_page": current, "start_page": cfg.start_page, "pages": {
            p: [key_row(cfg, p, k) for k in range(1, m.total_keys + 1) if cfg.key(p, k)] for p in pages}}, True)
        return
    for p in pages:
        tags = []
        if p == cfg.start_page:
            tags.append("start")
        if p == current:
            tags.append("showing")
        click.echo(f"page {p!r}" + (f" ({', '.join(tags)})" if tags else "") + f"   pages: {' '.join(cfg.pages())}")
        click.echo(grid_text(cfg, p, m))
        for k, spec in sorted(cfg.keys(p).items()):
            mark = "*" if cfg.is_global(p, k) else " "
            look = " ".join(f"{f}={spec.raw[f]!r}" for f in ("label", "icon", "bg") if f in spec.raw)
            act = f"{spec.action}={spec.value!r}" if spec.action else ""
            extra = " ".join(f"{f}={spec.raw[f]!r}" for f in ("enter", "cwd", "show_result", "poll", "every") if f in spec.raw)
            click.echo(f" {mark}{k:>2}  {look}  {act}  {extra}".rstrip())
        if any(cfg.is_global(p, k) for k in cfg.keys(p)):
            click.echo("  (* = from [global], shown on every page that doesn't set that key)")
        click.echo()


@cli.command("get")
@click.argument("key", type=KEY)
@click.option("--page", "-p")
@global_opt
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def get_cmd(ctx: click.Context, key: int, page: str | None, global_: bool, as_json: bool) -> None:
    """Show one key's settings."""
    cfg = load_cfg(ctx)
    page = pick_page(cfg, page, global_)
    row = key_row(cfg, page, key)
    if as_json:
        out({"page": page, **row}, True)
    elif len(row) == 1:
        click.echo(f"key {key} on page {page!r} is empty")
    else:
        for k, v in row.items():
            click.echo(f"{k:12} {v}")


# ---- editing ---------------------------------------------------------------
LOOK_OPTS = [
    click.option("--label", "-l", help="Text on the key (\\n for newline)."),
    click.option("--icon", "-i", help="Emoji, image path, or app:Name."),
    click.option("--image", help="Full-bleed background image path."),
    click.option("--bg", help="Background colour (#rrggbb or name)."),
    click.option("--fg", help="Text colour."),
    click.option("--border", help="Border ring colour."),
    click.option("--font-size", type=int, help="Label size in px (0 = auto)."),
    click.option("--weight", type=click.Choice(["regular", "medium", "semibold", "bold", "heavy", "black"])),
    click.option("--label-pos", type=click.Choice(["auto", "top", "middle", "bottom"])),
    click.option("--mono/--no-mono", default=None, help="Monospace label."),
]
ACTION_OPTS = [
    click.option("--run", "-r", help="Shell command to run on press."),
    click.option("--open", "open_", help="URL, file, or app to open."),
    click.option("--text", help="Text to type."),
    click.option("--hotkey", help="Key combo, e.g. cmd+shift+4."),
    click.option("--goto", help="Switch to page (name/next/prev/back)."),
    click.option("--brightness", "set_brightness", help="Set brightness: 50, +10, -10."),
    click.option("--shortcut", help="macOS Shortcuts name to run."),
    click.option("--applescript", help="AppleScript to run."),
    click.option("--cwd", help="Working directory for --run / --poll."),
    click.option("--show-result/--no-show-result", default=None, help="Flash green/red when --run exits."),
    click.option("--enter/--no-enter", default=None, help="Press Return after --text."),
    click.option("--poll", help="Shell command whose output becomes the label."),
    click.option("--every", type=float, help="Seconds between polls (default 60)."),
    click.option("--note", help="Free-text note (not shown on the key)."),
]


def add_opts(opts):
    def deco(f):
        for o in reversed(opts):
            f = o(f)
        return f
    return deco


def collect(kw: dict[str, Any]) -> dict[str, Any]:
    rename = {"open_": "open", "goto": "page", "set_brightness": "brightness"}
    vals = {}
    for k, v in kw.items():
        if v is None:
            continue
        name = rename.get(k, k)
        if isinstance(v, str) and name in ("label",):
            v = v.replace("\\n", "\n")
        vals[name] = v
    return vals


@cli.command("set")
@click.argument("key", type=KEY)
@click.option("--page", "-p", help="Page (default: start_page; created if missing).")
@global_opt
@add_opts(LOOK_OPTS)
@add_opts(ACTION_OPTS)
@click.option("--unset", multiple=True, help="Remove a field (repeatable), e.g. --unset icon.")
@click.option("--replace", is_flag=True, help="Replace the whole key instead of merging.")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def set_cmd(ctx: click.Context, key: int, page: str | None, global_: bool, unset: tuple[str, ...], replace: bool, as_json: bool, **kw) -> None:
    """Create or update a key (fields merge into what is there).

    \b
    dockctl set 1 --label Build --icon 🔨 --run "make" --cwd ~/src/app --show-result
    dockctl set 2,1 --label Docs --open https://docs.python.org
    dockctl set 5 --label Media --goto media
    dockctl set 10 --poll "date +%H:%M" --every 30 --font-size 26
    dockctl set 18 --global --poll "date +%H:%M" --every 30     # on every page
    """
    init_config(ctx.obj["config"])
    cfg = load_cfg(ctx)
    page = pick_page(cfg, page, global_)
    vals = collect(kw)
    for f in unset:
        vals[f] = None
    if not vals and not replace:
        fail("nothing to set (see `dockctl set -h`)", EXIT_USAGE)
    if vals.get("page") and vals["page"] not in ("next", "prev", "back") and vals["page"] not in cfg.pages():
        cfg.ensure_page(vals["page"])
    try:
        result = cfg.set_key(page, key, vals, replace=replace)
        if not cfg.doc.get("start_page") and page != GLOBAL:
            cfg.set_top("start_page", page)
        cfg.save()
    except (ConfigError, ValueError) as e:
        fail(str(e))
    live = notify_daemon()
    out({"page": page, "key": key, "settings": result, "daemon_reloaded": live}, as_json,
        f"{'global' if page == GLOBAL else 'page ' + repr(page)} key {key}: " + ", ".join(f"{k}={v!r}" for k, v in result.items()))


@cli.command("rm")
@click.argument("keys", type=KEY, nargs=-1, required=True)
@click.option("--page", "-p")
@global_opt
@click.pass_context
def rm_cmd(ctx: click.Context, keys: tuple[int, ...], page: str | None, global_: bool) -> None:
    """Remove keys from a page."""
    cfg = load_cfg(ctx)
    page = pick_page(cfg, page, global_)
    removed = [k for k in keys if cfg.delete_key(page, k)]
    cfg.save()
    notify_daemon()
    click.echo(f"removed {removed or 'nothing'} from page {page!r}")


@cli.command("mv")
@click.argument("src", type=KEY)
@click.argument("dst", type=KEY)
@click.option("--page", "-p")
@click.pass_context
def mv_cmd(ctx: click.Context, src: int, dst: int, page: str | None) -> None:
    """Move a key (swaps if the destination is occupied)."""
    cfg = load_cfg(ctx)
    page = page or cfg.start_page
    try:
        cfg.move_key(page, src, dst)
    except ConfigError as e:
        fail(str(e))
    cfg.save()
    notify_daemon()
    click.echo(f"moved {src} -> {dst} on page {page!r}")


@cli.command("style")
@add_opts(LOOK_OPTS[3:])
@click.option("--unset", multiple=True)
@click.pass_context
def style_cmd(ctx: click.Context, unset: tuple[str, ...], **kw) -> None:
    """Set default look fields for every key."""
    init_config(ctx.obj["config"])
    cfg = load_cfg(ctx)
    vals = collect(kw)
    for f in unset:
        vals[f] = None
    try:
        cfg.set_style(vals)
        cfg.save()
    except ConfigError as e:
        fail(str(e))
    notify_daemon()
    click.echo("style: " + ", ".join(f"{k}={v!r}" for k, v in cfg.style.items()))


# ---- pages -----------------------------------------------------------------
@cli.group()
def page() -> None:
    """List, add, remove, rename, and switch pages."""


@page.command("ls")
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def page_ls(ctx: click.Context, as_json: bool) -> None:
    cfg = load_cfg(ctx)
    current = None
    try:
        current = client.request("status", _timeout=2).get("page")
    except (client.NotRunning, OSError, RuntimeError):
        pass
    rows = [{"page": p, "keys": len(cfg.page_raw(p)), "start": p == cfg.start_page, "showing": p == current} for p in cfg.pages()]
    out(rows, as_json, "\n".join(
        f"{'*' if r['showing'] else ' '} {r['page']:20} {r['keys']:>2} keys{'  (start)' if r['start'] else ''}" for r in rows))


@page.command("add")
@click.argument("name")
@click.option("--copy-from", help="Start as a copy of this page.")
@click.pass_context
def page_add(ctx: click.Context, name: str, copy_from: str | None) -> None:
    init_config(ctx.obj["config"])
    cfg = load_cfg(ctx)
    try:
        cfg.add_page(name, copy_from)
        cfg.save()
    except ConfigError as e:
        fail(str(e))
    notify_daemon()
    click.echo(f"added page {name!r}")


@page.command("rm")
@click.argument("name")
@click.pass_context
def page_rm(ctx: click.Context, name: str) -> None:
    cfg = load_cfg(ctx)
    try:
        cfg.delete_page(name)
        if cfg.doc.get("start_page") == name:
            cfg.set_top("start_page", cfg.pages()[0] if cfg.pages() else None)
        cfg.save()
    except ConfigError as e:
        fail(str(e))
    notify_daemon()
    click.echo(f"removed page {name!r}")


@page.command("rename")
@click.argument("old")
@click.argument("new")
@click.pass_context
def page_rename(ctx: click.Context, old: str, new: str) -> None:
    cfg = load_cfg(ctx)
    try:
        cfg.rename_page(old, new)
        cfg.save()
    except ConfigError as e:
        fail(str(e))
    notify_daemon()
    click.echo(f"renamed {old!r} -> {new!r}")


@page.command("start")
@click.argument("name")
@click.pass_context
def page_start(ctx: click.Context, name: str) -> None:
    """Set the page shown when the daemon starts."""
    cfg = load_cfg(ctx)
    if name not in cfg.pages():
        fail(f"no page {name!r}")
    cfg.set_top("start_page", name)
    cfg.save()
    click.echo(f"start page: {name}")


@page.command("go")
@click.argument("name")
def page_go(name: str) -> None:
    """Show a page now (name, next, prev, back)."""
    r = daemon_req("page", name=name)
    click.echo(f"showing {r['page']!r}")


# ---- preview ---------------------------------------------------------------
@cli.command()
@click.option("--page", "-p")
@click.option("-o", "--output", type=click.Path(path_type=Path), help="PNG path (default: a temp file).")
@click.option("--live", is_flag=True, help="Render what the daemon is showing now (incl. overlays and polled labels).")
@click.option("--scale", type=int, default=2, show_default=True, help="Pixel scale factor.")
@click.option("--no-numbers", is_flag=True)
@click.option("--open", "open_after", is_flag=True, help="Open the PNG in Preview.")
@click.pass_context
def preview(ctx: click.Context, page: str | None, output: Path | None, live: bool, scale: int, no_numbers: bool, open_after: bool) -> None:
    """Render a page to a PNG (key numbers in yellow). Prints the file path."""
    cfg = load_cfg(ctx)
    m = DEFAULT_MODEL
    base = ctx.obj["config"].parent
    faces: dict[int, Face] = {}
    title_page = page or cfg.start_page
    if live:
        snap = daemon_req("snapshot")
        title_page = snap["page"]
        for k, entry in snap["keys"].items():
            if "face" in entry:
                faces[int(k)] = Face(**entry["face"])
    else:
        if page and page not in cfg.pages():
            fail(f"no page {page!r}")
        for k, spec in cfg.keys(title_page).items():
            face = spec.face
            if spec.options.get("poll") and not face.label:
                face = Face(**{**face.__dict__, "label": "⟳ " + str(spec.options["poll"])[:24], "font_size": 11})
            faces[k] = face
    size = m.key_px * scale
    imgs = {}
    for k, f in faces.items():
        px = m.key_size(k) * scale
        try:
            imgs[k] = render_face(f, px, base, scale=scale)
        except Exception as e:
            imgs[k] = render_face(Face(label=f"⚠ {e}"[:60], bg="#7f1d1d", font_size=10), px, scale=scale)
    img = render_preview(imgs, m.cols, m.rows, size, title=f"page: {title_page}", numbers=not no_numbers,
                         side_keys=m.side_keys, side_px=m.side_px * scale)
    if output is None:
        output = Path(tempfile.gettempdir()) / f"dockctl-preview-{title_page}.png"
    img.save(output)
    click.echo(str(output))
    if open_after:
        subprocess.run(["open", str(output)])


# ---- live control ----------------------------------------------------------
@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def status(ctx: click.Context, as_json: bool) -> None:
    """Daemon, device and current page."""
    try:
        st = client.request("status", _timeout=3)
    except client.NotRunning:
        devs = find()
        st = {"ok": True, "daemon": False, "devices": [
            {"model": MODELS[(d["vendor_id"], d["product_id"])].name, "serial": d["serial_number"]} for d in devs],
            "vendor_app_running": vendor_app_running()}
        out(st, as_json, "daemon: not running (start with `dockctl daemon install`)\n"
            f"devices: {', '.join(d['model'] + ' ' + d['serial'] for d in st['devices']) or 'none found'}"
            + ("\nvendor app: running (it holds the device — quit it)" if st["vendor_app_running"] else ""))
        sys.exit(EXIT_NO_DAEMON)
    if as_json:
        out(st, True)
        return
    dev = st.get("device") or {}
    click.echo(f"daemon: running (pid {st['pid']}, up {st['uptime_s']}s)")
    if st["connected"]:
        click.echo(f"device: {dev.get('model')} serial {dev.get('serial')} firmware {dev.get('firmware') or '?'}")
    else:
        click.echo(f"device: not connected — {st.get('device_error')}")
    click.echo(f"page: {st['page']}  (pages: {' '.join(st['pages'])})")
    click.echo(f"brightness: {st['brightness']}")
    if st.get("accessibility") is False:
        click.echo("accessibility: not granted — text/hotkey keys won't type (dockctl permissions)")
    if st.get("overlays"):
        click.echo(f"overlays on keys: {st['overlays']}")
    if st.get("config_error"):
        click.echo(f"CONFIG ERROR: {st['config_error']}")
    if st.get("last_event"):
        e = st["last_event"]
        click.echo(f"last event: {e['event']} key {e['key']} ({e.get('label') or 'no label'}) on {e['page']}")


@cli.command()
@click.argument("value")
def brightness(value: str) -> None:
    """Set brightness now: 0-100, +N or -N (config keeps its own default)."""
    if not re.fullmatch(r"[+-]?\d{1,3}", value):
        fail("brightness must be 0-100, +N or -N", EXIT_USAGE)
    r = daemon_req("brightness", value=value)
    click.echo(f"brightness {r['brightness']}")


@cli.command()
@click.argument("key", type=KEY)
@click.option("--json", "as_json", is_flag=True)
def press(key: int, as_json: bool) -> None:
    """Simulate pressing a key (runs its action)."""
    daemon_req("press", key=key)
    out({"ok": True, "key": key}, as_json, f"pressed {key}")


@cli.command()
@click.argument("keys", type=KEY, nargs=-1, required=True)
@add_opts(LOOK_OPTS)
@click.option("--ttl", type=float, help="Remove after N seconds.")
@click.option("--sticky", is_flag=True, help="Keep after the key is pressed.")
@click.option("--merge", is_flag=True, help="Start from the key's current look instead of blank.")
@click.option("--tag", default="", help="Label this overlay so it can be cleared by tag.")
def overlay(keys: tuple[int, ...], ttl: float | None, sticky: bool, merge: bool, tag: str, **kw) -> None:
    """Temporarily show something on keys without touching the config.

    Pressing an overlaid key reports the press but does not run the key's
    action; the overlay is then removed (unless --sticky).

    \b
    dockctl overlay 15 --label "tests\\nrunning" --bg amber
    dockctl overlay 15 --label PASS --bg green --ttl 60
    dockctl overlay 3 --merge --border red      # keep the key, add a red ring
    """
    face = collect(kw)
    if not face:
        fail("give at least one look option (--label, --bg, --icon, ...)", EXIT_USAGE)
    daemon_req("overlay", keys=list(keys), face=face, ttl=ttl, sticky=sticky, merge=merge, tag=tag)
    click.echo(f"overlay on {list(keys)}" + (f" for {ttl:g}s" if ttl else ""))


@cli.command("overlay-clear")
@click.argument("keys", type=KEY, nargs=-1)
@click.option("--tag", help="Only overlays with this tag.")
def overlay_clear(keys: tuple[int, ...], tag: str | None) -> None:
    """Remove overlays (all if no keys given)."""
    daemon_req("clear_overlay", keys=list(keys), tag=tag)
    click.echo("cleared")


def _text_faces(text: str, rows: list[int], bg: str, fg: str) -> dict[int, dict]:
    """Spread text over whole rows of keys, a few words per key, centred."""
    m = DEFAULT_MODEL
    chunks = chunk_text(text, m.cols * len(rows), m.key_px)
    size = min((fitted_size(c, m.key_px) for c in chunks), default=0)  # one size for the whole message
    faces: dict[int, dict] = {}
    per_row = [chunks[i : i + m.cols] for i in range(0, len(chunks), m.cols)]
    for row, row_chunks in zip(rows, per_row):
        start = (m.cols - len(row_chunks)) // 2
        for i, chunk in enumerate(row_chunks):
            faces[m.key_at(row, start + i + 1)] = {"label": chunk, "bg": bg, "fg": fg, "weight": "bold", "font_size": size}
    return faces


@cli.command()
@click.argument("text")
@click.option("--row", type=click.IntRange(1, 3), default=1, show_default=True)
@click.option("--bg", default="#1e3a8a", show_default=True)
@click.option("--fg", default="#ffffff", show_default=True)
@click.option("--ttl", type=float)
@click.option("--sticky", is_flag=True)
def banner(text: str, row: int, bg: str, fg: str, ttl: float | None, sticky: bool) -> None:
    """Show a message across a row of keys, a few words per key (as overlays)."""
    m = DEFAULT_MODEL
    faces = _text_faces(text.replace("\\n", " "), [row], bg, fg)
    for k in range(m.key_at(row, 1), m.key_at(row, m.cols) + 1):
        face = faces.get(k, {"bg": bg})
        daemon_req("overlay", keys=[k], face=face, ttl=ttl, sticky=sticky, tag="banner")
    click.echo(f"banner on row {row}: " + " | ".join(f["label"] for f in faces.values()))


@cli.command()
@click.argument("question")
@click.option("-o", "--option", "options", multiple=True,
              help="Answer as LABEL or LABEL:COLOUR (repeatable, max 10). Default: Yes:green, No:red.")
@click.option("--timeout", type=float, help="Give up after N seconds (exit 124).")
@click.option("--json", "as_json", is_flag=True)
def ask(question: str, options: tuple[str, ...], timeout: float | None, as_json: bool) -> None:
    """Ask the human a question on the device and wait for them to press an answer.

    The question fills the top row (two rows if it is long and there are at most
    five answers), answers go below. Prints the chosen answer; exit 0, or 124 on
    timeout. Configured keys come back afterwards.

    \b
    dockctl ask "Deploy to prod?" -o Deploy:green -o Cancel:red --timeout 300
    """
    m = DEFAULT_MODEL
    opts = list(options) or ["Yes:green", "No:red"]
    if len(opts) > 2 * m.cols:
        fail(f"at most {2 * m.cols} options", EXIT_USAGE)
    tag = "ask-" + uuid.uuid4().hex[:6]
    q_bg = "#0f172a"
    text = question.replace("\\n", " ")
    long_q = len(chunk_text(text, 99, m.key_px)) > m.cols and len(opts) <= m.cols
    faces = _text_faces(text, [1, 2] if long_q else [1], q_bg, "#ffffff")
    for row in ([1, 2] if long_q else [1]):
        for c in range(1, m.cols + 1):
            faces.setdefault(m.key_at(row, c), {"bg": q_bg})
    answer_rows = [3] if long_q else ([2, 3] if len(opts) > m.cols else [2])
    answer_keys: dict[int, str] = {}
    for row, row_opts in zip(answer_rows, [opts[: m.cols], opts[m.cols :]]):
        start = (m.cols - len(row_opts)) // 2
        for i, o in enumerate(row_opts):
            label, _, color = o.partition(":")
            k = m.key_at(row, start + i + 1)
            answer_keys[k] = label
            faces[k] = {"label": label, "bg": color or "#374151", "weight": "bold"}
    for k in range(1, m.total_keys + 1):
        faces.setdefault(k, {"bg": "#000000"})
    r: dict = {}
    try:
        for k, face in faces.items():
            daemon_req("overlay", keys=[k], face=face, sticky=True, tag=tag)
        r = daemon_req("wait", _timeout=None, keys=list(answer_keys), timeout=timeout)
    finally:
        try:
            client.request("clear_overlay", keys=list(faces), tag=tag)
        except Exception:
            pass
    if not r.get("ok"):
        out({"answer": None, "timeout": True}, as_json, "timeout")
        sys.exit(EXIT_TIMEOUT)
    answer = answer_keys[r["key"]]
    out({"answer": answer, "key": r["key"]}, as_json, answer)


@cli.command()
@click.argument("keys", type=KEY, nargs=-1)
@click.option("--timeout", type=float, help="Give up after N seconds (exit 124).")
@click.option("--json", "as_json", is_flag=True)
def wait(keys: tuple[int, ...], timeout: float | None, as_json: bool) -> None:
    """Block until a key (any, or one of KEYS) is pressed; print its number."""
    r = daemon_req("wait", _timeout=None, keys=list(keys) or None, timeout=timeout)
    if not r.get("ok"):
        out({"key": None, "timeout": True}, as_json, "timeout")
        sys.exit(EXIT_TIMEOUT)
    r.pop("ok", None)
    out(r, as_json, str(r["key"]))


@cli.command()
@click.option("--json", "as_json", is_flag=True, help="JSON lines (default is human readable).")
@click.option("--count", type=int, help="Exit after N events.")
def events(as_json: bool, count: int | None) -> None:
    """Stream key presses/releases, page changes, connects, action results."""
    n = 0
    try:
        for evt in client.subscribe():
            if as_json:
                click.echo(json.dumps(evt, ensure_ascii=False))
            else:
                ts = time.strftime("%H:%M:%S")
                rest = " ".join(f"{k}={v}" for k, v in evt.items() if k not in ("event", "ts"))
                click.echo(f"{ts} {evt['event']:12} {rest}")
            n += 1
            if count and n >= count:
                break
    except client.NotRunning as e:
        fail(str(e), EXIT_NO_DAEMON)
    except KeyboardInterrupt:
        pass


@cli.command()
@click.pass_context
def apply(ctx: click.Context) -> None:
    """Validate and show the config now (reloads the daemon, or pushes directly if none)."""
    cfg = load_cfg(ctx)
    if notify_daemon():
        client.request("refresh", _timeout=5)
        click.echo("daemon reloaded")
        return
    with open_direct() as dock:
        m = dock.model
        dock.brightness(cfg.brightness)
        keys = cfg.keys(cfg.start_page)
        for k in range(1, m.total_keys + 1):
            spec = keys.get(k)
            face = spec.face if spec else Face(bg="#000000")
            dock.set_key_image(k, render_face(face, m.key_size(k), ctx.obj["config"].parent), flush=False)
        dock.flush()
    click.echo(f"pushed page {cfg.start_page!r} directly (no daemon: keys show, but presses do nothing)")


def open_direct() -> Dock:
    if vendor_app_running():
        fail(f"the {VENDOR_APP} app is running and owns the device — quit it first "
             f"(osascript -e 'quit app \"{VENDOR_APP}\"')")
    try:
        return Dock.open_first()
    except DeviceError as e:
        fail(str(e))
        raise


@cli.command("test-pattern")
@click.option("--hold", type=float, default=0, help="Seconds to keep it (direct mode).")
def test_pattern(hold: float) -> None:
    """Number every key so you can check orientation and key mapping."""
    colors = ["#dc2626", "#ea580c", "#ca8a04", "#16a34a", "#2563eb"]
    m = DEFAULT_MODEL
    if client.running():
        for k in range(1, m.total_keys + 1):
            r, c = m.position(k)
            daemon_req("overlay", keys=[k], face={"label": f"{k}\n{r},{c}", "bg": colors[(c - 1) % len(colors)], "weight": "bold"}, ttl=hold or 20, tag="test")
        click.echo("test pattern shown via daemon; press keys and watch `dockctl events`")
        return
    with open_direct() as dock:
        for k in range(1, dock.model.total_keys + 1):
            r, c = dock.model.position(k)
            face = Face(label=f"{k}\n{r},{c}", bg=colors[(c - 1) % len(colors)], weight="bold")
            dock.set_key_image(k, render_face(face, dock.model.key_size(k)), flush=False)
        dock.flush()
        click.echo("test pattern shown: key 1 should be top-left, ▲ pointing up. Press keys to see their numbers (Ctrl-C to stop).")
        deadline = time.monotonic() + hold if hold else None
        try:
            while deadline is None or time.monotonic() < deadline:
                ev = dock.read_event(200)
                if ev:
                    click.echo(f"{'press' if ev.pressed else 'release'} {ev.key}  raw={ev.raw.hex()}")
        except KeyboardInterrupt:
            pass


@cli.command("clear")
def clear_cmd() -> None:
    """Blank the screen (direct mode only; the daemon would redraw)."""
    if client.running():
        fail("the daemon is running; use `dockctl daemon stop` first, or `dockctl overlay 1 2 ... --bg black`")
    with open_direct() as dock:
        dock.clear_all()
    click.echo("cleared")


# ---- daemon ----------------------------------------------------------------
@cli.group()
def daemon() -> None:
    """Run / install the background daemon (LaunchAgent)."""


@daemon.command("run")
@click.option("--serial", help="Only drive the device with this serial.")
@click.option("-v", "--verbose", is_flag=True)
@click.pass_context
def daemon_run(ctx: click.Context, serial: str | None, verbose: bool) -> None:
    """Run in the foreground (what the LaunchAgent executes)."""
    from .daemon import main
    main(ctx.obj["config"], serial, verbose)


@daemon.command("install")
@click.pass_context
def daemon_install(ctx: click.Context) -> None:
    """Install + start the LaunchAgent (runs at login, restarts on crash)."""
    init_config(ctx.obj["config"])
    if vendor_app_running():
        click.echo(f"note: the {VENDOR_APP} app is running; the daemon will connect once you quit it.")
    p = launchd.install()
    for _ in range(20):
        if client.running():
            break
        time.sleep(0.25)
    click.echo(f"installed {p}\nlogs: {LOG_PATH}")
    if not launchd.agent_in_use():
        click.echo("note: couldn't build dockctl-agent (no C compiler), so text/hotkey keys need Accessibility for "
                   "Python itself. Better: run `xcode-select --install`, then `dockctl daemon install` again.")
    ctx.invoke(status)


@daemon.command("uninstall")
def daemon_uninstall() -> None:
    """Stop and remove the LaunchAgent."""
    click.echo("removed" if launchd.uninstall() else "was not installed")


@daemon.command("start")
@click.pass_context
def daemon_start(ctx: click.Context) -> None:
    """Start the daemon (installs the LaunchAgent if needed)."""
    if client.running():
        click.echo("already running")
        return
    if launchd.PLIST.exists():
        launchd.install()
    else:
        ctx.invoke(daemon_install)
        return
    for _ in range(20):
        if client.running():
            break
        time.sleep(0.25)
    click.echo("started" if client.running() else f"failed to start — see {LOG_PATH}")


@daemon.command("stop")
def daemon_stop() -> None:
    """Stop the daemon (LaunchAgent stays installed, starts again at login)."""
    if launchd.loaded():
        launchd.stop()
    elif client.running():
        client.request("shutdown")
    click.echo("stopped")


@daemon.command("restart")
@click.pass_context
def daemon_restart(ctx: click.Context) -> None:
    if launchd.loaded():
        launchd.restart()
    else:
        ctx.invoke(daemon_start)
    click.echo("restarted")


@daemon.command("logs")
@click.option("-n", "lines", default=40, show_default=True)
@click.option("-f", "follow", is_flag=True)
def daemon_logs(lines: int, follow: bool) -> None:
    if not LOG_PATH.exists():
        fail(f"no log yet at {LOG_PATH}")
    os.execvp("tail", ["tail", f"-n{lines}", *(["-f"] if follow else []), str(LOG_PATH)])


@cli.command()
def permissions() -> None:
    """Open the Accessibility settings so text/hotkey actions can send keystrokes."""
    click.echo(permissions_hint())
    subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])


def permissions_hint() -> str:
    if launchd.agent_in_use():
        target = f"'dockctl-agent' ({launchd.AGENT_BIN})"
    else:
        target = "the Python that runs dockctl (" + os.path.realpath(sys.executable) + ")"
    return (f"In System Settings > Privacy & Security > Accessibility, switch on {target}.\n"
            "If it isn't listed, press +, then Cmd+Shift+G and paste the path. Then: dockctl daemon restart")


def confirm(question: str, yes: bool, default: bool = True) -> bool:
    """click.confirm that also works unattended (--yes, or no terminal to ask on)."""
    if yes:
        return True
    if not sys.stdin.isatty():
        click.echo(f"{question} [{'Y/n' if default else 'y/N'}] (no terminal, assuming {'yes' if default else 'no'})")
        return default
    return click.confirm(question, default=default)


@cli.command()
@click.option("-y", "--yes", is_flag=True, help="Accept every default without asking.")
@click.option("--no-import", is_flag=True, help="Use the starter config even if the vendor app has a layout.")
@click.pass_context
def setup(ctx: click.Context, yes: bool, no_import: bool) -> None:
    """First run on a Mac: check the device, retire the vendor app, create a config, start the daemon.

    Safe to re-run (e.g. after upgrading): it keeps your config and restarts the daemon.
    """
    path = ctx.obj["config"]

    click.secho("1. Device", bold=True)
    devs = find()
    if devs:
        d = devs[0]
        click.echo(f"   found {MODELS[(d['vendor_id'], d['product_id'])].name} (serial {d['serial_number']})")
    else:
        click.echo("   none plugged in. Carrying on; the daemon will pick it up when you connect it.")

    click.secho("2. Vendor app", bold=True)
    if vendor_app_running():
        if confirm(f"   {VENDOR_APP} is running and would fight dockctl for the device. Quit it?", yes):
            subprocess.run(["osascript", "-e", f'quit app "{VENDOR_APP}"'], capture_output=True)
            for _ in range(20):
                if not vendor_app_running():
                    break
                time.sleep(0.25)
        if vendor_app_running():
            click.echo("   still running; quit it yourself before using dockctl")
    else:
        click.echo("   not running")
    click.echo("   Keep it out of System Settings > General > Login Items so it doesn't start at login.")

    click.secho("3. Config", bold=True)
    if path.exists():
        click.echo(f"   keeping {path}")
    else:
        from .appimport import find_profiles, import_profile

        profiles = [] if no_import else find_profiles(devs[0]["serial_number"] if devs else None)
        active = [p for p in profiles if p["active"]]
        if active and confirm(f"   Import your layout from the vendor app (profile {active[0]['name']!r})?", yes):
            cfg = Config(path)
            import_profile(active[0], cfg, path.parent / "icons")
            cfg.save()
            click.echo(f"   imported {len(cfg.pages())} page(s) into {path}")
        else:
            init_config(path)
            click.echo(f"   wrote a starter config to {path}")

    click.secho("4. Daemon", bold=True)
    launchd.install()
    for _ in range(40):
        if client.running():
            break
        time.sleep(0.25)
    click.echo(f"   LaunchAgent installed; starts at login. Log: {LOG_PATH}")
    if not launchd.agent_in_use():
        click.echo("   (no C compiler, so the daemon runs under Python directly; `xcode-select --install` and "
                   "re-run setup to use the dedicated dockctl-agent launcher)")

    click.secho("5. Keystroke permission (for text / hotkey keys)", bold=True)
    granted = None
    try:
        time.sleep(1)
        granted = client.request("status", _timeout=3).get("accessibility")
    except (client.NotRunning, OSError, RuntimeError):
        pass
    if granted:
        click.echo("   already granted")
    else:
        click.echo("   " + permissions_hint().replace("\n", "\n   "))
        if confirm("   Open that settings page now?", yes):
            subprocess.run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])

    click.secho("\nStatus", bold=True)
    try:
        ctx.invoke(doctor)
    except SystemExit:
        pass
    click.echo("\nNext: `dockctl ls`, `dockctl set 1 --label Hi --icon 👋 --run 'say hi'`, `dockctl guide`.")


# ---- doctor ----------------------------------------------------------------
@cli.command()
@click.option("--json", "as_json", is_flag=True)
@click.pass_context
def doctor(ctx: click.Context, as_json: bool) -> None:
    """Diagnose setup problems and say how to fix them."""
    checks = []

    def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix if not ok else ""})

    devs = find()
    check("device", bool(devs), ", ".join(f"{MODELS[(d['vendor_id'], d['product_id'])].name} serial {d['serial_number']}" for d in devs) or "none found",
          "plug the dock in (USB-C data cable, not a charge-only one)")
    app = vendor_app_running()
    check("vendor app", not app, "running" if app else "not running",
          f"quit it: osascript -e 'quit app \"{VENDOR_APP}\"' and remove it from Login Items")
    path = ctx.obj["config"]
    try:
        cfg = Config(path)
        cfg.validate(max_key=DEFAULT_MODEL.total_keys)
        check("config", path.exists(), f"{path} ({len(cfg.pages())} pages)" if path.exists() else f"{path} missing", "dockctl init")
    except ConfigError as e:
        check("config", False, str(e), f"fix {path} (dockctl validate)")
    check("launch agent", launchd.PLIST.exists(), str(launchd.PLIST) if launchd.PLIST.exists() else "not installed",
          "dockctl daemon install")
    try:
        st = client.request("status", _timeout=3)
        check("daemon", True, f"pid {st['pid']}, page {st['page']!r}")
        check("daemon ↔ device", st["connected"], "connected" if st["connected"] else str(st.get("device_error")),
              "quit the vendor app / replug the device; see `dockctl daemon logs`")
        if st.get("config_error"):
            check("daemon config", False, st["config_error"], "fix the config; the daemon keeps the last good one")
        check("accessibility", bool(st.get("accessibility")),
              "granted (text/hotkey actions work)" if st.get("accessibility") else "not granted: text/hotkey actions fail",
              "dockctl permissions")
    except client.NotRunning:
        check("daemon", False, "not running", "dockctl daemon start")
    if as_json:
        out({"ok": all(c["ok"] for c in checks), "checks": checks}, True)
    else:
        for c in checks:
            click.echo(f"{'✓' if c['ok'] else '✗'} {c['check']:16} {c['detail']}" + (f"\n    fix: {c['fix']}" if c["fix"] else ""))
    sys.exit(0 if all(c["ok"] for c in checks) else EXIT_ERR)


# ---- import from the vendor app ---------------------------------------------
@cli.command("import-app")
@click.option("--profile", help="Profile name or id to import (default: the one the app last used for your device).")
@click.option("--list", "list_only", is_flag=True, help="List profiles for the connected device model.")
@click.option("--dry-run", is_flag=True)
@click.pass_context
def import_app(ctx: click.Context, profile: str | None, list_only: bool, dry_run: bool) -> None:
    """Copy key layouts from the vendor app's profiles (websites, apps, text, hotkeys, pages)."""
    from .appimport import find_profiles, import_profile

    devs = find()
    profiles = find_profiles(devs[0]["serial_number"] if devs else None)
    if not profiles:
        fail("no Stream Dock app profiles found")
    if list_only:
        for p in profiles:
            click.echo(f"{'*' if p['active'] else ' '} {p['name']:24} {p['device']:16} {p['pages']} page(s)  {p['path'].name}")
        return
    chosen = [p for p in profiles if (profile in (p["name"], p["path"].name, p["path"].stem) if profile else p["active"])]
    if not chosen:
        fail("profile not found; try `dockctl import-app --list`")
    init_config(ctx.obj["config"])
    cfg = load_cfg(ctx)
    report = import_profile(chosen[0], cfg, ctx.obj["config"].parent / "icons", dry_run=dry_run)
    for line in report:
        click.echo(line)
    if dry_run:
        click.echo("(dry run — nothing saved)")
        return
    cfg.save()
    notify_daemon()
    click.echo(f"saved to {ctx.obj['config']}")


def main() -> None:
    cli(obj={})
