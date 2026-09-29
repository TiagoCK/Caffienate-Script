#!/usr/bin/env python3
"""Keep a Mac awake (even lid-closed on battery) only while Claude Code is working.

Driven by Claude Code hooks:
    UserPromptSubmit -> start   (opens a background Terminal window running caffeinate)
    Stop / StopFailure / SessionEnd -> stop   (Ctrl+C's caffeinate, closes the window)
    PreToolUse(ExitPlanMode|AskUserQuestion), Notification(permission prompt) -> pause
    PostToolUse / PostToolUseFailure -> resume

While paused (a plan, question or permission prompt is waiting on you) caffeinate is
stopped and the Mac may sleep; keep-awake comes back once you answer.

If Claude hits its usage limit, the window lets the Mac sleep, schedules a wake for
when the limit resets (`pmset schedule wake`), then resumes the session headlessly
with `claude -p --resume` and keeps the Mac awake until that finishes. The limit is
detected through the StopFailure hook and, as a fallback, by watching the transcript.

Lid-closed-on-battery sleep can only be overridden with `pmset -a disablesleep 1`,
which needs a passwordless sudoers rule (see README). Sleep is re-enabled as soon
as the last working session stops or waits, the battery gets low, or MAX_HOURS elapses.

Usage:
    caffeinate_claude.py start | stop [--all] | status | panic | install | uninstall
    caffeinate_claude.py test-wake <minutes>
"""

import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta

STATE_DIR = os.path.expanduser("~/.claude/caffeinate")
LOG_FILE = os.path.join(STATE_DIR, "log.txt")
SETTINGS_FILE = os.path.expanduser("~/.claude/settings.json")
SCRIPT = os.path.abspath(__file__)
# System python survives Homebrew upgrades; fall back to whatever is running us.
PYTHON = "/usr/bin/python3" if os.path.exists("/usr/bin/python3") else sys.executable
PMSET = "/usr/bin/pmset"

BATTERY_MIN_PERCENT = 15  # stop keeping awake at or below this (when discharging)
MAX_HOURS = 2  # safety cap per awake stretch, in case a Stop hook never fires
BATTERY_CHECK_SECONDS = 60
STARTUP_GRACE_SECONDS = 30  # a session is "active" this long before its pid appears
TRANSCRIPT_CHECK_SECONDS = 10
LIMIT_ENTRY_WAIT_SECONDS = 15  # after StopFailure, how long to wait for the transcript
WAKE_DELAY_SECONDS = 90  # wake this long after the reset, to be safely past it
MAX_WAIT_DAYS = 7  # don't schedule resumes further out than this
MAX_RESUMES = 3  # give up after this many limit -> resume cycles
TEST_WAKE_AWAKE_SECONDS = 60
CHILD_ENV = "CAFFEINATE_CLAUDE_CHILD"  # set for the resumed claude; its hooks no-op
RESUME_PROMPT = ("Your usage limit has reset. Continue the task you were working on "
                 "from where you left off.")

# (event, matcher or None, action)
HOOKS = [
    ("UserPromptSubmit", None, "start"),
    ("Stop", None, "stop"),
    ("StopFailure", None, "stop"),  # turn ended on an API error (usage limit, etc.)
    ("SessionEnd", None, "stop"),
    # Waiting on you: a plan to approve, a question, or a permission prompt.
    ("PreToolUse", "ExitPlanMode|AskUserQuestion", "pause"),
    ("Notification", "permission_prompt|elicitation_dialog", "pause"),
    # You answered and the tool ran (or was rejected/denied): back to work.
    ("PostToolUse", None, "resume"),
    ("PostToolUseFailure", None, "resume"),
]


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


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def update_meta(sid, **fields):
    meta = read_json(meta_path(sid))
    if meta is not None:
        meta.update(fields)
        write_json(meta_path(sid), meta)


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


def session_waiting(sid):
    meta = read_json(meta_path(sid)) or {}
    return meta.get("state") == "waiting" and pid_alive(read_pid(sid))


def session_state(sid):
    """'working', 'paused' (waiting on you), 'waiting' (usage-limit reset), or None
    if the session's window process isn't running."""
    if not pid_alive(read_pid(sid)):
        return None
    return (read_json(meta_path(sid)) or {}).get("state", "working")


def session_active(sid):
    """Active = keeping the Mac awake: its window process is alive and working
    (not paused or waiting for a reset), or it was started moments ago."""
    meta = read_json(meta_path(sid)) or {}
    if pid_alive(read_pid(sid)):
        return meta.get("state", "working") == "working"
    return time.time() - meta.get("started", 0) < STARTUP_GRACE_SECONDS


def others_active(sid):
    return any(session_active(s) for s in all_sessions() if s != sid)


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


def schedule_wake(when, cancel=False):
    """Schedule (or cancel) a system wake at epoch time `when`. Returns success."""
    stamp = time.strftime("%m/%d/%y %H:%M:%S", time.localtime(when))
    cmd = ["sudo", "-n", PMSET, "schedule"] + (["cancel"] if cancel else []) + ["wake", stamp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(f"pmset schedule {'cancel ' if cancel else ''}wake {stamp} failed "
            f"(sudoers rule missing?): {r.stderr.strip()}")
    return r.returncode == 0


def battery_status():
    """Returns (percent, discharging) or (None, False) if there's no battery."""
    out = subprocess.run([PMSET, "-g", "batt"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)%;\s*([\w ]+?);", out)
    if not m:
        return None, False
    return int(m.group(1)), m.group(2).strip() == "discharging"


def battery_low():
    pct, discharging = battery_status()
    if discharging and pct is not None and pct <= BATTERY_MIN_PERCENT:
        return pct
    return None


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


def set_title(text):
    print(f"\033]0;{text}\a", end="", flush=True)


def fmt_time(epoch):
    return time.strftime("%-I:%M %p", time.localtime(epoch))


def find_claude(override=None):
    """The claude CLI: $CAFFEINATE_CLAUDE_BIN (captured at start), PATH,
    ~/.claude/local, or the newest copy bundled with the Claude desktop app."""
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    local = os.path.expanduser("~/.claude/local/claude")
    if os.access(local, os.X_OK):
        return local
    bundled = glob.glob(os.path.expanduser(
        "~/Library/Application Support/Claude/claude-code/*/claude.app/Contents/MacOS/claude"))

    def version(path):
        v = path.split("/claude-code/")[1].split("/")[0]
        return [int(p) if p.isdigit() else 0 for p in v.split(".")]

    return max(bundled, key=version) if bundled else None


# ---------------------------------------------------------------- usage limits

def usage_limit_entry(transcript, offset):
    """Scan transcript lines appended after `offset` for a usage-limit error.

    Returns (entry or None, new_offset). Only complete lines are consumed.
    """
    try:
        with open(transcript, "rb") as f:
            f.seek(offset)
            chunk = f.read()
    except (OSError, TypeError):
        return None, offset
    end = chunk.rfind(b"\n") + 1
    found = None
    for line in chunk[:end].splitlines():
        if b"isApiErrorMessage" not in line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if entry.get("isApiErrorMessage") and entry.get("error") == "rate_limit":
            found = entry  # keep the latest one
    return found, offset + end


def reset_time(entry):
    """Epoch time the usage limit resets, from a transcript error entry, or None."""
    resets_at = (entry.get("quotaLimits") or {}).get("resetsAt")
    if isinstance(resets_at, (int, float)):
        return float(resets_at)
    content = (entry.get("message") or {}).get("content") or []
    text = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
    return parse_reset_text(text)


def parse_reset_text(text, now=None):
    """Parse e.g. 'resets 1:20pm (America/New_York)' or 'resets Oct 1, 5pm (UTC)'."""
    m = re.search(r"resets\s+(?:([A-Z][a-z]{2})\s+(\d{1,2}),?\s+(?:at\s+)?)?"
                  r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)", text, re.I)
    if not m:
        return None
    month, day, hour, minute, ampm, tz = m.groups()
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo(tz)
    except Exception:
        return None
    hour = int(hour) % 12 + (12 if ampm.lower() == "pm" else 0)
    now = now or datetime.now(zone)
    t = now.replace(hour=hour, minute=int(minute or 0), second=0, microsecond=0)
    if month:
        t = t.replace(month=datetime.strptime(month.title(), "%b").month, day=int(day))
        if t < now - timedelta(days=1):
            t = t.replace(year=t.year + 1)
    elif t <= now:
        t += timedelta(days=1)
    return t.timestamp()


# ---------------------------------------------------------------- commands

def cmd_start(hook, extra=None):
    sid = hook["session_id"]
    if session_waiting(sid):
        # You came back and typed before the reset: take over from the scheduled resume.
        log(f"{sid}: new prompt while waiting for reset; cancelling scheduled resume")
        cmd_stop(sid, force=True, reason="new prompt; scheduled resume cancelled")
    elif session_state(sid) == "paused":
        cmd_resume(sid, "new prompt")
        update_meta(sid, **{k: hook[k] for k in ("cwd", "permission_mode") if k in hook})
        return
    elif session_active(sid):
        # Keep what the resume needs current (permission mode can change mid-session).
        update_meta(sid, **{k: hook[k] for k in ("cwd", "permission_mode") if k in hook})
        return
    os.makedirs(STATE_DIR, exist_ok=True)
    write_json(meta_path(sid), {
        "started": time.time(), "window_id": None, "state": "working",
        "transcript_path": hook.get("transcript_path"),
        "cwd": hook.get("cwd"), "permission_mode": hook.get("permission_mode"),
        "claude_bin": os.environ.get("CAFFEINATE_CLAUDE_BIN"),
        **(extra or {}),
    })

    shell_cmd = f"exec {shlex.quote(PYTHON)} {shlex.quote(SCRIPT)} _window {shlex.quote(sid)}"
    # Open the window, then hand focus straight back to whatever was in front, so the
    # window stays open behind it. Terminal's previous front window is re-raised too:
    # otherwise typing in Terminal (now or when you next switch to it) would land in,
    # and garble, the new window.
    window_id = osascript(
        "on run argv",
        "set prevApp to path to frontmost application as text",
        "set inTerminal to prevApp ends with \"Terminal.app:\"",
        'tell application "Terminal"',
        "set prevWin to missing value",
        "if (count of windows) > 0 then set prevWin to id of front window",
        "do script (item 1 of argv)",
        "set wid to id of front window",
        "if prevWin is not missing value then set index of window id prevWin to 1",
        "end tell",
        "if not inTerminal then",
        "try",
        "tell application prevApp to activate",
        "end try",
        "end if",
        "return wid",
        "end run",
        args=[shell_cmd],
    )
    update_meta(sid, window_id=int(window_id) if window_id.isdigit() else None)


def cmd_pause(sid, trigger):
    """Claude is waiting on you: let the window process stop keeping the Mac awake."""
    if session_state(sid) != "working":
        return
    if not (read_json(meta_path(sid)) or {}).get("pausable"):
        return  # window started by an older version: SIGUSR2 would kill it
    update_meta(sid, state="paused")
    os.kill(read_pid(sid), signal.SIGUSR2)
    log(f"{sid}: pause ({trigger})")


def cmd_resume(sid, trigger):
    """You answered: keep the Mac awake again. Fast no-op unless paused, since
    PostToolUse fires on every tool call."""
    if session_state(sid) != "paused":
        return
    update_meta(sid, state="working")
    os.kill(read_pid(sid), signal.SIGUSR2)
    log(f"{sid}: resume ({trigger})")


class Watch:
    """Mutable state shared between the window loop and its signal handlers."""

    def __init__(self, sid, transcript):
        self.sid = sid
        self.caf = None  # the running caffeinate, None while paused
        self.title = "Claude is working"
        self.state_changed = False  # set by SIGUSR2: pause/resume
        self.transcript = transcript
        try:
            self.offset = os.path.getsize(transcript) if transcript else 0
        except OSError:
            self.offset = 0
        self.check_by = None  # set by SIGUSR1: expect a limit entry by this time

    def limit(self):
        """Returns the reset time if a usage-limit entry has appeared, else None.
        Returns 0 for a limit whose reset time couldn't be determined."""
        entry, self.offset = usage_limit_entry(self.transcript, self.offset)
        if entry is None:
            return None
        return reset_time(entry) or 0


def keep_awake(title):
    set_title(title)
    ok = set_sleep_disabled(True)
    return subprocess.Popen(["caffeinate", "-ims"]), ok


def stop_process(proc, sig=signal.SIGINT):
    if proc is not None and proc.poll() is None:
        proc.send_signal(sig)  # the Ctrl+C
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


def apply_pause_state(w):
    """Handle a SIGUSR2: stop or restart caffeinate to match the session state.
    Returns True if keep-awake was (re)started."""
    state = (read_json(meta_path(w.sid)) or {}).get("state")
    if state == "paused" and w.caf is not None:
        stop_process(w.caf)
        w.caf = None
        set_title("Paused: waiting for your answer")
        if not others_active(w.sid):
            set_sleep_disabled(False)
        print("Paused: Claude is waiting on you — sleep allowed until you answer.", flush=True)
    elif state == "working" and w.caf is None:
        w.caf, _ = keep_awake(w.title)
        print("Back to work — keeping awake.", flush=True)
        return True
    return False


def watch_working(w, child=None):
    """Loop while Claude works. Returns ("limit", resets_at) or ("done", reason).
    The interactive case ends via SIGINT from the Stop hook (KeyboardInterrupt)."""
    deadline = time.time() + MAX_HOURS * 3600
    next_battery = next_transcript = 0
    while True:
        now = time.time()
        if w.state_changed:
            w.state_changed = False
            if apply_pause_state(w):
                deadline = now + MAX_HOURS * 3600  # time paused doesn't count
        if w.caf is None:  # paused: nothing to watch until you answer
            time.sleep(1)
            continue
        if child is not None and child.poll() is not None:
            resets_at = w.limit() if w.transcript else None
            if resets_at is not None:
                return "limit", resets_at
            return "done", f"resumed task finished (exit {child.returncode})"
        if w.caf.poll() is not None:
            return "done", "caffeinate exited"
        if now >= deadline:
            return "done", f"reached {MAX_HOURS}h safety cap"
        if now >= next_battery:
            next_battery = now + BATTERY_CHECK_SECONDS
            pct = battery_low()
            if pct is not None:
                return "done", f"battery low ({pct}%)"
        if w.transcript and (now >= next_transcript or w.check_by):
            next_transcript = now + TRANSCRIPT_CHECK_SECONDS
            resets_at = w.limit()
            if resets_at is not None:
                w.check_by = None
                return "limit", resets_at
        if w.check_by and now >= w.check_by:
            return "done", "usage limit (reset time unknown)"
        time.sleep(1)


def wait_for_reset(sid, resets_at):
    """Let the Mac sleep until the limit resets; a scheduled wake gets us back."""
    wake_at = resets_at + WAKE_DELAY_SECONDS
    update_meta(sid, state="waiting", resets_at=resets_at, wake_at=wake_at)
    set_title(f"Usage limit — resuming at {fmt_time(wake_at)}")
    if not others_active(sid):
        set_sleep_disabled(False)
    scheduled = schedule_wake(wake_at)
    print(f"\nUsage limit hit. Sleeping allowed; resuming at {fmt_time(wake_at)}.")
    if not scheduled:
        print("   Couldn't schedule a wake (see README sudoers rule) — the resume "
              "will run the next time the Mac is awake after that.")
    print("   Ctrl+C to cancel the resume.", flush=True)
    log(f"{sid}: usage limit; waiting until {fmt_time(wake_at)} "
        f"(wake {'scheduled' if scheduled else 'NOT scheduled'})")
    try:
        # time.sleep pauses while the Mac sleeps, so poll the wall clock.
        while time.time() < wake_at:
            time.sleep(5)
    except BaseException:
        if scheduled:
            schedule_wake(wake_at, cancel=True)
        raise
    update_meta(sid, state="working")


def cmd_window(sid):
    """Runs inside the Terminal window: holds caffeinate, the safety guards, and
    the usage-limit wait/resume cycle."""
    with open(pid_path(sid), "w") as f:
        f.write(str(os.getpid()))
    meta = read_json(meta_path(sid)) or {}
    w = Watch(sid, meta.get("transcript_path"))

    def _exit(signum, _frame):
        raise SystemExit(signum)

    def _check_limit(_signum, _frame):  # StopFailure(rate_limit) says: look now
        w.check_by = time.time() + LIMIT_ENTRY_WAIT_SECONDS

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGHUP, _exit)  # window closed by hand
    def _state_changed(_signum, _frame):  # pause/resume
        w.state_changed = True

    signal.signal(signal.SIGUSR1, _check_limit)
    signal.signal(signal.SIGUSR2, _state_changed)
    update_meta(sid, pausable=True)  # only now is SIGUSR2 safe to send us

    child = None
    phase = "working"
    reason = "stopped"
    resumes = 0
    try:
        if meta.get("test_resets_at"):
            outcome = ("limit", meta["test_resets_at"])
        else:
            w.caf, ok = keep_awake(w.title)
            if ok:
                print("Sleep disabled (lid-close on battery too) while Claude works.")
            else:
                print("caffeinate only — lid-close sleep NOT blocked on battery.\n"
                      "   Set up the sudoers rule from the README to enable that.")
            print(f"   Auto-stops when Claude finishes, battery ≤ {BATTERY_MIN_PERCENT}%, "
                  f"or after {MAX_HOURS}h. On a usage limit it sleeps until the reset, "
                  f"then resumes.  Ctrl+C to stop now.", flush=True)
            outcome = watch_working(w)

        while outcome[0] == "limit":
            resets_at = outcome[1]
            if child is not None:  # a resumed run hit the limit again; let it exit
                try:
                    child.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    stop_process(child, signal.SIGTERM)
                child = None
            stop_process(w.caf)
            w.caf = None
            if not resets_at:
                outcome = ("done", "usage limit (reset time unknown)")
                break
            if resets_at - time.time() > MAX_WAIT_DAYS * 86400:
                outcome = ("done", f"usage limit resets too far out ({fmt_time(resets_at)})")
                break
            if resumes >= MAX_RESUMES:
                outcome = ("done", f"usage limit hit again after {MAX_RESUMES} resumes")
                break
            phase = "waiting"
            wait_for_reset(sid, resets_at)
            phase = "resuming"
            resumes += 1

            w.title = "Claude is resuming"
            w.caf, ok = keep_awake(w.title)
            print(f"\nWoke at {fmt_time(time.time())}; "
                  f"sleep {'disabled' if ok else 'NOT disabled'}.", flush=True)
            pct = battery_low()
            if pct is not None:
                outcome = ("done", f"battery low ({pct}%), skipped resume")
                break
            if meta.get("test_resets_at"):
                log(f"{sid}: woke OK")
                print(f"Test wake worked. Staying awake {TEST_WAKE_AWAKE_SECONDS}s.",
                      flush=True)
                time.sleep(TEST_WAKE_AWAKE_SECONDS)
                outcome = ("done", "test wake finished")
                break

            meta = read_json(meta_path(sid)) or meta
            claude = find_claude(meta.get("claude_bin"))
            if not claude:
                outcome = ("done", "claude CLI not found, skipped resume")
                break
            cmd = [claude, "-p", "--resume", sid]
            if meta.get("permission_mode"):
                cmd += ["--permission-mode", meta["permission_mode"]]
            cmd.append(RESUME_PROMPT)
            cwd = meta.get("cwd") if os.path.isdir(meta.get("cwd") or "") else None
            log(f"{sid}: resuming ({shlex.join(cmd[1:-1])}) in {cwd}")
            print(f"Resuming Claude session (mode: {meta.get('permission_mode')})…\n",
                  flush=True)
            child = subprocess.Popen(cmd, cwd=cwd or os.path.expanduser("~"),
                                     env={**os.environ, CHILD_ENV: "1"},
                                     stdin=subprocess.DEVNULL)
            outcome = watch_working(w, child)
        reason = outcome[1]
    except (KeyboardInterrupt, SystemExit) as e:
        # cmd_stop records why before signalling us; no record means the signal
        # came from you (Ctrl+C in the window, or closing it).
        reason = (read_json(meta_path(sid)) or {}).get("stop_reason")
        if not reason and isinstance(e, KeyboardInterrupt):
            reason = "Stopped with Ctrl+C " + {
                "working": ("while waiting for your answer" if w.caf is None
                            else "while Claude was working"),
                "waiting": "while waiting for the usage-limit reset (resume cancelled)",
                "resuming": "while resuming",
            }[phase]
        elif not reason:
            reason = {signal.SIGHUP: "Terminal window closed",
                      signal.SIGTERM: "terminated"}.get(e.code, f"stopped by signal {e.code}")
    finally:
        stop_process(child, signal.SIGTERM)
        stop_process(w.caf)
        window_id = (read_json(meta_path(sid)) or meta).get("window_id")
        release(sid)
        log(f"{sid}: {reason}")
        print(f"\n{reason} — sleep re-enabled.", flush=True)
        # Close our own window once this process has exited (needed when we stop
        # ourselves: usage limit, battery, cap). Harmless if `stop` closes it first.
        if window_id:
            subprocess.Popen([PYTHON, SCRIPT, "_close", str(window_id)],
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def release(sid):
    remove(pid_path(sid))
    remove(meta_path(sid))
    if not any(session_active(s) for s in all_sessions()):
        set_sleep_disabled(False)


def cmd_stop(sid, error=None, event=None, force=False, reason=None):
    meta = read_json(meta_path(sid))
    if meta is None:
        return
    if not force:
        if event and session_waiting(sid):
            return  # waiting for a usage-limit reset: the resume outlives the session
        if error == "rate_limit" and pid_alive(read_pid(sid)):
            log(f"{sid}: turn ended with usage limit; checking reset time")
            os.kill(read_pid(sid), signal.SIGUSR1)
            return
    if not reason:
        reason = {
            "Stop": "Claude finished",
            "StopFailure": f"turn ended with API error: {error}",
            "SessionEnd": "Claude session ended",
        }.get(event, "stopped by command")
    # A very short task can end before the window process has written its pid.
    for _ in range(30):
        pid = read_pid(sid)
        if pid or not session_active(sid):
            break
        time.sleep(0.1)

    if pid_alive(pid):
        update_meta(sid, stop_reason=reason)  # so the window reports the real cause
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
        cmd_stop(sid, force=True, reason="stopped by stop --all/panic")
    set_sleep_disabled(False)


def cmd_test_wake(minutes):
    """Simulate a usage limit that resets in `minutes` (no Claude resume)."""
    resets_at = time.time() + minutes * 60 - WAKE_DELAY_SECONDS
    cmd_start({"session_id": "test-wake"}, extra={"test_resets_at": resets_at})
    print(f"Test wake scheduled for {fmt_time(resets_at + WAKE_DELAY_SECONDS)}. Unplug, "
          f"close the lid, and afterwards look for 'woke OK' in {LOG_FILE}")


def cmd_status():
    sessions = all_sessions()
    if not sessions:
        print("No active sessions.")
    for sid in sessions:
        meta = read_json(meta_path(sid)) or {}
        if session_waiting(sid):
            state = f"waiting for usage-limit reset, resuming at {fmt_time(meta['wake_at'])}"
        elif session_state(sid) == "paused":
            state = "paused (Claude is waiting on you)"
        else:
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
    for event, matcher, action in HOOKS:
        group = {"hooks": [{"type": "command", "command": hook_command(action)}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
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
    if action in ("start", "stop", "pause", "resume") and os.environ.get(CHILD_ENV):
        return 0  # a resumed run: its window process manages keep-awake itself
    try:
        if action == "start":
            cmd_start(hook_input())
        elif action == "stop" and "--all" in args:
            cmd_stop_all()
        elif action == "stop":
            hook = hook_input()
            cmd_stop(hook["session_id"], hook.get("error"), hook.get("hook_event_name"))
        elif action == "pause":
            hook = hook_input()
            cmd_pause(hook["session_id"], hook.get("tool_name")
                      or hook.get("notification_type") or hook.get("hook_event_name"))
        elif action == "resume":
            hook = hook_input()
            cmd_resume(hook["session_id"], hook.get("tool_name")
                       or hook.get("hook_event_name"))
        elif action == "panic":
            cmd_stop_all()
            print("All sessions stopped; sleep re-enabled.")
        elif action == "_window":
            cmd_window(args[1])
        elif action == "_close":
            time.sleep(1)  # let the window's process finish exiting
            close_window(args[1])
        elif action == "test-wake":
            cmd_test_wake(float(args[1]) if len(args) > 1 else 3)
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
        if action not in ("start", "stop", "pause", "resume"):
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
