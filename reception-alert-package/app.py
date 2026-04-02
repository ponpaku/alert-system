#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging

from alert_service import AlertService
from config import AppConfig, load_config
from destinations import build_destination
from dispatcher import Dispatcher
from transport import HttpTransport


def list_buttons(config: AppConfig) -> None:
    for button in config.buttons:
        destinations = ", ".join(button.destinations) if button.destinations else "(all enabled destinations)"
        print(f"{button.name}: gpio={button.gpio} prefix={button.prefix} message={button.message} destinations={destinations}")


def build_dispatcher(config: AppConfig) -> Dispatcher:
    transport = HttpTransport(config.http)
    destinations = [build_destination(destination, transport) for destination in config.destinations]
    return Dispatcher(destinations, retry_delays_seconds=config.delivery.retry_delays_seconds)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to config.toml")
    parser.add_argument("--test", metavar="BUTTON_NAME", help="Send one test message using the named button definition")
    parser.add_argument("--list-buttons", action="store_true", help="List configured buttons and exit")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    config = load_config(args.config)
    if args.list_buttons:
        list_buttons(config)
        return

    dispatcher = build_dispatcher(config)
    service = AlertService(config, dispatcher, use_gpio=not bool(args.test))

    if args.test:
        try:
            summary = service.dispatch_test_button(args.test)
        finally:
            service.shutdown()
        print(f"Test dispatch result: {summary}")
        if summary == "success":
            raise SystemExit(0)
        if summary == "warning":
            raise SystemExit(2)
        raise SystemExit(1)

    service.run()


if __name__ == "__main__":
    main()
