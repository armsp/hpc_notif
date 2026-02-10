#!/usr/bin/env python3
"""
HPC Job Tray Monitor
====================
A Ubuntu system tray app that listens to ntfy.sh and shows colored
notifications when HPC jobs start, finish, or crash.

- Blue tray icon + notification when a job starts
- Green tray icon + notification when a job finishes
- Red tray icon + notification when a job fails/crashes

Usage:
    python3 hpc_tray.py --topic YOUR_SECRET_TOPIC

Dependencies (Ubuntu):
    sudo apt install gir1.2-appindicator3-0.1 gir1.2-notify-0.7 python3-gi python3-requests
"""

import argparse
from asyncio import subprocess
import json
import os
import sys
import threading
import time
import logging
from datetime import datetime

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("AppIndicator3", "0.1")
gi.require_version("Notify", "0.7")

from gi.repository import Gtk, GLib, AppIndicator3, Notify

try:
    import requests
except ImportError:
    print("Missing dependency: requests")
    print("Install with: pip install requests")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hpc-tray")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ICON_DIR = os.path.join(SCRIPT_DIR, "icons")

TRAY_ICONS = {
    "idle": os.path.join(ICON_DIR, "hpc-idle.svg"),
    "started": os.path.join(ICON_DIR, "hpc-started.svg"),
    "finished": os.path.join(ICON_DIR, "hpc-finished.svg"),
    "failed": os.path.join(ICON_DIR, "hpc-failed.svg"),
}

NOTIF_ICONS = {
    "started": os.path.join(ICON_DIR, "notif-started.svg"),
    "finished": os.path.join(ICON_DIR, "notif-finished.svg"),
    "failed": os.path.join(ICON_DIR, "notif-failed.svg"),
}

# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------
STARTED_KEYWORDS = ["started", "running", "launched", "queued", "beginning", "🚀"]
FINISHED_KEYWORDS = ["finished", "completed", "done", "success", "✅"]
FAILED_KEYWORDS = ["failed", "crashed", "error", "killed", "timeout", "oom", "❌", "abort"]


def classify_message(text: str) -> str:
    """Classify a notification message into started / finished / failed."""
    lower = text.lower()
    for kw in FAILED_KEYWORDS:
        if kw in lower:
            return "failed"
    for kw in FINISHED_KEYWORDS:
        if kw in lower:
            return "finished"
    for kw in STARTED_KEYWORDS:
        if kw in lower:
            return "started"
    return "started"  # default


def extract_job_id(text: str) -> str:
    """Try to extract a job ID from the message text."""
    lower = text.lower()
    # Look for patterns like "Job 12345" or "job_id=12345"
    import re

    patterns = [
        r"job\s+(\d+)",
        r"job_id[=:]\s*(\d+)",
        r"jobid[=:]\s*(\d+)",
        r"slurm[_\s]job[_\s]id[=:]\s*(\d+)",
        r"#(\d{4,})",  # any 4+ digit number preceded by #
    ]
    for pattern in patterns:
        match = re.search(pattern, lower)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Job history entry
# ---------------------------------------------------------------------------
MAX_HISTORY = 25


class JobEvent:
    def __init__(self, message: str, status: str, job_id: str = None):
        self.message = message
        self.status = status
        self.job_id = job_id
        self.timestamp = datetime.now()

    def menu_label(self) -> str:
        icons = {"started": "🔵", "finished": "🟢", "failed": "🔴"}
        icon = icons.get(self.status, "⚪")
        time_str = self.timestamp.strftime("%H:%M:%S")
        job_str = f"Job {self.job_id}" if self.job_id else "Job"
        # Truncate message for menu
        short_msg = self.message[:60] + ("..." if len(self.message) > 60 else "")
        return f"{icon} [{time_str}] {short_msg}"


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class HPCTrayApp:
    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.topic = topic
        self.server = server
        self.history: list[JobEvent] = []
        self.running = True

        # --- Verify icons exist ---
        for name, path in {**TRAY_ICONS, **NOTIF_ICONS}.items():
            if not os.path.isfile(path):
                log.error(f"Icon not found: {path}")
                sys.exit(1)

        # --- Initialize libnotify ---
        Notify.init("HPC Job Monitor")

        # --- Create AppIndicator ---
        self.indicator = AppIndicator3.Indicator.new(
            "hpc-job-monitor",
            TRAY_ICONS["idle"],
            AppIndicator3.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("HPC Job Monitor")

        # --- Build menu ---
        self.menu = Gtk.Menu()
        self._build_menu()
        self.indicator.set_menu(self.menu)

        # --- Start subscription thread ---
        self.thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        self.thread.start()

        log.info(f"HPC Tray Monitor started — listening on topic: {topic}")

    # ----- Menu -----

    def _build_menu(self):
        """Rebuild the dropdown menu."""
        # Remove old items
        for child in self.menu.get_children():
            self.menu.remove(child)

        # Header
        header = Gtk.MenuItem(label=f"📡 Topic: {self.topic}")
        header.set_sensitive(False)
        self.menu.append(header)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Job history
        if self.history:
            label_item = Gtk.MenuItem(label="Recent Events:")
            label_item.set_sensitive(False)
            self.menu.append(label_item)

            for event in reversed(self.history[-MAX_HISTORY:]):
                item = Gtk.MenuItem(label=event.menu_label())
                item.set_sensitive(False)
                self.menu.append(item)
        else:
            empty = Gtk.MenuItem(label="  No events yet — waiting for jobs...")
            empty.set_sensitive(False)
            self.menu.append(empty)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Clear history
        clear_item = Gtk.MenuItem(label="🗑  Clear History")
        clear_item.connect("activate", self._on_clear)
        self.menu.append(clear_item)

        # Reset icon
        reset_item = Gtk.MenuItem(label="⏹  Reset Icon")
        reset_item.connect("activate", self._on_reset_icon)
        self.menu.append(reset_item)

        self.menu.append(Gtk.SeparatorMenuItem())

        # Quit
        quit_item = Gtk.MenuItem(label="✕  Quit")
        quit_item.connect("activate", self._on_quit)
        self.menu.append(quit_item)

        self.menu.show_all()

    def _on_clear(self, _widget):
        self.history.clear()
        self._build_menu()

    def _on_reset_icon(self, _widget):
        self.indicator.set_icon_full(TRAY_ICONS["idle"], "Idle")

    def _on_quit(self, _widget):
        self.running = False
        Notify.uninit()
        Gtk.main_quit()

    # ----- Notification handling -----

    def _handle_message(self, data: dict):
        """Process an incoming ntfy message (called from background thread)."""
        message = data.get("message", "(no message)")
        title = data.get("title", "")

        status = classify_message(message)
        job_id = extract_job_id(message) or extract_job_id(title)

        event = JobEvent(message, status, job_id)

        # Schedule GUI update on the main GTK thread
        GLib.idle_add(self._update_ui, event)

    def _update_ui(self, event: JobEvent):
        """Update tray icon, show notification, add to history. Runs on GTK thread."""
        self.history.append(event)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

        # Update tray icon color
        icon_path = TRAY_ICONS.get(event.status, TRAY_ICONS["idle"])
        self.indicator.set_icon_full(icon_path, event.status)

        # Build notification title
        status_labels = {
            "started": "Job Started",
            "finished": "Job Finished",
            "failed": "Job Failed",
        }
        notif_title = status_labels.get(event.status, "HPC Update")
        if event.job_id:
            notif_title += f" — #{event.job_id}"

        # Choose urgency
        urgency_map = {
            "started": Notify.Urgency.NORMAL,
            "finished": Notify.Urgency.NORMAL,
            "failed": Notify.Urgency.CRITICAL,
        }

        # Show system notification with colored icon
        notif_icon = NOTIF_ICONS.get(event.status, NOTIF_ICONS["started"])
        notification = Notify.Notification.new(notif_title, event.message, notif_icon)
        notification.set_urgency(urgency_map.get(event.status, Notify.Urgency.NORMAL))

        # Keep failed notifications persistent until dismissed
        if event.status == "failed":
            notification.set_timeout(0)  # 0 = persistent
        else:
            notification.set_timeout(5000)  # 5 seconds

        try:
            notification.show()
            import subprocess
            subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"])
        except Exception as e:
            log.error(f"Failed to show notification: {e}")

        # Rebuild menu to include new event
        self._build_menu()

        log.info(f"[{event.status.upper()}] {event.message}")
        return False  # Remove from GLib idle queue

    # ----- ntfy subscription -----

    def _subscribe_loop(self):
        """Background thread: subscribe to ntfy JSON stream with auto-reconnect."""
        url = f"{self.server}/{self.topic}/json"

        while self.running:
            try:
                log.info(f"Connecting to {url} ...")
                with requests.get(url, stream=True, timeout=(10, None)) as resp:
                    resp.raise_for_status()
                    for line in resp.iter_lines(decode_unicode=True):
                        if not self.running:
                            return
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue

                        event_type = data.get("event")
                        if event_type in ("open", "keepalive"):
                            if event_type == "open":
                                log.info("Connected to ntfy stream.")
                            continue
                        if event_type != "message":
                            continue

                        self._handle_message(data)

            except requests.ConnectionError:
                log.warning("Connection lost. Reconnecting in 5s...")
                time.sleep(5)
            except requests.Timeout:
                log.warning("Timeout. Reconnecting in 5s...")
                time.sleep(5)
            except Exception as e:
                if self.running:
                    log.error(f"Error: {e}. Reconnecting in 10s...")
                    time.sleep(10)

    # ----- Run -----

    def run(self):
        """Start the GTK main loop."""
        try:
            Gtk.main()
        except KeyboardInterrupt:
            self._on_quit(None)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="HPC Job Tray Monitor — colored tray icon + desktop notifications via ntfy.sh",
    )
    parser.add_argument(
        "--topic",
        "-t",
        required=True,
        help="Your secret ntfy topic name (e.g. hpc-jobs-abc123xyz)",
    )
    parser.add_argument(
        "--server",
        "-s",
        default="https://ntfy.sh",
        help="ntfy server URL (default: https://ntfy.sh)",
    )
    args = parser.parse_args()

    app = HPCTrayApp(args.topic, args.server)
    app.run()


if __name__ == "__main__":
    main()
