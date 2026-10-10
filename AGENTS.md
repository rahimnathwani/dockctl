# Working on dockctl

- **Publishing**: public repo github.com/rahimnathwani/dockctl. `install.sh` installs from the `main` branch tarball, so anything pushed to main ships to users on their next install.
- **Run things**: `uv run pytest`, `uv run dockctl ...`. The installed `dockctl` is an editable `uv tool` install, so source edits take effect immediately. The LaunchAgent daemon needs `dockctl daemon restart` to pick up code changes.
- **Hardware**: an AKP153E (0300:3010) is usually plugged in, and the user's real daemon owns it. Test against a throwaway daemon with `DOCKCTL_HOME=.scratch/x DOCKCTL_CONFIG=.scratch/x/config.toml DOCKCTL_SOCKET=.scratch/x/d.sock`, plus `DOCKCTL_FAKE=1` unless you have stopped the real daemon.
- **Never `dockctl press`** a key with a `text` / `hotkey` action: it types into whatever window has focus, which is probably your own terminal.
- **Verify visually**: `dockctl preview --live -o .scratch/p.png`, then read the PNG.
- **Protocol facts** are in README "Protocol notes". Treat them as verified; don't "fix" the key-id mapping or the 95 px size without hardware evidence.
- **Config writes** go through `Config.set_key` / `save` (tomlkit), which keeps the user's comments. Don't rewrite the file with a plain TOML dumper.
- **agent.c**: changing it changes the binary's code hash, and the user has to re-grant Accessibility. Avoid touching it.
