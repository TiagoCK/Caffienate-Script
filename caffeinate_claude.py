#!/usr/bin/env python3
"""Keep a Mac awake (even lid-closed on battery) only while Claude Code is working.

Driven by Claude Code hooks:
    UserPromptSubmit -> start   (opens a Terminal window running caffeinate)
    Stop / StopFailure / SessionEnd -> stop   (Ctrl+C's caffeinate, closes the window)

StopFailure is what fires when a turn ends on an API error such as a usage limit.
As a fallback, the window also watches the session transcript for a usage-limit error.

Lid-closed-on-battery sleep can only be overridden with `pmset -a disablesleep 1`,
which needs a passwordless sudoers rule (see README). Sleep is re-enabled as soon
as the last active session stops, Claude hits a usage limit, the battery gets
low, or MAX_HOURS elapses.

Usage:
    caffeinate_claude.py start | stop [--all] | status | panic | install | uninstall
"""

import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time

STATE_DIR = os.path.expanduser("~/.claude/caffeinate")
LOG_FILE = os.path.join(STATE_DIR, "log.txt")
SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")
SCRIPT = os.path.abspath(__file__)
# System python survives Homebrew upgrades; fall back to whatever is running us.
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
PMSET = "/usr/bin/pmset"

BATTERY_MIN_PERCENT = 15  # stop keeping awake at or below this (when discharging)
MAX_HOURS = 2  # safety cap in case a Stop hook never fires
BATTERY_CHECK_SECONDS = 60
STARTUP_GRACE_SECONDS = 30  # a session is "active" this long before its pid appears
TRANSCRIPT_CHECK_SECONDS = 10

HOOK_EVENTS = {
    "UserPromptSubmit": "start",
    "Stop": "stop",
    "StopFailure": "stop",  # turn ended on an API error (usage limit, etc.)
    "SessionEnd": "stop",
}


# ---------------------------------------------------------------- helpers

def log(msg):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")


def hook_input():
    """Hooks pass JSON on stdin; session_id falls back to 'manual' when run by hand."""
    data = {}
    if not sys.stdin.isatty():
        try:
            data = json.loads(sys.stdin.read() or "{}")
        except json.JSONDecodeError:
            pass
    data["session_id"] = safe_id(data.get("session_id") or "manual")
    return data


def safe_id(sid):
    return re.sub(r"[^A-Za-z0-9_-]", "_", sid)


def meta_path(sid):
    return os.path.join(STATE_DIR, f"{sid}.json")


def pid_path(sid):
    return os.path.join(STATE_DIR, f"{sid}.pid")


def read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def read_pid(sid):
    try:
        with open(pid_path(sid)) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def remove(path):
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def all_sessions():
    if not os.path.isdir(STATE_DIR):
        return []
    return [f[:-5] for f in os.listdir(STATE_DIR) if f.endswith(".json")]


def session_active(sid):
    """Active = its window process is alive, or it was started moments ago."""
    if pid_alive(read_pid(sid)):
        return True
    meta = read_json(meta_path(sid)) or {}
    return time.time() - meta.get("started", 0) < STARTUP_GRACE_SECONDS


def set_sleep_disabled(disabled):
    cmd = ["sudo", "-n", PMSET, "-a", "disablesleep", "1" if disabled else "0"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"pmset disablesleep {int(disabled)} failed (sudoers rule missing?): "
            f"{r.stderr.strip()}")
    return r.returncode == 0


def sleep_disabled_now():
    out = subprocess.run([PMSET, "-g"], capture_output=True, text=True).stdout
    m = re.search(r"SleepDisabled\s+(\d)", out)
    return m and m.group(1) == "1"


def battery_status():
    """Returns (percent, discharging) or (None, False) if there's no battery."""
    out = subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)%;\s*([\w ]+?);", out)
    if not m:
        return None, False
    return int(m.group(1)), m.group(2).strip() == "discharging"


def osascript(*lines, args=()):
    cmd = ["osascript"]
    for line in lines:
        cmd += ["-e", line]
    r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        log(f"osascript failed: {r.stderr.strip()}")
    return r.stdout.strip()


def close_window(window_id):
    if window_id:
        osascript(f'tell application "Terminal" to close '
                  f'(every window whose id is {int(window_id)})')


# ---------------------------------------------------------------- commands

def usage_limit_hit(transcript, offset):
    """Scan transcript lines appended after `offset` for a usage-limit error.

    Returns (hit, new_offset). Only complete lines are consumed.
    """
    try:
        with open(transcript, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except OSError:
        return False, offset
    end = chunk.rfind(b"\n") + 1
    for line in chunk[:end].splitlines():
        if b"isApiErrorMessage" not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isApiErrorMessage") and entry.get("error") == "rate_limit":
            return True, offset + end
    return False, offset + end


def cmd_start(hook):
    sid = hook["session_id"]
    if session_active(sid):
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(meta_path(sid), "w") as f:
        json.dump({"started": time.time(), "window_id": None,
                   "transcript_path": hook.get("transcript_path")}, f)

    shell_cmd = f"exec {shlex.quote(PYTHON)} {shlex.quote(SCRIPT)} _window {shlex.quote(sid)}"
    window_id = osascript(
        "on run argv",
        'tell application "Terminal"',
        "do script (item 1 of argv)",
        "return id of front window",
        "end tell",
        "end run",
        args=[shell_cmd],
    )
    meta = read_json(meta_path(sid)) or {"started": time.time()}
    meta["window_id"] = int(window_id) if window_id.isdigit() else None
    with open(meta_path(sid), "w") as f:
        json.dump(meta, f)


def cmd_window(sid):
    """Runs inside the Terminal window: holds caffeinate and the safety guards."""
    with open(pid_path(sid), "w") as f:
        f.write(str(os.getpid()))

    def _exit(signum, _frame):
        raise SystemExit(f"signal {signum}")

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGHUP, _exit)  # window closed by hand

    print("\033]0;☕ Claude is working\a", end="")
    if set_sleep_disabled(True):
        print("☕ Sleep disabled (lid-close on battery too) while Claude works.")
    else:
        print("☕ caffeinate only — lid-close sleep NOT blocked on battery.\n"
              "   Set up the sudoers rule from the README to enable that.")
    print(f"   Auto-stops when Claude finishes or hits a usage limit, "
          f"battery ≤ {BATTERY_MIN_PERCENT}%, or after {MAX_HOURS}h.  "
          f"Ctrl+C to stop now.", flush=True)

    meta = read_json(meta_path(sid)) or {}
    transcript = meta.get("transcript_path")
    try:
        transcript_offset = os.path.getsize(transcript) if transcript else 0
    except OSError:
        transcript_offset = 0

    caf = subprocess.Popen(["caffeinate", "-ims"])
    deadline = time.time() + MAX_HOURS * 3600
    next_battery_check = 0
    next_transcript_check = 0
    reason = "stopped"
    try:
        while caf.poll() is None:
            now = time.time()
            if now >= deadline:
                reason = f"reached {MAX_HOURS}h safety cap"
                break
            if now >= next_battery_check:
                next_battery_check = now + BATTERY_CHECK_SECONDS
                pct, discharging = battery_status()
                if discharging and pct is not None and pct <= BATTERY_MIN_PERCENT:
                    reason = f"battery low ({pct}%)"
                    break
            if transcript and now >= next_transcript_check:
                next_transcript_check = now + TRANSCRIPT_CHECK_SECONDS
                hit, transcript_offset = usage_limit_hit(transcript, transcript_offset)
                if hit:
                    reason = "Claude hit its usage limit"
                    break
            time.sleep(1)
        else:
            reason = "caffeinate exited"
    except KeyboardInterrupt:
        reason = "Claude finished"
    except SystemExit as e:
        reason = f"stopped by {e}"
    finally:
        if caf.poll() is None:
            caf.send_signal(signal.SIGINT)  # the Ctrl+C
            try:
                caf.wait(timeout=3)
            except subprocess.TimeoutExpired:
                caf.kill()
        window_id = (read_json(meta_path(sid)) or meta).get("window_id")
        release(sid)
        log(f"{sid}: {reason}")
        print(f"\n😴 {reason} — sleep re-enabled.", flush=True)
        # Close our own window once this process has exited (needed when we stop
        # ourselves: usage limit, battery, cap). Harmless if `stop` closes it first.
        if window_id:
            subprocess.Popen([PYTHON, SCRIPT, "_close", str(window_id)],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def release(sid):
    remove(pid_path(sid))
    remove(meta_path(sid))
    others = [s for s in all_sessions() if session_active(s)]
    if not others:
        set_sleep_disabled(False)


def cmd_stop(sid, error=None):
    meta = read_json(meta_path(sid))
    if meta is None:
        return
    if error:
        log(f"{sid}: turn ended with API error: {error}")
    # A very short task can end before the window process has written its pid.
    for _ in range(30):
        pid = read_pid(sid)
        if pid or not session_active(sid):
            break
        time.sleep(0.1)

    if pid_alive(pid):
        # Ctrl+C first; SIGTERM if it hasn't exited within 3s.
        for sig in (signal.SIGINT, signal.SIGTERM):
            if pid_alive(pid):
                os.kill(pid, sig)
                wait_for_exit(pid, 3)
        time.sleep(0.3)  # let the window's shell finish exiting
    else:
        release(sid)  # stale state, clean it up
    close_window(meta.get("window_id"))
    remove(meta_path(sid))


def wait_for_exit(pid, seconds):
    end = time.time() + seconds
    while pid_alive(pid) and time.time() < end:
        time.sleep(0.1)


def cmd_stop_all():
    for sid in all_sessions():
        cmd_stop(sid)
    set_sleep_disabled(False)


def cmd_status():
    sessions = all_sessions()
    if not sessions:
        print("No active sessions.")
    for sid in sessions:
        state = "active" if session_active(sid) else "stale"
        print(f"{sid}  pid={read_pid(sid)}  {state}")
    print(f"SleepDisabled: {'1 (Mac will not sleep)' if sleep_disabled_now() else '0'}")


def hook_command(action):
    return f"{shlex.quote(PYTHON)} {shlex.quote(SCRIPT)} {action}"


def strip_our_hooks(hooks):
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            kept = [h for h in group.get("hooks", [])
                    if "caffeinate_claude.py" not in h.get("command", "")]
            if kept:
                groups.append({**group, "hooks": kept})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]


def load_settings():
    return read_json(SETTINGS_FILE) or {}


def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")


def cmd_install():
    settings = load_settings()
    hooks = settings.setdefault("hooks", {})
    strip_our_hooks(hooks)
    for event, action in HOOK_EVENTS.items():
        hooks.setdefault(event, []).append(
            {"hooks": [{"type": "command", "command": hook_command(action)}]})
    save_settings(settings)
    print(f"Installed hooks into {SETTINGS_FILE}")


def cmd_uninstall():
    settings = load_settings()
    strip_our_hooks(settings.get("hooks", {}))
    if not settings.get("hooks"):
        settings.pop("hooks", None)
    save_settings(settings)
    print(f"Removed hooks from {SETTINGS_FILE}")


def main():
    args = sys.argv[1:]
    action = args[0] if args else "help"
    try:
        if action == "start":
            cmd_start(hook_input())
        elif action == "stop" and "--all" in args:
            cmd_stop_all()
        elif action == "stop":
            hook = hook_input()
            cmd_stop(hook["session_id"], hook.get("error"))
        elif action == "panic":
            cmd_stop_all()
            print("All sessions stopped; sleep re-enabled.")
        elif action == "_window":
            cmd_window(args[1])
        elif action == "_close":
            time.sleep(1)  # let the window's process finish exiting
            close_window(args[1])
        elif action == "status":
            cmd_status()
        elif action == "install":
            cmd_install()
        elif action == "uninstall":
            cmd_uninstall()
        else:
            print(__doc__)
    except Exception as e:  # never break a Claude hook
        log(f"{action} error: {e!r}")
        if action not in ("start", "stop"):
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
