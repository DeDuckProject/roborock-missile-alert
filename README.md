# MAMAD Roborock — Missile Alert Auto-Cleaner

Automatically triggers your Roborock vacuum whenever Pikud HaOref (Home Front Command) issues a missile/rocket alert for your area. The vacuum cleans a different room on each alert (round-robin), stops after a configurable duration, and returns to dock.

Rooms named **Mamad / ממד / ממ״ד** are permanently excluded from the cycle — you need that room free during an alert.

---

## How it works

1. Polls `oref.org.il` every 5 seconds for active alerts.
2. On a matching alert → picks the next room in round-robin order → starts segment cleaning.
3. After `clean_duration_minutes` (default: 10) → stops and returns to dock.
4. State (room index, last-cleaned timestamps, credentials) is persisted in `mamad_state.json`.

---

## Quick Start with Docker

No Python environment needed — just Docker.

### With Docker Compose

```bash
# 1. Create data dir and copy config template
mkdir data
cp config.yaml data/config.yaml   # then edit data/config.yaml

# 2. One-time interactive setup (Roborock login + area selection)
docker compose run --rm mamad --setup

# 3. Run daemon
docker compose up -d

# 4. View logs
docker compose logs -f
```

The `data/` directory holds `config.yaml` (user-provided), `mamad_state.json` (auto-created), and `mamad.log` — all persisted across container restarts.

### Without Compose

```bash
docker build -t mamad-roborock .
docker run -it --rm -v ./data:/data mamad-roborock --setup
docker run -d --restart=unless-stopped -v ./data:/data mamad-roborock
```

---

## Requirements

- Python 3.11+
- A Roborock account (the same one used in the Roborock app)
- A Roborock vacuum that has completed at least one mapping run

---

## Quickstart

### 1. Clone the repo

```bash
git clone https://github.com/your-username/roborock-missile-alert.git
cd roborock-missile-alert
```

### 2. Create a virtual environment and install dependencies

```bash
python3 -m venv venv
source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Run first-time setup

```bash
venv/bin/python mamad_roborock.py --setup
```

This will interactively:
- Ask for your Roborock account email
- Send a verification code to that email and ask you to enter it
- Ask which Hebrew city/area names you want to monitor for alerts
- Discover your rooms and print them
- Save everything to `mamad_state.json`

Example:
```
Enter your Roborock account email: you@example.com
Enter the verification code sent to you@example.com: 123456

--- Alert Areas ---
Enter the Hebrew city/area names to watch for alerts.
Substring matching is used — 'תל אביב' matches 'תל אביב - מרכז' too.
Separate multiple areas with commas.

Areas: תל אביב, חיפה

Discovered 8 rooms:
  id=   16  name=Kitchen
  id=   17  name=Living room
  id=   21  name=Mamad        ← automatically excluded from cleaning cycle
  ...

Setup complete. You can now start the daemon:
  python mamad_roborock.py
```

You can re-run `--setup` at any time to change your email or monitored areas.

### 4. (Optional) Tweak settings

Open `config.yaml` to adjust things like poll interval, fan speed, clean duration, notifications, and more. All settings have sensible defaults — you don't need to change anything to get started.

See [Configuration](#configuration) below for the full reference.

### 5. Start the daemon

```bash
venv/bin/python mamad_roborock.py
```

Watch the logs in a second terminal:

```bash
tail -f mamad.log
```

Stop with `Ctrl+C` — if the vacuum is cleaning it will be stopped and docked cleanly.

---

## Testing

### Test vacuum control

Clean one room for 30 seconds then dock (replace `16` with any room id from your setup output):

```bash
venv/bin/python mamad_roborock.py --test-clean 16
```

### Test alert detection

Poll the alert API once and check whether your configured areas would match:

```bash
venv/bin/python mamad_roborock.py --test-alert
```

---

## Run as a systemd service (Linux / Raspberry Pi)

```bash
# Copy and edit the unit file
sudo cp mamad-roborock.service /etc/systemd/system/
sudo nano /etc/systemd/system/mamad-roborock.service
# Update User= and WorkingDirectory= / ExecStart= paths to match your setup

sudo systemctl daemon-reload
sudo systemctl enable mamad-roborock
sudo systemctl start mamad-roborock

# Check status
sudo systemctl status mamad-roborock
sudo journalctl -u mamad-roborock -f
```

---

## Configuration

All options live in `config.yaml` (excluded from git):

| Key | Default | Description |
|-----|---------|-------------|
| `areas` | *(required)* | List of Hebrew area/city name substrings to match against alerts |
| `poll_seconds` | `5` | How often to poll the alert API |
| `alert_types` | `["1"]` | Alert categories to react to (`"1"` = missiles/rockets) |
| `clean_duration_minutes` | `10` | How long to clean per alert |
| `fan_speed` | `balanced` | Fan speed: `quiet`, `balanced`, `turbo`, `max`, `max_plus` |
| `cleaning_profile` | `auto` | Cleaning behavior: `auto`, `vacuum_only`, `mop_only`, `vacuum_and_mop`, `mop_after_vacuum` |
| `exclude_rooms` | `[]` | Room name substrings to exclude from rotation (in addition to Mamad) |
| `cooldown_hours` | `1` | Minimum hours between cleans of the same room |
| `room_selection_strategy` | `round_robin` | Room selection mode: `round_robin` or `oldest_cleaned` |
| `min_battery_percent` | `20` | Skip cleaning if battery is below this level |
| `notifications.enabled` | `false` | Enable Telegram or ntfy notifications |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `log_file` | `mamad.log` | Log file path (set to `""` for stdout only) |

### Notifications (optional)

**Telegram:**
```yaml
notifications:
  enabled: true
  provider: telegram
  telegram:
    bot_token: "YOUR_BOT_TOKEN"
    chat_id: "YOUR_CHAT_ID"
```

**ntfy:**
```yaml
notifications:
  enabled: true
  provider: ntfy
  ntfy:
    topic: "mamad-roborock"
    server: "https://ntfy.sh"
```

---

## State file

`mamad_state.json` is auto-generated and stores:
- Cached Roborock credentials (no re-login needed after setup)
- Discovered rooms
- Round-robin index
- Per-room last-cleaned timestamps
- Alert history

The file is created with `chmod 600` (owner read/write only). It is excluded from git.

---

## Security notes

- Credentials are stored locally in `mamad_state.json` with restricted permissions.
- `config.yaml` and `mamad_state.json` are both in `.gitignore` — never committed.
- The daemon never logs credentials.
