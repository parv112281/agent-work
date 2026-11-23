"""Cross-platform process helpers: executable lookup, Git, notifications."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def executable(name: str) -> str:
    """Resolve a command on PATH, including Windows .exe/.cmd shims."""
    found = shutil.which(name)
    if not found:
        raise FileNotFoundError(f"Command not found on PATH: {name}")
    return found


def git(*args, check=False, capture=True, cwd=None) -> subprocess.CompletedProcess:
    return subprocess.run([executable("git"), *map(str, args)], cwd=cwd, check=check, text=True,
                          stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def git_out(*args, cwd=None) -> str | None:
    """Stripped stdout of a Git command, or None if it failed."""
    result = git(*args, cwd=cwd)
    return result.stdout.strip() if result.returncode == 0 else None


def write_json(path: Path, value) -> None:
    """Write JSON atomically so an interrupted run never leaves a half-written file."""
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, indent=2), encoding="utf-8")
    os.replace(temp, path)


def notify(message: str, title: str = "Logseq agent") -> None:
    """Best-effort desktop notification; silently does nothing where unsupported."""
    try:
        if sys.platform == "darwin" and shutil.which("osascript"):
            script = f"display notification {json.dumps(message)} with title {json.dumps(title)}"
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
        elif sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
        elif os.name == "nt" and shutil.which("powershell"):
            script = ("[void][Reflection.Assembly]::LoadWithPartialName('System.Windows.Forms');"
                      "$n=New-Object System.Windows.Forms.NotifyIcon;$n.Icon=[System.Drawing.SystemIcons]::Information;"
                      f"$n.Visible=$true;$n.ShowBalloonTip(5000,{ps_quote(title)},{ps_quote(message)},'Info');"
                      "Start-Sleep -Seconds 6;$n.Dispose()")
            subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        pass


def ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
