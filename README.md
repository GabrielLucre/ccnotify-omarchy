![Notification Screenshot](alert.png)

# ccnotify-omarchy

Desktop notifications for Claude Code on Arch Linux + Hyprland ([Omarchy](https://omarchy.org)), alerting you when Claude needs your input or completes tasks.

This is a Linux port of [dazuiba/CCNotify](https://github.com/dazuiba/CCNotify) (macOS). All credit for the original idea and implementation goes to its authors. This fork swaps `terminal-notifier` for `notify-send` and uses `hyprctl` for click-to-focus.
## Important Notes
Starting from claude-code v1.0.95 (2025-08-31), any invalid settings in `~/.claude/settings.json` will disable hooks. See [Why not working](#why-not-working) for solutions.

## Features

- 🔔 **Get notified** when Claude requires your input or completes a task.
- 🔗 **Click to jump back**: clicking the notification focuses the terminal window running that Claude session (Hyprland).
- ⏱️ **Task Duration**: Displays started time, and how long the task took to complete

**Note**: This fork targets Linux (Arch Linux + Hyprland, e.g. Omarchy). Notifications go through `notify-send`, so any Freedesktop notification daemon (mako, swaync, dunst, Quickshell…) will display them. Click-to-focus requires Hyprland.


## Installation Guide

### 1. Install CCNotify
```bash
git clone https://github.com/GabrielLucre/ccnotify-omarchy.git
cd ccnotify-omarchy

# Create the directory if it doesn't exist
mkdir -p ~/.claude/ccnotify

# soft link ccnotify.py to the directory
ln -f ccnotify.py ~/.claude/ccnotify/

chmod a+x ~/.claude/ccnotify/ccnotify.py

# run this script, should print: ok
~/.claude/ccnotify/ccnotify.py

ok

```
### 2. Install dependencies
ccnotify uses `notify-send` (from `libnotify`) to send notifications:

```bash
sudo pacman -S --needed libnotify python
```

Test it manually — a notification should pop up:

```bash
~/.claude/ccnotify/ccnotify.py "Test" "Claude Code finished the task."
```

On Hyprland, clicking the notification focuses the terminal it was sent from. This works by walking up the process tree to a window listed by `hyprctl clients`, so it does not work when Claude runs inside tmux/zellij, or in terminals that serve every window from one process (e.g. `foot --server`). It also needs a notification daemon that supports actions.

### 3. Configure Claude Hooks
Add the following hooks to your Claude configuration to enable ccnotify.

If your `settings.json` already has a `"hooks"` key, don't paste a second one: merge these events into it. If an event already has hooks, append the ccnotify command to its existing `"hooks"` array. An invalid `settings.json` disables all hooks.

 ~/.claude/settings.json 
 
```json

  "hooks": {
  "UserPromptSubmit": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "~/.claude/ccnotify/ccnotify.py UserPromptSubmit"
        }
      ]
    }
  ],
  "Stop": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "~/.claude/ccnotify/ccnotify.py Stop"
        }
      ]
    }
  ],
  "Notification": [
    {
      "hooks": [
        {
          "type": "command",
          "command": "~/.claude/ccnotify/ccnotify.py Notification"
        }
      ]
    }
  ]
}

```

## Try It Out

To verify the notification system works, start a new Claude Code session and run:
```
after 1 second, echo 'hello'
```
You should see a desktop notification appear.

## Why not working
1. Ensure hooks configuration is active. Here's an example where other configurations prevent hooks from working:

`claude -p --model haiku -d hooks --verbose "hi"`

Expected output:

```
[DEBUG] Found 1 hook commands to execute
[DEBUG] Executing hook command: ~/.claude/ccnotify/ccnotify.py UserPromptSubmit with timeout 60000ms
[DEBUG] Hook command completed with status 0: ~/.claude/ccnotify/ccnotify.py UserPromptSubmit
```

Actual output:

```
[DEBUG] Invalid settings in userSettings source - key: permissions.allow.0, error:.....
[DEBUG] Found 0 hook commands to execute
```
Reason: In September 2025, claude-code strengthened validation rules for settings.json. Any invalid configuration will disable hooks.
You need to modify the relevant configurations in `~/.claude/settings.json` until the `Invalid settings` error stops appearing.



## How It Works

ccnotify tracks Claude sessions and provides notifications at key moments:

- **When you submit a prompt**: Records the start time and project context
- **When Claude completes**: Calculates duration and sends a completion notification
- **When Claude waits for input**: Immediately alerts you that input is needed

All activity is logged to `~/.claude/ccnotify/ccnotify.log` and session data is stored in `~/.claude/ccnotify/ccnotify.db` locally. No data is uploaded or shared externally.


## Uninstall

Edit `~/.claude/settings.json` and remove all hook commands related to `ccnotify`.

Remove all files with a single command:
```bash
rm -rf ~/.claude/ccnotify
```

