# Caffeinate Script

Keeps your Mac awake **only while Claude Code is working**, including with the lid
closed on battery, then lets it sleep again as soon as the task is done.

- When you send Claude a prompt, a Terminal window titled **☕ Claude is working**
  opens and runs `caffeinate -ims`. It also runs `pmset -a disablesleep 1`, which is the
  only thing that stops lid-close sleep on battery.
- When Claude finishes, the script Ctrl+C's caffeinate, runs `pmset -a disablesleep 0`, and closes the window.
- If Claude **hits a usage limit**, the Mac is allowed to sleep, a wake is scheduled for
  when the limit resets, and then Claude **resumes the task by itself** and the Mac stays
  awake until it's done. See [Usage limits](#usage-limits).

It runs automatically through Claude Code hooks, so it works in every Claude Code
instance (CLI and desktop app).

## Setup

### 1. Install the hooks

```bash
python3 caffeinate_claude.py install
```

This adds `UserPromptSubmit`, `Stop`, `StopFailure` and `SessionEnd` hooks to `~/.claude/settings.json`,
using absolute paths, so keep the script where it is (or re-run `install` after moving it).
`python3 caffeinate_claude.py uninstall` removes them.

### 2. Allow lid-closed-on-battery (one-time, needs your password)

Without this step you still get plain `caffeinate`, which **does not** stop sleep when you
close the lid on battery. Changing sleep settings requires root, so give the script a
passwordless sudo rule for **only these `pmset` commands and nothing else**: turning
sleep off/on, and scheduling/cancelling a wake-up (for usage-limit resumes):

```bash
sudo visudo -f /etc/sudoers.d/claude-caffeinate
```

Add this single line (replace `admin` with your username from `whoami`), then save:

```
admin ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, /usr/bin/pmset -a disablesleep 0, /usr/bin/pmset schedule wake *, /usr/bin/pmset schedule cancel wake *
```

The `*` lets the script pass a wake-up time. It can only schedule or cancel wake-ups,
not change any other power setting.

Check that it works (it shouldn't ask for a password):

```bash
sudo -n /usr/bin/pmset -a disablesleep 0 && echo ok
```

### 3. Allow Terminal automation

The first time it runs, macOS asks whether Claude/your terminal may control **Terminal**.
Click **Allow**.

## Safety nets

`disablesleep` is a global setting, so the script works hard to never leave it on:

| Trigger | What happens |
|---|---|
| Claude finishes (`Stop`) or the session ends (`SessionEnd`) | stops that session |
| Claude hits a usage limit (`StopFailure`, or spotted in the transcript as a backup) | lets the Mac sleep until the reset, then resumes (see below) |
| Claude stops on any other API error (`StopFailure`) | stops that session |
| Battery ≤ 15% and discharging | stops by itself |
| 2 hours awake | stops by itself (in case a hook never fired); counted per awake stretch |
| You close the window or press Ctrl+C | stops and re-enables sleep |
| Several Claude sessions at once | sleep is re-enabled only when the **last** one stops (or starts waiting) |

The thresholds are constants at the top of `caffeinate_claude.py` (`BATTERY_MIN_PERCENT`, `MAX_HOURS`).

## Usage limits

When a session hits its usage limit:

1. The window stops caffeinate, turns sleep back on, and retitles itself
   **💤 Usage limit — resuming at 1:21 PM**. The reset time comes from Claude Code's
   own record of the limit, and the resume is set for 90 seconds after it.
2. It schedules a wake with `pmset schedule wake`, so the Mac wakes itself up.
3. At that time it turns sleep off again, restarts caffeinate, and runs
   `claude -p --resume <session> --permission-mode <the session's mode>` with the prompt
   *"Your usage limit has reset. Continue the task…"*. Output shows in the window.
4. When that finishes, the Mac is allowed to sleep again and the window closes. If it hits
   the limit again (e.g. the weekly limit), it repeats, up to 3 times.

Ways it's cancelled:
- You type into the session yourself before the reset. You've taken over, so the
  scheduled resume is dropped.
- You press Ctrl+C in the window, or run `stop --all` or `panic`.
- The battery is at or below 15% when it wakes. It skips the resume and lets the Mac sleep.

Quitting the Claude app does **not** cancel it; the resume runs on its own.

**Permissions:** the resume uses the permission mode the session was in. With **default**
mode nobody is there to click Allow, so tools that need approval are denied and Claude may
stop and say what it needed. For unattended work, run the session in `acceptEdits` or
`auto` mode.

**Where to see what it did:** the resumed work runs as a separate `claude` process on the
same session, so the desktop app won't show it live. Reopen the session afterwards, or run
`claude --resume <session-id>`.

**Test that waking works on your Mac.** macOS decides how a lid-closed, on-battery wake
behaves, so check it once:

```bash
python3 caffeinate_claude.py test-wake 3
```

Then unplug, close the lid, and wait about 5 minutes. Open it and look for `woke OK` in
`~/.claude/caffeinate/log.txt`, or run `pmset -g log | grep -i wake`. The test doesn't run
Claude; it just wakes, logs, and stays up 60 seconds.

## Commands

```bash
python3 caffeinate_claude.py status
```

Lists active and waiting sessions (with their resume time) and whether sleep is
currently disabled. `pmset -g sched` shows scheduled wake-ups.

```bash
python3 caffeinate_claude.py panic
```

Stops everything, including scheduled resumes, and forces sleep back on.

You can also check the setting directly with `pmset -g | grep SleepDisabled`. Logs are in
`~/.claude/caffeinate/log.txt`.

## Caveats

- With sleep disabled and the lid closed, the Mac keeps running in your bag. Claude's work is
  mostly waiting on the network and fairly light, but a long local build or test run
  can make it warm.
- `Stop` fires every time Claude finishes a reply, so the window opens and closes once per prompt.
