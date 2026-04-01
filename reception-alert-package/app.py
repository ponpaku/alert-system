#!/usr/bin/env python3
import argparse
import json
import logging
import signal
import threading
import time
from pathlib import Path
from typing import Any

import requests
from gpiozero import Button, Device, LED

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


CONFIG_SUFFIXES = {".toml"}


def load_config(path: str) -> dict[str, Any]:
    config_path = Path(path)
    if config_path.suffix.lower() not in CONFIG_SUFFIXES:
        raise ValueError(f"Unsupported config format: {config_path.suffix}")

    with open(config_path, "rb") as f:
        return tomllib.load(f)


class TalkUserClient:
    def __init__(self, config: dict[str, Any]):
        self.base_url = str(config["nextcloud_base_url"]).rstrip("/")
        self.username = str(config["nextcloud_username"])
        self.app_password = str(config["nextcloud_app_password"])
        self.room_token = str(config["talk_room_token"])
        self.timeout = float(config.get("request_timeout_seconds", 5))

        ca_bundle_path = str(config.get("ca_bundle_path", "")).strip()
        if ca_bundle_path:
            self.verify: bool | str = ca_bundle_path
        else:
            self.verify = bool(config.get("verify_tls", True))

        self.session = requests.Session()
        self.session.auth = (self.username, self.app_password)
        self.session.headers.update(
            {
                "OCS-APIRequest": "true",
                "Accept": "application/json",
                "Content-Type": "application/json",
            }
        )

    def send_message(self, message: str) -> None:
        url = f"{self.base_url}/ocs/v2.php/apps/spreed/api/v1/chat/{self.room_token}"
        resp = self.session.post(
            url,
            json={"message": message},
            timeout=self.timeout,
            verify=self.verify,
        )
        if resp.status_code != 201:
            body = resp.text[:500]
            raise RuntimeError(f"Talk send failed: HTTP {resp.status_code} {body}")



def list_buttons(config: dict[str, Any]) -> None:
    for item in config["buttons"]:
        print(
            f'{item["name"]}: gpio={item["gpio"]} '
            f'prefix={item.get("prefix", "")} '
            f'message={item["message"]}'
        )



def send_test_without_gpio(config: dict[str, Any], button_name: str) -> None:
    buttons = {item["name"]: item for item in config["buttons"]}
    if button_name not in buttons:
        valid = ", ".join(buttons.keys())
        raise ValueError(f"Unknown button name: {button_name}. Valid: {valid}")

    item = buttons[button_name]
    message = (
        f"【受付ボタン試験】 {item['message']}\n"
        f"場所：{config['location_name']}"
    )

    client = TalkUserClient(config)
    client.send_message(message)
    print(f"Sent test message for button: {button_name}")


class AlertService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.client = TalkUserClient(config)

        self.location_name = str(config["location_name"])

        self.cooldown_seconds = float(config.get("cooldown_seconds", 3))
        self.bounce_seconds = float(config.get("bounce_seconds", 0.08))
        self.retry_delays = [float(x) for x in config.get("retry_delays_seconds", [0, 1, 3])]
        self.success_hold_seconds = float(config.get("success_hold_seconds", 30))
        self.failure_blink_seconds = float(config.get("failure_blink_seconds", 30))

        self.alive_led_gpio = int(config.get("alive_led_gpio", 5))
        self.send_led_gpio = int(config.get("send_led_gpio", 27))

        self.alive_led = LED(self.alive_led_gpio)
        self.send_led = LED(self.send_led_gpio)

        self._busy_lock = threading.Lock()
        self._last_sent_monotonic = 0.0
        self._stop_event = threading.Event()
        self._send_led_timer: threading.Timer | None = None
        self._send_led_timer_lock = threading.Lock()
        self._send_thread: threading.Thread | None = None
        self._send_thread_lock = threading.Lock()
        self._led_lock = threading.Lock()
        self._leds_closed = False

        self.button_defs = config["buttons"]
        self.buttons: list[Button] = []
        self.button_map: dict[str, dict[str, Any]] = {}

        for item in self.button_defs:
            name = str(item["name"])
            gpio = int(item["gpio"])
            btn = Button(
                gpio,
                pull_up=True,
                bounce_time=self.bounce_seconds,
            )
            btn.when_pressed = lambda item=item: self.trigger_async(item)
            self.buttons.append(btn)
            self.button_map[name] = item

    def build_message(self, item: dict[str, Any], kind: str = "alert") -> str:
        prefix = str(item.get("prefix", "【受付通知】"))
        text = str(item["message"])

        if kind == "test":
            prefix = "【受付ボタン試験】"

        return f"{prefix} {text}\n場所：{self.location_name}"

    def _cancel_send_led_timer(self) -> None:
        with self._send_led_timer_lock:
            if self._send_led_timer is not None:
                self._send_led_timer.cancel()
                self._send_led_timer = None

    def _safe_send_led_on(self) -> bool:
        with self._led_lock:
            if self._leds_closed:
                return False
            self.send_led.on()
            return True

    def _safe_send_led_off(self) -> bool:
        with self._led_lock:
            if self._leds_closed:
                return False
            self.send_led.off()
            return True

    def _send_led_off(self) -> None:
        self._cancel_send_led_timer()
        self._safe_send_led_off()

    def _set_send_led_success_hold(self, seconds: float) -> None:
        self._cancel_send_led_timer()
        if not self._safe_send_led_on():
            return
        timer = threading.Timer(seconds, self._safe_send_led_off)
        timer.daemon = True
        with self._send_led_timer_lock:
            self._send_led_timer = timer
        timer.start()

    def _blink_send_led_until(self, stop_event: threading.Event, on_sec: float, off_sec: float) -> None:
        while not stop_event.is_set() and not self._stop_event.is_set():
            if not self._safe_send_led_on():
                break
            if stop_event.wait(on_sec) or self._stop_event.is_set():
                break
            if not self._safe_send_led_off():
                break
            if stop_event.wait(off_sec) or self._stop_event.is_set():
                break
        self._safe_send_led_off()

    def _blink_failure_for(self, seconds: float) -> None:
        end_at = time.monotonic() + seconds
        while time.monotonic() < end_at and not self._stop_event.is_set():
            if not self._safe_send_led_on():
                break
            if self._stop_event.wait(0.5):
                break
            if not self._safe_send_led_off():
                break
            if self._stop_event.wait(0.5):
                break
        self._safe_send_led_off()

    def _send_with_retry(self, item: dict[str, Any], kind: str = "alert") -> None:
        blink_stop = threading.Event()
        blink_thread = threading.Thread(
            target=self._blink_send_led_until,
            args=(blink_stop, 0.15, 0.15),
            daemon=True,
        )

        self._send_led_off()
        blink_thread.start()

        try:
            msg = self.build_message(item, kind=kind)
            last_error: Exception | None = None

            for delay in self.retry_delays:
                if self._stop_event.is_set():
                    logging.info("Stopping send for %s before retry", item["name"])
                    return
                if delay > 0:
                    if self._stop_event.wait(delay):
                        logging.info("Stopping send for %s during retry wait", item["name"])
                        return

                try:
                    self.client.send_message(msg)
                    self._last_sent_monotonic = time.monotonic()
                    logging.info("Message sent successfully: %s", item["name"])
                    blink_stop.set()
                    blink_thread.join(timeout=1.0)
                    if not self._stop_event.is_set():
                        self._set_send_led_success_hold(self.success_hold_seconds)
                    return
                except Exception as e:  # pragma: no cover - runtime path
                    last_error = e
                    logging.warning("Send failed for %s: %s", item["name"], e)

            logging.error("All retries failed for %s: %s", item["name"], last_error)
            blink_stop.set()
            blink_thread.join(timeout=1.0)
            if not self._stop_event.is_set():
                self._blink_failure_for(self.failure_blink_seconds)

        finally:
            blink_stop.set()
            if blink_thread.is_alive():
                blink_thread.join(timeout=1.0)
            with self._send_thread_lock:
                if self._send_thread is threading.current_thread():
                    self._send_thread = None
            self._busy_lock.release()

    def trigger_async(self, item: dict[str, Any]) -> None:
        now = time.monotonic()

        if now - self._last_sent_monotonic < self.cooldown_seconds:
            logging.warning("Ignored press due to cooldown: %s", item["name"])
            return

        if not self._busy_lock.acquire(blocking=False):
            logging.warning("Ignored press because sender is busy: %s", item["name"])
            return

        t = threading.Thread(target=self._send_with_retry, args=(item, "alert"), daemon=True)
        with self._send_thread_lock:
            self._send_thread = t
        t.start()

    def _signal_handler(self, signum: int, frame: Any) -> None:
        logging.info("Received signal %s, stopping", signum)
        self._stop_event.set()

    def run(self) -> None:
        logging.info("Pin factory: %s", Device.pin_factory)
        logging.info("Ready with %d buttons", len(self.buttons))
        self.alive_led.on()

        signal.signal(signal.SIGTERM, self._signal_handler)
        signal.signal(signal.SIGINT, self._signal_handler)

        try:
            while not self._stop_event.is_set():
                time.sleep(1)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._cancel_send_led_timer()
        with self._send_thread_lock:
            send_thread = self._send_thread
        if send_thread is not None and send_thread.is_alive():
            send_thread.join(timeout=self.client.timeout + 1.0)
        for btn in self.buttons:
            btn.close()
        with self._led_lock:
            if not self._leds_closed:
                self.send_led.close()
                self.alive_led.close()
                self._leds_closed = True
        logging.info("Shutdown complete")



def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to config.toml")
    parser.add_argument("--test", metavar="BUTTON_NAME", help="Send one test message using the named button definition")
    parser.add_argument("--list-buttons", action="store_true", help="List configured buttons and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    config = load_config(args.config)

    if args.list_buttons:
        list_buttons(config)
        return

    if args.test:
        send_test_without_gpio(config, args.test)
        return

    service = AlertService(config)
    service.run()


if __name__ == "__main__":
    main()
