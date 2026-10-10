GUIDE = """\
# dockctl — drive an AJAZZ / Mirabox Stream Dock from the command line

dockctl replaces the vendor "Stream Dock AJAZZ" app. A small daemon owns the
device; a TOML file is the single source of truth for what every key shows and
does; this CLI edits that file and talks to the daemon. Quit the vendor app first
(both cannot drive the device at once).

## Layout (AKP153E)

     1  2  3  4  5  | 16
     6  7  8  9 10  | 17
    11 12 13 14 15  | 18

1-15 are buttons with screens, numbered row-major from the top-left.
16-18 are the narrow side column: screens only, no switch — use them for
status, clocks, `poll` output and overlays; actions there are rejected.
Keys can also be given as ROW,COL (1-based), e.g. `2,3` = key 8, `1,6` = key 16.
`dockctl preview -o page.png` renders a page to an image you can look at.

## Config file (~/.config/dockctl/config.toml; `dockctl config path`)

    brightness = 70          # 0-100
    start_page = "main"

    [style]                  # defaults for every key
    bg = "#18181b"
    fg = "#fafafa"

    [pages.main.1]
    label = "Build"
    icon = "🔨"
    run = "make build"
    cwd = "~/src/app"
    show_result = true       # flash green / red when the command exits

    [pages.main.5]
    label = "Media"
    page = "media"           # switch page

    [pages.main.10]
    poll = "date +%H:%M"     # live key: stdout becomes the label
    every = 30               # seconds

    [global.18]              # shown on every page that doesn't set key 18 itself
    poll = "date +%H:%M"
    every = 15

The daemon reloads the file on save. Edits made with `dockctl set` keep comments.
`--global` / `-g` on set, get and rm targets [global] instead of a page.

### Look fields
| field      | value                                                             |
|------------|-------------------------------------------------------------------|
| label      | text; `\\n` for line breaks; auto-sized to fit                    |
| icon       | emoji (`🚀`), image path, `app:Safari` or `app:/Applications/X.app` |
| image      | full-bleed background image (cropped to fill); label becomes a caption |
| bg, fg     | `#rrggbb`, `#rgb` or red/green/blue/yellow/orange/purple/gray/... |
| border     | ring colour, e.g. to mark state                                   |
| font_size  | max label px (keys are 95 px, side keys 82 px); labels always shrink to fit |
| weight     | regular, medium, semibold, bold, heavy, black                     |
| label_pos  | auto, top, middle, bottom                                         |
| mono       | true for a monospace label                                        |

### Actions (at most one per key)
| field       | does                                                              |
|-------------|-------------------------------------------------------------------|
| run         | shell command via `$SHELL -lc` (options: cwd, show_result)        |
| open        | URL, file, folder, or app name (`Safari`) / path                  |
| text        | types the text (clipboard paste, restored); `enter = true` adds ⏎ |
| hotkey      | key combo, e.g. `cmd+shift+4`, `ctrl+alt+left`, `f5`              |
| page        | page name, or `next` / `prev` / `back`                            |
| brightness  | `50`, `+10`, `-10`                                                |
| shortcut    | name of a macOS Shortcuts shortcut                                |
| applescript | AppleScript source                                                |

A key with no action still emits a press event (see `events` / `wait`).
`text` and `hotkey` send keystrokes, which needs Accessibility permission for
`dockctl-agent` (the daemon's launcher). `dockctl permissions` opens the setting.

### Live keys
`poll = "<shell>"` + `every = <seconds>`: stdout's text becomes the label. If stdout
is a JSON object, its look fields are applied, e.g.
`{"label": "CI ✓", "bg": "#166534"}`. Non-zero exit adds a red border.

## Commands (all support --json where output matters)

Layout editing (works without the device):
    dockctl ls [--page P] [--all]          grid + table of keys
    dockctl get KEY [--page P]
    dockctl set KEY [--page P] --label .. --icon .. --bg .. --run ..   (merge)
    dockctl set KEY --unset icon --unset bg                             (remove fields)
    dockctl set KEY --replace ...           replace the key instead of merging
    dockctl set 18 --global --poll ...      a key shown on every page
    dockctl rm KEY [--page P]
    dockctl mv SRC DST [--page P]           move / swap keys
    dockctl page ls | add NAME [--copy-from P] | rm NAME | rename OLD NEW
    dockctl style --bg .. --fg ..           defaults for all keys
    dockctl preview [--page P] [-o out.png] [--live]
    dockctl validate | edit | config path | init

Live control (needs the daemon):
    dockctl status                          daemon + device + current page
    dockctl page go NAME|next|prev|back
    dockctl brightness 40 | +10 | -10
    dockctl overlay KEY.. --label .. --bg .. [--ttl 30] [--sticky]
    dockctl overlay-clear [KEY..]
    dockctl banner "Deploying to staging" [--row 1] [--ttl 20]   (a few words per key)
    dockctl ask "Deploy to prod?" -o Yes:green -o No:red [--timeout 120]
    dockctl wait [KEY..] [--timeout S]      block until a key is pressed
    dockctl events                          stream key events (JSON lines)
    dockctl press KEY                       simulate a press
    dockctl apply                           reload config (pushes directly if no daemon)

Daemon:
    dockctl daemon install                  LaunchAgent: start at login, restart on crash
    dockctl daemon uninstall | start | stop | restart | run | logs

Diagnostics:
    dockctl doctor                          what is wrong and how to fix it
    dockctl permissions                     open Accessibility settings (for text/hotkey)
    dockctl test-pattern                    number every key (check orientation)
    dockctl import-app [--profile NAME]     copy layouts from the vendor app

## Patterns for coding agents

- Status light: the side column (16-18) is made for this.
  `dockctl overlay 16 --label "tests\\nrunning" --bg amber`, then
  `dockctl overlay 16 --label PASS --bg green --ttl 60`. Overlays sit on top of the
  configured key until cleared, their ttl ends, or (on 1-15) the key is pressed.
- Human-in-the-loop: `dockctl ask "Run migration?" -o Yes:green -o No:red --timeout 300`
  prints the chosen option and exits 0 (timeout: exit 124). Use `--json` for details.
- Wait for a physical go signal: `dockctl wait 1 --timeout 600`.
- Make a key for the current project: `dockctl set 6 --label "$(basename $PWD)\\ntest" --icon 🧪 --run "make test" --cwd "$PWD" --show-result`.
- Check your work visually: `dockctl preview -o /tmp/p.png` and read the image.

Exit codes: 0 ok, 1 error, 2 bad usage, 3 daemon not running, 124 timeout.
"""
