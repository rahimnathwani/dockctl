"""Install the daemon as a per-user LaunchAgent so it starts at login and restarts if it dies."""

from __future__ import annotations

import hashlib
import os
import plistlib
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .config import CONFIG_DIR
from .daemon import LOG_PATH

AGENT_SRC = Path(__file__).with_name("agent.c")
AGENT_BIN = CONFIG_DIR / "bin" / "dockctl-agent"
XCODE = Path("/Applications/Xcode.app/Contents/Developer")

LABEL = "com.dockctl.daemon"
PLIST = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def has_compiler() -> bool:
    """True if a C compiler is really installed. Without the Command Line Tools, /usr/bin/cc is a shim
    that pops up an install dialog, so ask xcode-select instead of running it."""
    return subprocess.run(["xcode-select", "-p"], capture_output=True).returncode == 0


def agent_in_use() -> bool:
    """Whether the installed LaunchAgent runs the daemon under dockctl-agent."""
    try:
        with PLIST.open("rb") as fh:
            return plistlib.load(fh)["ProgramArguments"][0] == str(AGENT_BIN)
    except (OSError, KeyError, IndexError, plistlib.InvalidFileException):
        return False


def build_agent() -> Path | None:
    """Compile the tiny launcher that holds the Accessibility grant. Rebuilt only when agent.c changes,
    because a new binary has a new code hash and macOS would forget the grant."""
    if not has_compiler():
        return None
    digest = hashlib.sha256(AGENT_SRC.read_bytes()).hexdigest()
    stamp = AGENT_BIN.with_suffix(".sha256")
    if AGENT_BIN.exists() and stamp.exists() and stamp.read_text().strip() == digest:
        return AGENT_BIN
    AGENT_BIN.parent.mkdir(parents=True, exist_ok=True)
    args = ["-O2", "-framework", "ApplicationServices", "-o", str(AGENT_BIN), str(AGENT_SRC)]
    compilers = [["cc", *args], ["xcrun", "clang", *args]]
    sdk = XCODE / "Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk"
    clang = XCODE / "Toolchains/XcodeDefault.xctoolchain/usr/bin/clang"
    if clang.exists() and sdk.exists():  # works when the Command Line Tools SDK is out of step with the OS
        compilers.append([str(clang), "-isysroot", str(sdk), *args])
    for cmd in compilers:
        if shutil.which(cmd[0]) or Path(cmd[0]).exists():
            if subprocess.run(cmd, capture_output=True).returncode == 0:
                stamp.write_text(digest)
                return AGENT_BIN
    return None


def _program() -> list[str]:
    exe = shutil.which("dockctl")
    cmd = [exe, "daemon", "run"] if exe else [sys.executable, "-m", "dockctl", "daemon", "run"]
    if not exe:
        return cmd
    agent = build_agent()
    if agent is None:
        return cmd
    # the agent posix_spawns its first argument directly, so give it the interpreter, not the script
    with open(exe, "rb") as fh:
        first = fh.readline().decode(errors="ignore")
    if first.startswith("#!"):
        return [str(agent), first[2:].strip(), exe, "daemon", "run"]
    return [str(agent), *cmd]


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install() -> Path:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": _program(),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Interactive",
        "StandardOutPath": str(LOG_PATH),
        "StandardErrorPath": str(LOG_PATH),
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin"),
            "HOME": str(Path.home()),
            "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        },
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    stop()
    with PLIST.open("wb") as fh:
        plistlib.dump(plist, fh)
    for _ in range(10):  # bootstrap fails with EIO while a previous instance is still shutting down
        r = subprocess.run(["launchctl", "bootstrap", _domain(), str(PLIST)], capture_output=True, text=True)
        if r.returncode == 0 or "already" in r.stderr.lower():
            return PLIST
        time.sleep(0.5)
    raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip()}")


def stop() -> None:
    """Unload the job (if loaded) and wait until launchd has really let go of it."""
    if not loaded():
        return
    subprocess.run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], capture_output=True)
    for _ in range(20):
        if not loaded():
            return
        time.sleep(0.25)


def uninstall() -> bool:
    existed = PLIST.exists()
    stop()
    if existed:
        PLIST.unlink()
    return existed


def loaded() -> bool:
    r = subprocess.run(["launchctl", "print", f"{_domain()}/{LABEL}"], capture_output=True)
    return r.returncode == 0


def restart() -> None:
    subprocess.run(["launchctl", "kickstart", "-k", f"{_domain()}/{LABEL}"], check=True, capture_output=True)
