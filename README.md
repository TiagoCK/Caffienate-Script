# Caffeinate Script

Keeps your Mac awake **only while Claude Code is working**, including with the lid
closed on battery, then lets it sleep again as soon as the task is done.

- When you send Claude a prompt, a Terminal window titled **☕ Claude is working**
  opens and runs `caffeinate -ims`. It also runs `pmset -a disablesleep 1`, which is the
  only thing that stops lid-close sleep on battery.
- When Claude finishes, the script Ctrl+C's caffeinate, runs `pmset -a disablesleep 0`, and closes the window.

It runs automatically through Claude Code hooks, so it works in every Claude Code
instance (CLI and desktop app).

## Setup

### 1. Install the hooks

```bash
python3 caffeinate_claude.py install
```

This adds `UserPromptSubmit`, `Stop` and `SessionEnd` hooks to `~/.claude/settings.json`,
using absolute paths, so keep the script where it is (or re-run `install` after moving it).
`python3 caffeinate_claude.py uninstall` removes them.

### 2. Allow lid-closed-on-battery (one-time, needs your password)

Without this step you still get plain `caffeinate`, which **does not** stop sleep when you
close the lid on battery. Changing sleep settings requires root, so give the script a
passwordless sudo rule for **exactly these two commands and nothing else**:

```bash
sudo visudo -f /etc/sudoers.d/claude-caffeinate
```

Add this single line (replace `admin` with your username from `whoami`), then save:

```
admin ALL=(root) NOPASSWD: /usr/bin/pmset -a disablesleep 1, /usr/bin/pmset -a disablesleep 0
```

Check that it works (neither command should ask for a password):

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
| Battery ≤ 15% and discharging | stops by itself |
| 4 hours elapsed | stops by itself (in case a hook never fired) |
| You close the window or press Ctrl+C | stops and re-enables sleep |
| Several Claude sessions at once | sleep is re-enabled only when the **last** one stops |

The thresholds are constants at the top of `caffeinate_claude.py` (`BATTERY_MIN_PERCENT`, `MAX_HOURS`).

## Commands

```bash
python3 caffeinate_claude.py status
```

Lists active sessions and whether sleep is currently disabled.

```bash
python3 caffeinate_claude.py panic
```

Stops everything and forces sleep back on.

You can also check the setting directly with `pmset -g | grep SleepDisabled`. Logs are in
`~/.claude/caffeinate/log.txt`.

## Caveats

- With sleep disabled and the lid closed, the Mac keeps running in your bag. Claude's work is
  mostly waiting on the network and fairly light, but a long local build or test run
  can make it warm.
- `Stop` fires every time Claude finishes a reply, so the window opens and closes once per prompt.
