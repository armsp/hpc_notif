#!/usr/bin/env python3
"""
HPC Job Tray Monitor — Windows Version
=======================================
A Windows system tray app that listens to ntfy.sh and shows color-coded
toast notifications when HPC jobs start, finish, or crash.

- Blue tray icon + toast when a job starts
- Green tray icon + toast when a job finishes
- Red tray icon + toast when a job fails/crashes

Usage:
    python hpc_tray_win.py --topic YOUR_SECRET_TOPIC

Dependencies:
    pip install pystray Pillow requests winotify
"""

import argparse
import json
import os
import re
import sys
import threading
import time
import logging
import tempfile
from datetime import datetime
from pathlib import Path

try:
    import requests
except ImportError:
    print("Missing: requests → pip install requests")
    sys.exit(1)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("Missing: Pillow → pip install Pillow")
    sys.exit(1)

try:
    import pystray
    from pystray import MenuItem, Menu
except ImportError:
    print("Missing: pystray → pip install pystray")
    sys.exit(1)

try:
    from winotify import Notification, audio
except ImportError:
    print("Missing: winotify → pip install winotify")
    sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("hpc-tray")

# ---------------------------------------------------------------------------
# Icon generation (creates colored .ico files with Pillow)
# ---------------------------------------------------------------------------
ICON_CACHE_DIR = Path(tempfile.gettempdir()) / "hpc-tray-icons"

COLORS = {
    "idle": "#888888",
    "started": "#2196F3",   # blue
    "finished": "#4CAF50",  # green
    "failed": "#F44336",    # red
}

# Larger notification icons
NOTIF_COLORS = {
    "started": ("#2196F3", "▶"),
    "finished": ("#4CAF50", "✓"),
    "failed": ("#F44336", "✕"),
}


def generate_tray_icon(status: str) -> Image.Image:
    """Generate a 64x64 tray icon: colored rounded square with 'H'."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color = COLORS.get(status, COLORS["idle"])

    # Rounded rectangle
    draw.rounded_rectangle([4, 4, 60, 60], radius=12, fill=color)

    # Letter "H" in center
    try:
        font = ImageFont.truetype("arial.ttf", 32)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "H", font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 2), "H", fill="white", font=font)

    return img


def generate_notif_icon(status: str) -> str:
    """Generate a notification icon and return its file path."""
    ICON_CACHE_DIR.mkdir(exist_ok=True)
    path = ICON_CACHE_DIR / f"notif-{status}.png"

    if path.exists():
        return str(path)

    size = 128
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    color, symbol = NOTIF_COLORS.get(status, ("#888888", "?"))
    draw.ellipse([8, 8, 120, 120], fill=color)

    try:
        font = ImageFont.truetype("arial.ttf", 56)
    except OSError:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), symbol, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) / 2, (size - th) / 2 - 4), symbol, fill="white", font=font)

    img.save(str(path), "PNG")
    return str(path)


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------
STARTED_KEYWORDS = ["started", "running", "launched", "queued", "beginning", "🚀"]
FINISHED_KEYWORDS = ["finished", "completed", "done", "success", "✅"]
FAILED_KEYWORDS = ["failed", "crashed", "error", "killed", "timeout", "oom", "❌", "abort"]


def classify_message(text: str) -> str:
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
    return "started"


def extract_job_id(text: str) -> str:
    patterns = [
        r"job\s+(\d+)",
        r"job_id[=:]\s*(\d+)",
        r"jobid[=:]\s*(\d+)",
        r"#(\d{4,})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


# ---------------------------------------------------------------------------
# Job history
# ---------------------------------------------------------------------------
MAX_HISTORY = 25


class JobEvent:
    def __init__(self, message: str, status: str, job_id: str = None):
        self.message = message
        self.status = status
        self.job_id = job_id
        self.timestamp = datetime.now()

    def menu_label(self) -> str:
        icons = {"started": "[STARTED]", "finished": "[DONE]", "failed": "[FAILED]"}
        icon = icons.get(self.status, "[?]")
        time_str = self.timestamp.strftime("%H:%M:%S")
        short = self.message[:50] + ("..." if len(self.message) > 50 else "")
        return f"{time_str} {icon} {short}"


# ---------------------------------------------------------------------------
# Main Application
# ---------------------------------------------------------------------------
class HPCTrayApp:
    def __init__(self, topic: str, server: str = "https://ntfy.sh"):
        self.topic = topic
        self.server = server
        self.history: list[JobEvent] = []
        self.running = True
        self.current_status = "idle"

        # Pre-generate icons
        log.info("Generating icons...")
        self.tray_icons = {s: generate_tray_icon(s) for s in COLORS}
        self.notif_icon_paths = {s: generate_notif_icon(s) for s in NOTIF_COLORS}

        # Create the tray icon
        self.tray = pystray.Icon(
            name="hpc-job-monitor",
            icon=self.tray_icons["idle"],
            title="HPC Job Monitor — Idle",
            menu=self._build_menu(),
        )

        log.info(f"HPC Tray Monitor started — listening on topic: {topic}")

    # ----- Menu -----

    def _build_menu(self) -> Menu:
        items = [
            MenuItem(f"Topic: {self.topic}", action=None, enabled=False),
            Menu.SEPARATOR,
        ]

        if self.history:
            for event in reversed(self.history[-10:]):
                label = event.menu_label()
                items.append(MenuItem(label, action=None, enabled=False))
        else:
            items.append(MenuItem("No events yet...", action=None, enabled=False))

        items.extend([
            Menu.SEPARATOR,
            MenuItem("Reset Icon", self._on_reset_icon),
            MenuItem("Clear History", self._on_clear),
            Menu.SEPARATOR,
            MenuItem("Quit", self._on_quit),
        ])

        return Menu(*items)

    def _refresh_menu(self):
        self.tray.menu = self._build_menu()
        self.tray.update_menu()

    def _on_reset_icon(self, icon, item):
        self.current_status = "idle"
        self.tray.icon = self.tray_icons["idle"]
        self.tray.title = "HPC Job Monitor — Idle"

    def _on_clear(self, icon, item):
        self.history.clear()
        self._refresh_menu()

    def _on_quit(self, icon, item):
        self.running = False
        self.tray.stop()

    # ----- Notification handling -----

    def _handle_message(self, data: dict):
        message = data.get("message", "(no message)")
        title = data.get("title", "")

        status = classify_message(message)
        job_id = extract_job_id(message) or extract_job_id(title)

        event = JobEvent(message, status, job_id)
        self.history.append(event)
        if len(self.history) > MAX_HISTORY:
            self.history = self.history[-MAX_HISTORY:]

        # Update tray icon
        self.current_status = status
        self.tray.icon = self.tray_icons.get(status, self.tray_icons["idle"])

        status_labels = {"started": "Running", "finished": "Finished", "failed": "FAILED"}
        self.tray.title = f"HPC Job Monitor — {status_labels.get(status, 'Active')}"

        # Build toast title
        toast_titles = {
            "started": "Job Started",
            "finished": "Job Finished",
            "failed": "Job Failed!",
        }
        toast_title = toast_titles.get(status, "HPC Update")
        if job_id:
            toast_title += f" — #{job_id}"

        # Show Windows toast notification
        try:
            notif = Notification(
                app_id="HPC Job Monitor",
                title=toast_title,
                msg=message,
                icon=self.notif_icon_paths.get(status, ""),
                duration="long" if status == "failed" else "short",
            )

            # Add sound for failures
            if status == "failed":
                notif.set_audio(audio.Reminder, loop=False)

            notif.show()
        except Exception as e:
            log.error(f"Toast notification failed: {e}")

        self._refresh_menu()
        log.info(f"[{status.upper()}] {message}")

    # ----- ntfy subscription -----

    def _subscribe_loop(self):
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
        # Start ntfy subscription in background thread
        sub_thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        sub_thread.start()

        # Run the tray icon on the main thread (required by pystray on Windows)
        self.tray.run()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="HPC Job Tray Monitor (Windows) — colored tray + toast notifications via ntfy.sh",
    )
    parser.add_argument(
        "--topic", "-t", required=True,
        help="Your secret ntfy topic name",
    )
    parser.add_argument(
        "--server", "-s", default="https://ntfy.sh",
        help="ntfy server URL (default: https://ntfy.sh)",
    )
    args = parser.parse_args()

    app = HPCTrayApp(args.topic, args.server)
    app.run()


if __name__ == "__main__":
    main()
