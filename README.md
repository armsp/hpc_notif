# HPC Job Tray Monitor

A Ubuntu system tray app that gives you instant, color-coded desktop notifications when your HPC cluster jobs start, finish, or crash.

```
 HPC Cluster                     ntfy.sh                    Your Ubuntu Desktop
┌──────────────┐    curl POST   ┌──────────────┐   SSE     ┌─────────────────────┐
│ SLURM Job    │ ─────────────► │  Free relay   │ ───────►  │ Tray icon changes:  │
│              │                │  (pub/sub)    │           │  🔵 Job started     │
│ hpc_notify() │                └──────────────┘           │  🟢 Job finished    │
└──────────────┘                                           │  🔴 Job failed      │
                                                           │ + system notification│
                                                           └─────────────────────┘
```

## Features

- **Color-coded tray icon** — blue (running), green (finished), red (failed), grey (idle)
- **Native system notifications** with colored icons: ▶️ blue play for started, ✅ green check for finished, ❌ red cross for failed
- **Job history dropdown** — click the tray icon to see recent events with timestamps
- **Auto-reconnect** — handles network drops gracefully
- **Failed job alerts persist** until you dismiss them; others auto-hide after 5s
- **Job ID extraction** — automatically pulls job IDs from messages for cleaner display

## Quick Setup

### 1. Install dependencies (Ubuntu)

```bash
# These are likely already installed on a stock Ubuntu desktop
sudo apt install gir1.2-appindicator3-0.1 gir1.2-notify-0.7 python3-gi python3-requests
```

If `python3-requests` isn't available via apt, use pip:
```bash
pip install requests
```

### 2. Pick a secret topic name

```bash
echo "hpc-jobs-$(head -c 12 /dev/urandom | base64 | tr -dc 'a-zA-Z0-9' | head -c 12)"
```

### 3. Start the tray app

```bash
python3 hpc_tray.py --topic hpc-jobs-YOUR-SECRET-TOPIC
```

A grey "H" icon should appear in your top bar.

### 4. Test it

From any terminal:
```bash
# Blue notification (started)
curl -d "🚀 Started: Job 12345 — python train.py" ntfy.sh/hpc-jobs-YOUR-SECRET-TOPIC

# Green notification (finished)
curl -d "✅ Finished: Job 12345 (took 2h 15m 3s)" ntfy.sh/hpc-jobs-YOUR-SECRET-TOPIC

# Red notification (failed)
curl -d "❌ Failed (exit 1): Job 12345 — OOM killed" ntfy.sh/hpc-jobs-YOUR-SECRET-TOPIC
```

### 5. Set up your HPC jobs

Copy `hpc_notify.sh` to your cluster home directory and edit the topic name. Then in job scripts:

```bash
#!/bin/bash

source ~/hpc_notify.sh
hpc_run python train.py --epochs 100
```

`hpc_run` automatically sends start/finish/fail notifications with timing.

Or send manual notifications at specific checkpoints:

```bash
source ~/hpc_notify.sh

hpc_notify "🚀 Started: preprocessing data"
python preprocess.py

hpc_notify "🚀 Started: training model"
python train.py

hpc_notify "✅ Finished: all steps complete, check /scratch/results/"
```

## Auto-start on Login

**Option A: Autostart entry (simplest)**
```bash
# Edit the .desktop file — replace USER and YOUR_TOPIC_HERE
nano hpc-job-monitor.desktop

# Copy to autostart
cp hpc-job-monitor.desktop ~/.config/autostart/
```

**Option B: Systemd user service**
```bash
# Create a service file
mkdir -p ~/.config/systemd/user/
cat > ~/.config/systemd/user/hpc-tray.service << 'EOF'
[Unit]
Description=HPC Job Tray Monitor
After=graphical-session.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 %h/hpc-tray/hpc_tray.py --topic YOUR_TOPIC_HERE
Restart=on-failure
RestartSec=10
Environment=DISPLAY=:0
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/%U/bus

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now hpc-tray.service
```

## Troubleshooting

**Tray icon not showing?**
On GNOME 42+, you may need the AppIndicator extension:
```bash
sudo apt install gnome-shell-extension-appindicator
# Then log out and back in, or restart GNOME Shell (Alt+F2 → r → Enter)
```

**Notifications not appearing?**
Test that libnotify works: `notify-send "test" "hello"`

**Missing dependencies?**
```bash
python3 -c "import gi; gi.require_version('AppIndicator3', '0.1'); from gi.repository import AppIndicator3; print('OK')"
```

**Want sound alerts?**
Add to the `_update_ui` method after `notification.show()`:
```python
import subprocess
subprocess.Popen(["paplay", "/usr/share/sounds/freedesktop/stereo/message-new-instant.oga"])
```
