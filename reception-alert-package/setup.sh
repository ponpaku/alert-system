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
