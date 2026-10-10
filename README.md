# dockctl

Drive an AJAZZ / Mirabox "Stream Dock" from the command line on macOS, without the vendor app.

- **Config as code**: one TOML file defines every page and key. Edit it by hand or with `dockctl set`; the daemon hot-reloads it.
- **Small daemon**: a LaunchAgent owns the USB device, draws the keys, runs actions, and redraws after unplug/replug, sleep and reboot.
- **Agent-friendly**: `--json` output, stable exit codes, PNG previews an agent can look at, overlays for status lights, and `ask` / `wait` for physical human-in-the-loop confirmation.

## Why not the vendor app?

The vendor app ("Stream Dock AJAZZ" / "Stream Dock" by Mirabox) is a GUI for setting up a dock by hand. dockctl is for people who want to drive the dock from scripts and coding agents.

| | vendor app | dockctl |
|---|---|---|
| **Configure** | GUI only; profiles are JSON with generated IDs | one commented TOML file, or `dockctl set` |
| **Version control / dotfiles** | impractical | yes: one file |
| **Scripts and agents** | no CLI or API | every command scriptable, `--json`, exit codes |
| **Live status on keys** | only via its plugins | `overlay`, `banner`, `poll` keys |
| **Ask a human** | no | `dockctl ask "Deploy?"` waits for a key press |
| **Footprint** | 1.1 GB Qt + embedded Chromium app, plus plugin processes | small Python daemon, no UI |
| **When it isn't running** | keys go dark | daemon starts at login and restarts if it crashes |
| **Plugin store** (OBS, weather, …) | yes | no; use `run` / `poll` with your own commands |
| **Platforms** | macOS, Windows | macOS only |

Typical uses:

- **Agent approvals**: a coding agent runs `dockctl ask` and waits for you to press Approve or Reject on the device.
- **Build status**: a side key shows the CI or test state.
- **Prompt nudges**: a key types "Keep going." into the agent's terminal.
- **Per-project keys**: an agent can add a page of keys for the repo it's working in.

Stick with the vendor app if you want its GUI editor, its plugins, Windows, or a model dockctl doesn't support.

## Supported devices

The hardware comes from one Chinese maker: the USB descriptors say HotSpot, and the brand is Mirabox. It's sold under several names. dockctl speaks the "293" family protocol: 15 LCD keys plus a 3-screen side column.

| sold as | USB id | firmware | status |
|---|---|---|---|
| **AJAZZ AKP153E** (rev 2) | `0300:3010` | v3 | **tested** (firmware `V3.AKP153E.01.002`) |
| AJAZZ AKP153R (rev 2) | `0300:3011` | v3 | should work, untested |
| Mirabox Stream Dock 293S V3 (HSV293SV3) | `6603:1014` | v3 | should work, untested |
| AJAZZ AKP153E (rev 1) | `0300:1010` | v1 | untested; older protocol variant (85 px keys, press-only events) |
| AJAZZ AKP153R (rev 1) | `0300:1020` | v1 | untested; as above |
| AJAZZ AKP153 | `5548:6674` | v1 | untested; as above |
| Mirabox Stream Dock 293S (HSV293S) | `5548:6670` | v1 | untested; as above |

Not supported:

- **Other Mirabox/AJAZZ layouts**: AKP03, AKP05, N3, N4 and other models with knobs or touch strips.
- **Elgato Stream Deck**: a different protocol.
- **Linux and Windows.**

`dockctl doctor` names your device if it's supported. Otherwise `hidutil list` shows each device's vendor and product id in the first two columns; v3 units usually describe themselves as `HOTSPOTEKUSB HID DEMO`. If yours is in an untested row, `dockctl test-pattern` shows whether keys land in the right place and orientation. Please open an issue with the result.

## Requirements

| what | notes |
|---|---|
| **macOS** | uses IOKit HID, launchd and System Events |
| **A supported device** | see above |
| **uv** | installed for you by `install.sh` if missing |
| **Xcode Command Line Tools** | optional; lets keystroke actions run under a dedicated launcher (see below). `xcode-select --install` |

## Install

```sh
curl -LsSf https://raw.githubusercontent.com/rahimnathwani/dockctl/main/install.sh | sh
```

That installs uv if needed, installs `dockctl` as a uv tool, then runs `dockctl setup`, which:

1. **Finds the device.** It carries on if it isn't plugged in yet.
2. **Quits the vendor app** if it's running. The vendor app and dockctl can't share the device; also remove "Stream Dock AJAZZ" from Login Items.
3. **Creates `~/.config/dockctl/config.toml`.** It imports your layout from the vendor app if it finds one, otherwise writes a starter page.
4. **Installs the LaunchAgent**, so the daemon runs now and at every login.
5. **Asks for Accessibility permission**, needed only for keys that type text or press hotkeys.

Prefer to do it by hand:

```sh
uv tool install git+https://github.com/rahimnathwani/dockctl
dockctl setup            # or --yes to accept every default
```

**Keystroke permission.** `text` and `hotkey` actions send keystrokes through System Events, which macOS gates behind Accessibility. With the Command Line Tools installed, setup compiles `src/dockctl/agent.c` into `~/.config/dockctl/bin/dockctl-agent`: a tiny launcher that runs the daemon, and the only thing you grant. Without them the daemon runs under uv's Python directly, and Python would need the grant. `dockctl permissions` opens the right settings page.

**Upgrade.** Re-run the install one-liner, or `uv tool install --force --reinstall git+https://github.com/rahimnathwani/dockctl && dockctl setup`. Your config and the Accessibility grant are kept; the launcher is only rebuilt when `agent.c` changes.

**Uninstall.**

```sh
dockctl daemon uninstall
uv tool uninstall dockctl
rm -rf ~/.config/dockctl       # config, imported icons, log, launcher
```

## Use

```sh
dockctl ls                                          # grid + table of the current page
dockctl set 1 --label Build --icon 🔨 --run "make" --cwd ~/src/app --show-result
dockctl set 2 --label Docs --open https://docs.python.org
dockctl set 3 --label GO --text "Great. Keep going." --enter
dockctl set 5 --label "Media ›" --goto media        # creates the page
dockctl set 18 --global --poll "date +%H:%M" --every 15   # clock on every page
dockctl preview -o /tmp/page.png                    # look at it without the device
dockctl overlay 17 --label PASS --bg green --ttl 60 # temporary status light
dockctl ask "Deploy to prod?" -o Deploy:green -o Cancel:red --timeout 300
```

`dockctl guide` prints the full reference: every field, action, command and agent recipe. Point a coding agent at it.

## Layout

```
 1  2  3  4  5  | 16
 6  7  8  9 10  | 17
11 12 13 14 15  | 18
```

Keys 1-15 are buttons with screens. 16-18, the narrow side column, are screens without switches: use them for clocks, polled status and overlays. Keys can also be addressed as `ROW,COL` (`2,3` = key 8, `1,6` = key 16).

## Files

| path | what |
|---|---|
| `~/.config/dockctl/config.toml` | layout (override with `DOCKCTL_CONFIG`) |
| `~/.config/dockctl/daemon.log` | daemon log (`dockctl daemon logs -f`) |
| `~/.config/dockctl/daemon.sock` | control socket, one JSON request per line |
| `~/.config/dockctl/bin/dockctl-agent` | launcher that holds the Accessibility grant |
| `~/Library/LaunchAgents/com.dockctl.daemon.plist` | LaunchAgent |

## Protocol notes

USB HID, VID `0x0300` PID `0x3010`, vendor interface (usage page `0xFFA0`). Output reports are 1024 bytes with no report ID; input reports are 512 bytes.

| command | bytes after `CRT\0\0` | use |
|---|---|---|
| wake | `DIS` | screen on |
| brightness | `LIG\0\0<0-100>` | |
| clear | `CLE\0\0\0<id or 0xFF>` | follow with `STP` |
| image | `BAT<len u32 BE><id>`, then JPEG in 1024-byte reports | 95×95 main, 82×82 side, rotated 90° CCW |
| flush | `STP` | show what was sent |
| keep-alive | `CONNECT` | every 10 s |
| release | `CLE\0\0DC`, `HAN` | back to idle logo, sleep |

Input: `ACK\0\0OK\0\0<id><1=press,0=release>`. Device ids run column by column from the top-right: 13 10 7 4 1 / 14 11 8 5 2 / 15 12 9 6 3, and 16-18 for the side column. Verified on firmware `V3.AKP153E.01.002`.

## Development

```sh
git clone https://github.com/rahimnathwani/dockctl && cd dockctl
uv tool install --editable .   # `dockctl` on PATH runs your checkout
uv run pytest
DOCKCTL_FAKE=1 DOCKCTL_HOME=/tmp/dd uv run dockctl daemon run -v   # simulated device, no hardware
```

`device.py` holds the HID protocol, `daemon.py` the run loop and control socket, `config.py` the TOML model, `render.py` the key images, and `cli.py` the commands.
