#!/usr/bin/env bash
set -euo pipefail

APP_USER="${APP_USER:-alert}"
APP_GROUP="${APP_GROUP:-$APP_USER}"
APP_DIR="${APP_DIR:-/opt/reception-alert}"
SERVICE_NAME="${SERVICE_NAME:-reception-alert}"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo ./setup.sh" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt update
apt install -y python3 python3-requests python3-gpiozero python3-lgpio python3-tomli

if ! id -u "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"
fi

mkdir -p "$APP_DIR"
install -m 0755 "$SOURCE_DIR/app.py" "$APP_DIR/app.py"
install -m 0644 "$SOURCE_DIR/alert_service.py" "$APP_DIR/alert_service.py"
install -m 0644 "$SOURCE_DIR/config.py" "$APP_DIR/config.py"
install -m 0644 "$SOURCE_DIR/dispatcher.py" "$APP_DIR/dispatcher.py"
install -m 0644 "$SOURCE_DIR/message_constants.py" "$APP_DIR/message_constants.py"
install -m 0644 "$SOURCE_DIR/models.py" "$APP_DIR/models.py"
install -m 0644 "$SOURCE_DIR/persistent_queue.py" "$APP_DIR/persistent_queue.py"
install -m 0644 "$SOURCE_DIR/send_led_controller.py" "$APP_DIR/send_led_controller.py"
install -m 0644 "$SOURCE_DIR/transport.py" "$APP_DIR/transport.py"
mkdir -p "$APP_DIR/destinations"
install -m 0644 "$SOURCE_DIR/destinations/__init__.py" "$APP_DIR/destinations/__init__.py"
install -m 0644 "$SOURCE_DIR/destinations/base.py" "$APP_DIR/destinations/base.py"
install -m 0644 "$SOURCE_DIR/destinations/common.py" "$APP_DIR/destinations/common.py"
install -m 0644 "$SOURCE_DIR/destinations/discord_webhook.py" "$APP_DIR/destinations/discord_webhook.py"
install -m 0644 "$SOURCE_DIR/destinations/generic_webhook.py" "$APP_DIR/destinations/generic_webhook.py"
install -m 0644 "$SOURCE_DIR/destinations/line_bot.py" "$APP_DIR/destinations/line_bot.py"
install -m 0644 "$SOURCE_DIR/destinations/nextcloud_bot.py" "$APP_DIR/destinations/nextcloud_bot.py"
install -m 0644 "$SOURCE_DIR/destinations/nextcloud_talk.py" "$APP_DIR/destinations/nextcloud_talk.py"
install -m 0644 "$SOURCE_DIR/destinations/slack_webhook.py" "$APP_DIR/destinations/slack_webhook.py"

if [[ -f "$SOURCE_DIR/config.toml" ]]; then
  install -m 0600 "$SOURCE_DIR/config.toml" "$APP_DIR/config.toml"
elif [[ ! -f "$APP_DIR/config.toml" ]]; then
  install -m 0600 "$SOURCE_DIR/config.toml.example" "$APP_DIR/config.toml"
  echo "Created $APP_DIR/config.toml from example. Edit it before starting the service."
fi

sed \
  -e "s|__APP_USER__|$APP_USER|g" \
  -e "s|__APP_GROUP__|$APP_GROUP|g" \
  -e "s|__APP_DIR__|$APP_DIR|g" \
  "$SOURCE_DIR/reception-alert.service" > "$UNIT_PATH"
chmod 0644 "$UNIT_PATH"
chown -R "$APP_USER:$APP_GROUP" "$APP_DIR"
chmod 700 "$APP_DIR"
chmod 600 "$APP_DIR/config.toml"
chmod 755 "$APP_DIR/app.py"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"

echo
echo "Setup complete."
echo "1) Edit: $APP_DIR/config.toml"
echo "2) Test: sudo -u $APP_USER /usr/bin/python3 $APP_DIR/app.py $APP_DIR/config.toml --list-buttons"
echo "3) Send test: sudo -u $APP_USER /usr/bin/python3 $APP_DIR/app.py $APP_DIR/config.toml --test staff"
echo "4) Start service: sudo systemctl restart $SERVICE_NAME"
