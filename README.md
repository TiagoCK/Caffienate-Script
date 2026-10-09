# Caffeinate Script

Keeps your Mac awake **only while Claude Code is working**, including with the lid
closed on battery, then lets it sleep again as soon as the task is done.

- When you send Claude a prompt, a background process (no window) runs `caffeinate -ims`
  and `pmset -a disablesleep 1`, which is the only thing that stops lid-close sleep on
  battery.
- When Claude finishes, the script Ctrl+C's caffeinate and runs `pmset -a disablesleep 0`.
- While Claude is **waiting on you** (a plan to approve, a question, or a permission
  prompt), it pauses: caffeinate stops and the Mac may sleep. Keep-awake comes back as
  soon as you answer.
- If Claude **hits a usage limit**, the Mac is allowed to sleep, a wake is scheduled for
  when the limit resets, and then Claude **resumes the task by itself** and the Mac stays
  awake until it's done. See [Usage limits](#usage-limits).

It runs automatically through Claude Code hooks, so it works in every Claude Code
instance (CLI and desktop app).

## The status window

One Terminal window, **Claude caffeinate**, shows every session (working, paused, or
waiting for a usage-limit reset), whether sleep is disabled, and recent log lines. It
opens only when it isn't already open (your first prompt after login, or after you've
closed it), with focus handed straight back to the app you were using. After that,
sending prompts never opens or moves a window.

- **Ctrl+C in the window** stops keeping the Mac awake right now for every session, and
  cancels scheduled resumes. The window stays open and shows idle. Your next prompt
  starts keeping awake again as normal; nothing needs restarting.
- **Closing the window** only hides it. Work in progress keeps going in the background,
  and the window reopens on your next prompt.

## Setup

### 1. Install the hooks

```bash
python3 caffeinate_claude.py install
```

This adds `UserPromptSubmit`, `Stop`, `StopFailure`, `SessionEnd`, `PreToolUse`
(`ExitPlanMode|AskUserQuestion`), `Notification` (`permission_prompt|elicitation_dialog`),
`PostToolUse` and `PostToolUseFailure` hooks to `~/.claude/settings.json`,
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

### 4. Sign the claude CLI in (one-time, for usage-limit resumes)

The automatic resume after a usage limit runs `claude` itself, and it can't use the
Claude desktop app's sign-in: the app signs in only the `claude` processes it starts.
Sign the CLI in once (a browser window opens; the login is kept in your keychain):

```bash
python3 caffeinate_claude.py login
```

Then confirm everything auto-resume needs is in place:

```bash
python3 caffeinate_claude.py check
```

## Safety nets

`disablesleep` is a global setting, so the script works hard to never leave it on:

| Trigger | What happens |
|---|---|
| Claude finishes (`Stop`) or the session ends (`SessionEnd`) | stops that session |
| Claude hits a usage limit (`StopFailure`, or spotted in the transcript as a backup) | lets the Mac sleep until the reset, then resumes (see below) |
| Claude stops on any other API error (`StopFailure`) | stops that session |
| Claude is waiting on you: plan approval, a question, or a permission prompt | pauses (Mac may sleep) until you answer |
| Battery ≤ 15% and discharging | stops by itself |
| 2 hours awake | stops by itself (in case a hook never fired); counted per awake stretch |
| You press Ctrl+C in the status window | stops every session and re-enables sleep |
| Several Claude sessions at once | sleep is re-enabled only when the **last** one stops (or pauses/waits) |

The thresholds are constants at the top of `caffeinate_claude.py` (`BATTERY_MIN_PERCENT`, `MAX_HOURS`).

## Usage limits

A session counts as having hit its usage limit in either of two ways:
- **Wrap-up allowance:** Claude Code tells Claude the limit is reached and gives it a
  short grace allowance to finish up, so the turn ends normally. The script spots that
  note in the session transcript when the turn ends.
- **Hard limit:** the turn is cut off with a rate-limit error (the `StopFailure` hook).

Then:

1. The session stops caffeinate, turns sleep back on, and the status window shows when
   it will wake. The reset time comes from Claude Code's record of a hard limit. After a
   wrap-up there's no recorded reset time, so the script sends one tiny request
   (`claude -p --no-session-persistence`, saved to no session). While the limit is in
   effect the request is rejected without using any of your usage, and the reply says
   when it resets.
2. **After a hard limit**, it schedules a wake (`pmset schedule wake`) for **3 minutes
   before the reset**, then keeps the Mac awake until **15 minutes after** it, waiting for
   **Claude Code's own Automatic continue**. That carries on inside the desktop app, so
   the work shows up live there. It can take about 10 minutes after the reset to kick in,
   and it gives up if the Mac was asleep when the limit reset, which is why the script
   wakes it first. If the app continues, the script just keeps the Mac awake for that turn.
3. **After a wrap-up**, the app doesn't continue on its own, so the script wakes the Mac
   90 seconds after the reset and goes straight to the background resume.
4. The background resume (also used if the app hasn't continued 15 minutes after a hard
   limit, or reports that it gave up) runs
   `claude -p --resume <session> --permission-mode <the session's current mode>` with the
   prompt *"Your usage limit has reset. Continue the task…"*. Its output is saved to
   `~/.claude/caffeinate/<session-id>.resume.out`. It uses the same `claude` binary the
   session was running, or the newest copy bundled with the Claude app if that one has
   since been updated away.
5. When that finishes, the Mac is allowed to sleep again. If it hits the limit again
   (e.g. the weekly limit), it repeats, up to 3 times.

**Plan mode:** a session in plan mode is never resumed in the background, since nothing
can get past plan approval without you. After a hard limit the app's own continue still
gets its chance (it shows the plan for your approval in the app). After a wrap-up the
Mac isn't woken for it at all, and the log says to continue it yourself after the reset.
The mode is tracked as it changes, so a session whose plan you approved counts as
whatever mode it switched to.

The log (`~/.claude/caffeinate/log.txt`) says which one happened: "Claude continued in
the app", "the app didn't continue within 15 min of the reset" followed by the resume,
or the plan-mode message, along with Claude Code's own `quota_auto_resume_*` events.

Ways it's cancelled:
- You type into the session yourself before the reset. You've taken over, so the
  scheduled resume is dropped.
- You press Ctrl+C in the status window, or run `stop --all` or `panic`.
- The battery is at or below 15% when it wakes. It skips the resume and lets the Mac sleep.

Quitting the Claude app does **not** cancel it; the resume runs on its own.

**Permissions:** the resume uses the permission mode the session was in. With **default**
mode nobody is there to click Allow, so tools that need approval are denied and Claude may
stop and say what it needed. For unattended work, run the session in `acceptEdits` or
`auto` mode.

**After a background resume:** that work runs as a separate `claude` process on the same
session. The desktop app keeps its own copy of an open session and has no way for another
program to refresh it, so it won't show that work until you reopen the session (quitting
and reopening the app does it). Do that before typing in the session again; otherwise the
app continues from its older copy and Claude won't know what the background run did.
To look without the app, run
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
python3 caffeinate_claude.py check
```

Checks everything auto-resume depends on: the `claude` CLI, its sign-in, the sudo rules,
and the hooks.

```bash
python3 caffeinate_claude.py status
```

Lists active, paused and waiting sessions (with their resume time) and whether sleep is
currently disabled, and whether the status window is open. `pmset -g sched` shows
scheduled wake-ups.

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
- The status window's command starts with a space so it stays out of your shell history (up arrow).
  That relies on zsh's `hist_ignore_space` option (or bash's `HISTCONTROL=ignorespace`).
