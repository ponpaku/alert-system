# alert-system

`alert-system` is a Raspberry Pi based reception alert dispatcher.
It watches physical GPIO buttons, builds a normalized alert event, persists that event to a local SQLite queue, and fans out notifications to one or more chat/webhook destinations.

The deployable application lives in `reception-alert-package/`.

## What It Does

- Accepts button presses from a Raspberry Pi front desk device
- Applies debounce and cooldown rules before accepting a new alert
- Persists accepted alerts to a SQLite backed queue before delivery
- Delivers alerts to multiple destinations in parallel
- Retries retryable failures and honors `Retry-After` when available
- Can emit heartbeat webhooks for external uptime monitoring
- Keeps running cleanly through shutdown and startup recovery
- Supports operator test commands without enabling GPIO monitoring

Supported destinations:

- Nextcloud Talk
- Nextcloud Bot
- Discord Webhook
- Slack Webhook
- LINE Bot
- Generic Webhook

## Repository Layout

- `reception-alert-package/app.py`: CLI entrypoint and runtime validation commands
- `reception-alert-package/alert_service.py`: GPIO handling, queue worker, shutdown flow, LED behavior
- `reception-alert-package/config.py`: TOML parsing and fail-fast config validation
- `reception-alert-package/dispatcher.py`: parallel fan-out, retry, deadline, and cutoff handling
- `reception-alert-package/persistent_queue.py`: SQLite queue and retry scheduling
- `reception-alert-package/destinations/`: destination-specific delivery adapters
- `reception-alert-package/setup.sh`: Raspberry Pi install script
- `reception-alert-package/reception-alert.service`: systemd unit template
- `reception-alert-package/config.toml.example`: example configuration
- `reception-alert-package/tests/`: unit test suite

## Runtime Flow

1. A GPIO button press is accepted if the worker is healthy and cooldown allows it.
2. The service creates an alert event with a unique event ID and timestamp.
3. The event is written to the persistent queue before any outbound delivery starts.
4. The queue worker claims ready records and dispatches them to the selected destinations.
5. Successful destinations are removed from the pending target list.
6. Retryable failures are re-queued with backoff until they succeed or keep failing.
7. LED behavior reflects activity and final delivery status.

## Configuration

The application reads a TOML file such as `reception-alert-package/config.toml.example`.

Main sections:

- `location_name`: human-readable location label included in messages
- `http`: shared HTTP transport settings
- `gpio`: alive LED and send LED pin numbers
- `timing`: debounce, cooldown, and LED hold/blink timing
- `delivery`: retry delays, queue capacity, shutdown grace, parallelism, and persistent queue path
- `heartbeat`: optional periodic heartbeat webhook settings for external monitors such as Google Apps Script
- `destinations`: enabled notification backends
- `buttons`: GPIO button definitions and per-button routing rules

About `name`:

- each entry in `destinations` needs a unique `name`
- `buttons[].destinations` does not use destination type or webhook URL; it uses that `name`
- destination `name` is the routing key that connects buttons to destinations
- if a button references a `name` that is missing or disabled, startup stops with a configuration error

Each button can target specific destinations with `buttons[].destinations`.
If that field is omitted, the button fans out to all enabled destinations.

Heartbeat notes:

- `heartbeat.enabled = true` turns on startup, periodic, and shutdown heartbeat delivery
- default interval is `300` seconds with `15` seconds of send jitter
- the heartbeat payload includes `event`, `status`, `instance_id`, timestamps, and optional `queue_depth` / `worker_alive`
- `heartbeat.stale_after_seconds` is intended for the remote monitor's missing-heartbeat threshold and defaults to `900`
- heartbeat failures are logged but do not block alert dispatch

The generic webhook destination supports template placeholders inside payload strings:

- `event_id`
- `button_name`
- `prefix`
- `message`
- `location_name`
- `kind`
- `occurred_at`
- `source`
- `text`

## Local Development

Python 3.11+ is the best fit because the code uses `tomllib`.
For non-GPIO development and tests, you do not need Raspberry Pi hardware.

Example test command:

```bash
cd reception-alert-package
python -m unittest discover -s tests -v
```

Useful CLI commands:

```bash
python app.py config.toml --list-buttons
python app.py config.toml --test staff
python app.py config.toml --validate-runtime
python app.py config.toml --validate-gpio
```

Exit codes for `--test`:

- `0`: all destinations succeeded
- `2`: warning / not-attempted result occurred
- `1`: at least one destination failed

## Raspberry Pi Setup

The repository includes a simple install script for a Debian-based Raspberry Pi environment.

```bash
cd /home/user
git clone https://github.com/ponpaku/alert-system.git
cd alert-system/reception-alert-package
chmod +x setup.sh
sudo ./setup.sh
```

The script:

- installs system packages such as `python3-requests`, `python3-gpiozero`, `python3-lgpio`, and `python3-tomli`
- creates the `alert` system user if needed
- installs the app into `/opt/reception-alert`
- creates `/opt/reception-alert/config.toml` from the example when missing
- installs and enables the `reception-alert` systemd service

After setup:

```bash
sudoedit /opt/reception-alert/config.toml
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --list-buttons
sudo -u alert /usr/bin/python3 /opt/reception-alert/app.py /opt/reception-alert/config.toml --test staff
sudo systemctl restart reception-alert
sudo systemctl status reception-alert
journalctl -u reception-alert -f
```

## Operational Notes

- Alerts are persisted before network delivery, so transient outbound failures do not immediately lose events.
- On restart, any queue rows left in `processing` are recovered back to `queued`.
- During shutdown, new button presses are rejected and in-flight work is allowed a bounded grace period.
- `--validate-runtime` and `--validate-gpio` are safe startup checks that open and recover queue storage without sending alerts.

## Current Caveats

- The repository currently contains generated `__pycache__` files and local notes that are not part of the documentation update in this README.
- `reception-alert-package/README.md` exists as a package-level document, but this root README is intended to be the main entrypoint for the repository.
