#!/usr/bin/env python3
from __future__ import annotations

import argparse
import logging
import sys

from alert_service import AlertService, _supports_result_handler
from config import AppConfig, ConfigError, load_config
from destinations import build_destination
from dispatcher import Dispatcher
from persistent_queue import PersistentQueue
from transport import HttpTransport


def list_buttons(config: AppConfig) -> None:
    for button in config.buttons:
        destinations = ", ".join(button.destinations) if button.destinations else "(all enabled destinations)"
        print(f"{button.name}: gpio={button.gpio} prefix={button.prefix} message={button.message} destinations={destinations}")


def build_dispatcher(config: AppConfig) -> Dispatcher:
    transport = HttpTransport(config.http)
    destinations = [build_destination(destination, transport) for destination in config.destinations]
    return Dispatcher(
        destinations,
        retry_delays_seconds=config.delivery.retry_delays_seconds,
        max_parallel_destinations=config.delivery.max_parallel_destinations,
        max_retry_after_seconds=config.delivery.max_retry_after_seconds,
        running_cutoff_grace_seconds=config.delivery.running_cutoff_grace_seconds,
        transport=transport,
        owns_transport=True,
    )


def open_validation_queue_store(config: AppConfig) -> PersistentQueue:
    return PersistentQueue(
        config.delivery.persistent_queue_path,
        capacity=config.delivery.queue_capacity,
        retry_base_seconds=config.delivery.persistent_retry_base_seconds,
        retry_max_seconds=config.delivery.persistent_retry_max_seconds,
        recover_processing_rows=True,
    )


def validate_dispatcher_runtime_contract(dispatcher: Dispatcher) -> None:
    if not _supports_result_handler(dispatcher.dispatch):
        raise ConfigError("persistent queue mode requires a dispatcher that supports result_handler")


def validate_runtime(config: AppConfig) -> None:
    dispatcher = build_dispatcher(config)
    queue_store = None
    service = None
    try:
        validate_dispatcher_runtime_contract(dispatcher)
        queue_store = open_validation_queue_store(config)
        service = AlertService(
            config,
            dispatcher,
            use_gpio=False,
            enable_queue_worker=False,
            enable_heartbeat=False,
        )
    finally:
        if service is not None:
            service.shutdown()
        else:
            dispatcher.close()
        if queue_store is not None:
            queue_store.close()


def validate_gpio_runtime(config: AppConfig) -> None:
    dispatcher = build_dispatcher(config)
    queue_store = None
    service = None
    try:
        validate_dispatcher_runtime_contract(dispatcher)
        queue_store = open_validation_queue_store(config)
        service = AlertService(
            config,
            dispatcher,
            use_gpio=True,
            enable_queue_worker=False,
            enable_heartbeat=False,
        )
    finally:
        if service is not None:
            service.shutdown()
        else:
            dispatcher.close()
        if queue_store is not None:
            queue_store.close()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("config", help="Path to config.toml")
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--test", metavar="BUTTON_NAME", help="Send one test message using the named button definition")
    mode_group.add_argument("--list-buttons", action="store_true", help="List configured buttons and exit")
    mode_group.add_argument(
        "--validate-runtime",
        action="store_true",
        help="Validate non-GPIO runtime dependencies and open/recover queue storage without sending alerts or starting the queue worker",
    )
    mode_group.add_argument(
        "--validate-gpio",
        action="store_true",
        help="Validate GPIO startup and open/recover queue storage without sending alerts or starting the queue worker",
    )
    return parser


def main() -> None:
    parser = build_argument_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        config = load_config(args.config)
        if args.list_buttons:
            list_buttons(config)
            return
        if args.validate_runtime:
            validate_runtime(config)
            print("Non-GPIO runtime validation passed")
            return
        if args.validate_gpio:
            validate_gpio_runtime(config)
            print("GPIO runtime validation passed")
            return

        dispatcher = build_dispatcher(config)
        service = AlertService(
            config,
            dispatcher,
            use_gpio=not bool(args.test),
            enable_queue_worker=not bool(args.test),
            enable_heartbeat=not bool(args.test),
        )

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
    except ConfigError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
