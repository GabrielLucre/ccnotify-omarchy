#!/usr/bin/env python3
"""
Claude Code Notify
https://github.com/dazuiba/CCNotify
"""

import os
import sys
import json
import sqlite3
import subprocess
import logging
from logging.handlers import TimedRotatingFileHandler
from datetime import datetime
import re


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(SCRIPT_DIR, "config.json")

DEFAULT_CONFIG = {
    "title": "project",  # project | claude | both
    "fields": ["job", "duration", "date"],
    "date_format": "long",  # long | short | time | iso | any strftime format
    "summary_mode": "excerpt",  # excerpt (free) | ai (uses Claude Code usage)
    "context_window": 1_000_000,  # 0 = auto-detect from the transcript
    "urgency_keywords": ["error", "fail"],
    "max_lines": 3,  # 0 = no limit; packs the body into N lines otherwise
}
VALID_FIELDS = ["job", "duration", "tokens", "context", "summary", "date"]
VALID_CHOICES = {
    "title": ["project", "claude", "both"],
    "summary_mode": ["excerpt", "ai"],
}
# Fields in the same group share one line of the notification body
FIELD_GROUPS = {
    "job": "status",
    "duration": "status",
    "tokens": "usage",
    "context": "usage",
    "summary": "summary",
    "date": "date",
}
DATE_FORMATS = {
    "long": "%B %d, %Y at %H:%M",
    "short": "%m/%d/%Y %H:%M",
    "time": "%H:%M",
    "iso": "%Y-%m-%d %H:%M",
}


def load_config():
    """Load config.json next to the script, falling back to defaults"""
    config = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config.update(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        logging.warning(f"Invalid config {CONFIG_PATH}, using defaults: {e}")
    return config


def save_config(config):
    """Write config.json, keeping only known keys"""
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(
            {k: config[k] for k in DEFAULT_CONFIG}, f, indent=2, ensure_ascii=False
        )
        f.write("\n")


def set_config_value(config, key, value):
    """Validate and set one config value; return an error message or None"""
    if key not in DEFAULT_CONFIG:
        return f"Unknown key '{key}'. Valid keys: {', '.join(DEFAULT_CONFIG)}"

    if key in ("fields", "urgency_keywords"):
        items = [v.strip() for v in value.split(",") if v.strip()]
        if key == "fields":
            invalid = [f for f in items if f not in VALID_FIELDS]
            if invalid:
                return f"Unknown fields {invalid}. Valid fields: {', '.join(VALID_FIELDS)}"
        config[key] = items
    elif key in ("context_window", "max_lines"):
        try:
            config[key] = int(value)
        except ValueError:
            return f"{key} must be a number (0 = {'auto' if key == 'context_window' else 'no limit'})"
    else:
        if key in VALID_CHOICES and value not in VALID_CHOICES[key]:
            return f"Invalid {key} '{value}'. Choose one of: {', '.join(VALID_CHOICES[key])}"
        config[key] = value
    return None


def run_config_command(args):
    """ccnotify.py config [set KEY VALUE | fields a,b,c | reset]"""
    config = load_config()

    if not args:
        print(f"# {CONFIG_PATH}")
        print(json.dumps(config, indent=2, ensure_ascii=False))
        return 0

    if args[0] == "reset":
        try:
            os.remove(CONFIG_PATH)
        except FileNotFoundError:
            pass
        print("Config reset to defaults")
        return 0

    if args[0] == "fields" and len(args) == 2:
        args = ["set", "fields", args[1]]

    if args[0] == "set" and len(args) == 3:
        key, value = args[1], args[2]
        error = set_config_value(config, key, value)
        if error:
            print(error, file=sys.stderr)
            return 1
        save_config(config)
        print(f"{key} = {json.dumps(config[key], ensure_ascii=False)}")
        return 0

    print(
        "Usage: ccnotify.py config [set KEY VALUE | fields a,b,c | reset]",
        file=sys.stderr,
    )
    return 1


def format_title(config, cwd):
    """Notification title: project folder, 'Claude Code', or both"""
    project = os.path.basename(cwd) if cwd else ""
    if config["title"] == "claude" or not project:
        return "Claude Code"
    if config["title"] == "both":
        return f"Claude Code · {project}"
    return project


def format_date(date_format):
    """Format the current time using a preset name or a strftime pattern"""
    return datetime.now().strftime(DATE_FORMATS.get(date_format, date_format))


def format_tokens(count):
    """Compact token count: 950, 12.4k, 1.2M"""
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count / 1000:.1f}".rstrip("0").rstrip(".") + "k"
    return f"{count / 1_000_000:.1f}".rstrip("0").rstrip(".") + "M"


def first_sentence(text, limit=80):
    """First sentence of the first non-empty line, without markdown, truncated"""
    line = ""
    for raw in text.splitlines():
        line = raw.strip().lstrip("#>-* ").replace("**", "").replace("`", "").strip()
        if line:
            break
    sentence = re.split(r"(?<=[.!?])\s", line, maxsplit=1)[0]
    if len(sentence) > limit:
        sentence = sentence[: limit - 1].rstrip() + "…"
    return sentence


def build_body(values, fields, max_lines=0):
    """Join the enabled fields into lines, grouping related fields on one line"""
    # Fields keep the configured order, except the date, which always goes last
    fields = [f for f in fields if f != "date"] + (["date"] if "date" in fields else [])

    groups = {}
    for field in fields:
        if values.get(field):
            groups.setdefault(FIELD_GROUPS[field], []).append(values[field])
    lines = [" · ".join(parts) for parts in groups.values()]

    # Too many lines for the notification daemon: keep the first lines and the
    # last one (the date), and pack the ones in between onto a single line
    if max_lines and len(lines) > max_lines:
        if max_lines >= 3:
            keep = max_lines - 2
            lines = lines[:keep] + [" · ".join(lines[keep:-1])] + lines[-1:]
        elif max_lines == 2:
            lines = [" · ".join(lines[:-1]), lines[-1]]
        else:
            lines = [" · ".join(lines)]
    return "\n".join(lines)


class ClaudePromptTracker:
    def __init__(self):
        """Initialize the prompt tracker with database setup"""
        script_dir = os.path.dirname(os.path.abspath(__file__))
        self.db_path = os.path.join(script_dir, "ccnotify.db")
        self.setup_logging()
        self.config = load_config()
        self.init_database()

    def setup_logging(self):
        """Setup logging to file with daily rotation"""

        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_path = os.path.join(script_dir, "ccnotify.log")

        # Create a timed rotating file handler
        handler = TimedRotatingFileHandler(
            log_path,
            when="midnight",  # Rotate at midnight
            interval=1,  # Every 1 day
            backupCount=1,  # Keep 1 days of logs
            encoding="utf-8",
        )

        # Set the log format
        formatter = logging.Formatter(
            "%(asctime)s - %(levelname)s - %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
        )
        handler.setFormatter(formatter)

        # Configure the root logger
        logger = logging.getLogger()
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)

    def init_database(self):
        """Create tables and triggers if they don't exist"""
        with sqlite3.connect(self.db_path) as conn:
            # Create main table
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    prompt TEXT,
                    cwd TEXT,
                    seq INTEGER,
                    stoped_at DATETIME,
                    lastWaitUserAt DATETIME
                )
            """)

            # Create trigger for auto-incrementing seq
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS auto_increment_seq
                AFTER INSERT ON prompt
                FOR EACH ROW
                BEGIN
                    UPDATE prompt
                    SET seq = (
                        SELECT COALESCE(MAX(seq), 0) + 1
                        FROM prompt
                        WHERE session_id = NEW.session_id
                    )
                    WHERE id = NEW.id;
                END
            """)

            conn.commit()

    def handle_user_prompt_submit(self, data):
        """Handle UserPromptSubmit event - insert new prompt record"""
        session_id = data.get("session_id")
        prompt = data.get("prompt", "")
        cwd = data.get("cwd", "")

        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                INSERT INTO prompt (session_id, prompt, cwd)
                VALUES (?, ?, ?)
            """,
                (session_id, prompt, cwd),
            )
            conn.commit()

        logging.info(f"Recorded prompt for session {session_id}")

    def handle_stop(self, data):
        """Handle Stop event - update completion time and send notification"""
        session_id = data.get("session_id")

        with sqlite3.connect(self.db_path) as conn:
            # Find the latest unfinished record for this session
            cursor = conn.execute(
                """
                SELECT id, created_at, cwd
                FROM prompt
                WHERE session_id = ? AND stoped_at IS NULL
                ORDER BY created_at DESC
                LIMIT 1
            """,
                (session_id,),
            )

            row = cursor.fetchone()
            if row:
                record_id, created_at, cwd = row

                # Update completion time
                conn.execute(
                    """
                    UPDATE prompt
                    SET stoped_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """,
                    (record_id,),
                )
                conn.commit()

                # Get seq number and calculate duration
                cursor = conn.execute(
                    "SELECT seq FROM prompt WHERE id = ?", (record_id,)
                )
                seq_row = cursor.fetchone()
                seq = seq_row[0] if seq_row else 1

                duration = self.calculate_duration_from_db(record_id)
                self.notify_task_done(data, cwd, seq, duration)

                logging.info(
                    f"Task completed for session {session_id}, job#{seq}, duration: {duration}"
                )

    def handle_notification(self, data):
        """Handle Notification event - check for various notification types and send notifications"""
        session_id = data.get("session_id")
        message = data.get("message", "")
        cwd = data.get("cwd", "")

        # Log all notifications for debugging
        logging.info(f"[NOTIFICATION] session={session_id}, message='{message}'")

        # Determine notification type and subtitle
        message_lower = message.lower()
        subtitle = None
        should_update_db = False
        should_notify = True

        if (
            "waiting for your input" in message_lower
            or "waiting for input" in message_lower
        ):
            subtitle = "Waiting for input"
            should_update_db = True
            should_notify = (
                False  # Suppress notification - Stop handler will send "job done"
            )
        elif "permission" in message_lower:
            subtitle = "Permission Required"
        elif "approval" in message_lower or "choose an option" in message_lower:
            subtitle = "Action Required"
        else:
            # For other notifications, use a generic subtitle
            subtitle = "Notification"

        # Update database for waiting notifications
        if should_update_db:
            with sqlite3.connect(self.db_path) as conn:
                # Fix: Use subquery instead of ORDER BY/LIMIT in UPDATE
                conn.execute(
                    """
                    UPDATE prompt
                    SET lastWaitUserAt = CURRENT_TIMESTAMP
                    WHERE id = (
                        SELECT id FROM prompt
                        WHERE session_id = ?
                        ORDER BY created_at DESC
                        LIMIT 1
                    )
                """,
                    (session_id,),
                )
                conn.commit()
            logging.info(f"Updated lastWaitUserAt for session {session_id}")

        # Send notification only if should_notify is True
        if should_notify:
            lines = [subtitle]
            if "date" in self.config["fields"]:
                lines.append(format_date(self.config["date_format"]))
            self.send_notification(
                title=format_title(self.config, cwd),
                body="\n".join(lines),
                window=self.find_terminal_window(),
                status=subtitle,
            )
            logging.info(f"Notification sent for session {session_id}: {subtitle}")
        else:
            logging.info(
                f"Notification suppressed for session {session_id}: {subtitle}"
            )

    def calculate_duration_from_db(self, record_id):
        """Calculate duration for a completed record"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                SELECT created_at, stoped_at
                FROM prompt
                WHERE id = ?
            """,
                (record_id,),
            )

            row = cursor.fetchone()
            if row and row[1]:
                return self.calculate_duration(row[0], row[1])

        return "Unknown"

    def calculate_duration(self, start_time, end_time):
        """Calculate human-readable duration between two timestamps"""
        try:
            if isinstance(start_time, str):
                start_dt = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
            else:
                start_dt = datetime.fromisoformat(start_time)

            if isinstance(end_time, str):
                end_dt = datetime.fromisoformat(end_time.replace("Z", "+00:00"))
            else:
                end_dt = datetime.fromisoformat(end_time)

            duration = end_dt - start_dt
            total_seconds = int(duration.total_seconds())

            if total_seconds < 60:
                return f"{total_seconds}s"
            elif total_seconds < 3600:
                minutes = total_seconds // 60
                seconds = total_seconds % 60
                if seconds > 0:
                    return f"{minutes}m{seconds}s"
                else:
                    return f"{minutes}m"
            else:
                hours = total_seconds // 3600
                minutes = (total_seconds % 3600) // 60
                if minutes > 0:
                    return f"{hours}h{minutes}m"
                else:
                    return f"{hours}h"
        except Exception as e:
            logging.error(f"Error calculating duration: {e}")
            return "Unknown"

    def parent_pid(self, pid):
        """Read the parent PID of a process from /proc"""
        try:
            with open(f"/proc/{pid}/stat") as f:
                stat = f.read()
            # comm (field 2) may contain spaces/parens, so split after the last ')'
            return int(stat[stat.rfind(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            return 0

    def find_terminal_window(self):
        """Return the Hyprland address of the window that owns this Claude session"""
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return None

        try:
            result = subprocess.run(
                ["hyprctl", "clients", "-j"], capture_output=True, text=True, timeout=2
            )
            windows = {c["pid"]: c["address"] for c in json.loads(result.stdout)}
        except Exception as e:
            logging.warning(f"Could not list Hyprland windows: {e}")
            return None

        # Walk up the process tree: hook -> claude -> shell -> terminal window
        pid = os.getppid()
        while pid > 1:
            if pid in windows:
                return windows[pid]
            pid = self.parent_pid(pid)

        return None

    def is_user_prompt(self, entry):
        """True for a transcript entry holding a typed prompt (not a tool result)"""
        if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
            return False
        content = entry.get("message", {}).get("content")
        if isinstance(content, str):
            return True
        if isinstance(content, list):
            types = {block.get("type") for block in content if isinstance(block, dict)}
            return "text" in types and "tool_result" not in types
        return False

    def read_transcript_stats(self, transcript_path):
        """Token usage and last reply of the current turn, read from the local transcript"""
        stats = {}
        if not transcript_path:
            return stats

        try:
            entries = []
            with open(os.path.expanduser(transcript_path), encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue

            # The current turn starts at the last prompt the user typed
            start = 0
            for i, entry in enumerate(entries):
                if self.is_user_prompt(entry):
                    start = i

            # One API response is split across several entries sharing an id
            usages = {}
            last_usage = None
            model = ""
            last_text = ""
            for entry in entries[start:]:
                if entry.get("type") != "assistant" or entry.get("isSidechain"):
                    continue
                message = entry.get("message", {})
                usage = message.get("usage")
                if usage:
                    usages[message.get("id") or len(usages)] = usage
                    last_usage = usage
                    model = message.get("model", model)
                content = message.get("content")
                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text" and block.get("text", "").strip():
                            last_text = block["text"]

            if usages:
                stats["tokens"] = sum(
                    u.get("input_tokens", 0)
                    + u.get("cache_creation_input_tokens", 0)
                    + u.get("output_tokens", 0)
                    for u in usages.values()
                )
            if last_usage:
                stats["context"] = sum(
                    last_usage.get(k, 0)
                    for k in (
                        "input_tokens",
                        "cache_read_input_tokens",
                        "cache_creation_input_tokens",
                        "output_tokens",
                    )
                )
                stats["model"] = model
            if last_text:
                stats["text"] = last_text
        except Exception as e:
            logging.warning(f"Could not read transcript {transcript_path}: {e}")

        return stats

    def context_window(self, stats):
        """Context window size: configured, or guessed from model and usage"""
        if self.config["context_window"]:
            return self.config["context_window"]
        if "[1m]" in stats.get("model", "") or stats.get("context", 0) > 200_000:
            return 1_000_000
        return 200_000

    def notify_task_done(self, data, cwd, seq, duration):
        """Build the task-completed notification from the configured fields"""
        fields = self.config["fields"]
        values = {
            "job": f"job#{seq} done",
            "duration": duration,
            "date": format_date(self.config["date_format"]),
        }

        stats = {}
        if any(f in fields for f in ("tokens", "context", "summary")):
            stats = self.read_transcript_stats(data.get("transcript_path"))
        if "tokens" in stats:
            values["tokens"] = f"{format_tokens(stats['tokens'])} tokens"
        if "context" in stats:
            window = self.context_window(stats)
            percent = round(stats["context"] * 100 / window)
            values["context"] = (
                f"context {percent}% "
                f"({format_tokens(stats['context'])}/{format_tokens(window)})"
            )

        text = data.get("last_assistant_message")
        if not isinstance(text, str) or not text.strip():
            text = stats.get("text", "")

        title = format_title(self.config, cwd)
        window = self.find_terminal_window()

        if "summary" in fields and text:
            if self.config["summary_mode"] == "ai":
                self.spawn_ai_summary(title, values, text, window)
                return
            values["summary"] = first_sentence(text)

        self.send_notification(
            title, build_body(values, fields, self.config["max_lines"]), window=window, status=values["job"]
        )

    def spawn_ai_summary(self, title, values, text, window):
        """Summarize the reply with Claude Haiku in the background, then notify"""
        payload = json.dumps(
            {"title": title, "values": values, "text": text[:4000], "window": window}
        )
        # CCNOTIFY_SUMMARY keeps the hooks of the nested `claude -p` run from
        # calling ccnotify again
        env = dict(os.environ, CCNOTIFY_SUMMARY="1")
        proc = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "_summary"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            text=True,
            start_new_session=True,
        )
        proc.stdin.write(payload)
        proc.stdin.close()
        logging.info("AI summary started in background")

    def run_ai_summary(self, payload):
        """Background worker: ask Claude Haiku for a one-line summary and notify"""
        values = payload["values"]
        text = payload["text"]
        prompt = (
            "Below is the final message an AI coding assistant wrote after "
            "finishing a task. Do not reply to it. Write a summary of what was "
            "done in at most 12 words. Write it in the same language the "
            "message is written in (a Portuguese message gets a Portuguese "
            "summary, and so on). Output only the summary.\n\n<message>\n"
            + text
            + "\n</message>"
        )

        summary = ""
        try:
            result = subprocess.run(
                [
                    "claude",
                    "-p",
                    "--model",
                    "haiku",
                    "--settings",
                    '{"disableAllHooks": true}',
                ],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode == 0:
                summary = result.stdout.strip()
            else:
                logging.warning(f"AI summary failed: {result.stderr.strip()}")
        except Exception as e:
            logging.warning(f"AI summary failed: {e}")

        # Fall back to the free excerpt if Claude could not summarize
        values["summary"] = first_sentence(summary or text, limit=100)
        self.send_notification(
            payload["title"],
            build_body(values, self.config["fields"], self.config["max_lines"]),
            window=payload.get("window"),
            status=values.get("job", ""),
        )

    def preview(self):
        """Send a sample task notification using the current config"""
        values = {
            "job": "job#1 done",
            "duration": "1m16s",
            "tokens": "12.4k tokens",
            "context": "context 7% (70k/1M)",
            "summary": "Ported the notifications to notify-send",
            "date": format_date(self.config["date_format"]),
        }
        title = format_title(self.config, os.getcwd())
        body = build_body(values, self.config["fields"], self.config["max_lines"])
        print(f"{title}\n{body}")
        self.send_notification(title, body, window=self.find_terminal_window())

    def send_notification(self, title, body, window=None, status=None):
        """Send desktop notification using notify-send (libnotify)"""
        # Urgency comes from the status line only, so a summary mentioning
        # "fail" does not turn a finished task into a critical alert
        status_lower = (body if status is None else status).lower()
        urgency = (
            "critical"
            if any(k.lower() in status_lower for k in self.config["urgency_keywords"])
            else "normal"
        )

        cmd = [
            "notify-send",
            "-a",
            "Claude Code",
            "-u",
            urgency,
            "-i",
            "utilities-terminal",
            title,
            body,
        ]

        try:
            if window:
                # Clicking the notification focuses the terminal running Claude.
                # notify-send blocks until the notification closes, so run it
                # detached to let the hook return immediately.
                cmd.extend(["-A", "default=Focus terminal"])
                script = (
                    'addr="$1"; shift; action=$("$@"); '
                    '[ "$action" = default ] && '
                    'hyprctl dispatch focuswindow "address:$addr" >/dev/null'
                )
                subprocess.Popen(
                    ["sh", "-c", script, "sh", window, *cmd],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            else:
                subprocess.run(cmd, check=False, capture_output=True)
            logging.info(f"Notification sent: {title} - {body!r}")
        except FileNotFoundError:
            logging.warning("notify-send not found (install libnotify), notification skipped")
        except Exception as e:
            logging.error(f"Error sending notification: {e}")


def validate_input_data(data, expected_event_name):
    """Validate input data matches design specification"""
    required_fields = {
        "UserPromptSubmit": ["session_id", "prompt", "cwd", "hook_event_name"],
        "Stop": ["session_id", "hook_event_name"],
        "Notification": ["session_id", "message", "hook_event_name"],
    }

    if expected_event_name not in required_fields:
        raise ValueError(f"Unknown event type: {expected_event_name}")

    # Check hook_event_name matches expected
    if data.get("hook_event_name") != expected_event_name:
        raise ValueError(
            f"Event name mismatch: expected {expected_event_name}, got {data.get('hook_event_name')}"
        )

    # Check required fields
    missing_fields = []
    for field in required_fields[expected_event_name]:
        if field not in data or data[field] is None:
            missing_fields.append(field)

    if missing_fields:
        raise ValueError(
            f"Missing required fields for {expected_event_name}: {missing_fields}"
        )

    return True


def main():
    """Main entry point - read JSON from stdin and process event"""
    try:
        # Check if hook type is provided as command line argument
        if len(sys.argv) < 2:
            print("ok")
            return

        expected_event_name = sys.argv[1]
        valid_events = ["UserPromptSubmit", "Stop", "Notification"]

        if expected_event_name == "config":
            sys.exit(run_config_command(sys.argv[2:]))

        if expected_event_name == "preview":
            ClaudePromptTracker().preview()
            return

        if expected_event_name == "_summary":
            ClaudePromptTracker().run_ai_summary(json.loads(sys.stdin.read()))
            return

        # Manual test: ccnotify.py "Title" "Message"
        if expected_event_name not in valid_events and len(sys.argv) >= 3:
            tracker = ClaudePromptTracker()
            tracker.send_notification(
                sys.argv[1], sys.argv[2], window=tracker.find_terminal_window()
            )
            return

        # Hooks fired by the `claude -p` run behind the AI summary
        if os.environ.get("CCNOTIFY_SUMMARY"):
            return

        if expected_event_name not in valid_events:
            logging.error(f"Invalid hook type: {expected_event_name}")
            logging.error(f"Valid hook types: {', '.join(valid_events)}")
            sys.exit(1)

        # Read JSON data from stdin
        input_data = sys.stdin.read().strip()
        if not input_data:
            logging.warning("No input data received")
            return

        data = json.loads(input_data)

        # Validate input data
        validate_input_data(data, expected_event_name)

        tracker = ClaudePromptTracker()

        if expected_event_name == "UserPromptSubmit":
            tracker.handle_user_prompt_submit(data)
        elif expected_event_name == "Stop":
            tracker.handle_stop(data)
        elif expected_event_name == "Notification":
            tracker.handle_notification(data)

    except json.JSONDecodeError as e:
        logging.error(f"JSON decode error: {e}")
        sys.exit(1)
    except ValueError as e:
        logging.error(f"Validation error: {e}")
        sys.exit(1)
    except Exception as e:
        logging.error(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
