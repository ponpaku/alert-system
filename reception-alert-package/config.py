from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


CONFIG_SUFFIXES = {".toml"}
GENERIC_METHODS = {"POST", "PUT", "PATCH"}
GENERIC_CONTENT_TYPES = {"json", "form", "text"}
GENERIC_AUTH_TYPES = {"none", "bearer", "basic", "header"}


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class HttpConfig:
    user_agent: str
    request_timeout_seconds: float
    verify_tls: bool
    ca_bundle_path: str


@dataclass(frozen=True)
class GpioConfig:
    alive_led_gpio: int
    send_led_gpio: int


@dataclass(frozen=True)
class TimingConfig:
    bounce_seconds: float
    cooldown_seconds: float
    success_hold_seconds: float
    failure_blink_seconds: float


@dataclass(frozen=True)
class DeliveryConfig:
    retry_delays_seconds: tuple[float, ...]
    queue_capacity: int
    shutdown_grace_seconds: float


@dataclass(frozen=True)
class ButtonConfig:
    name: str
    gpio: int
    prefix: str
    message: str
    destinations: tuple[str, ...] | None


@dataclass(frozen=True)
class GenericWebhookAuthConfig:
    type: Literal["none", "bearer", "basic", "header"]
    token: str | None = None
    username: str | None = None
    password: str | None = None
    header_name: str | None = None
    header_value: str | None = None


@dataclass(frozen=True)
class DestinationConfig:
    type: str
    name: str
    enabled: bool


@dataclass(frozen=True)
class NextcloudTalkConfig(DestinationConfig):
    base_url: str
    username: str
    app_password: str
    room_token: str


@dataclass(frozen=True)
class NextcloudBotConfig(DestinationConfig):
    base_url: str
    conversation_token: str
    shared_secret: str
    silent: bool


@dataclass(frozen=True)
class DiscordWebhookConfig(DestinationConfig):
    webhook_url: str


@dataclass(frozen=True)
class SlackWebhookConfig(DestinationConfig):
    webhook_url: str


@dataclass(frozen=True)
class LineBotConfig(DestinationConfig):
    channel_access_token: str
    to: str


@dataclass(frozen=True)
class GenericWebhookConfig(DestinationConfig):
    url: str
    method: Literal["POST", "PUT", "PATCH"]
    content_type: Literal["json", "form", "text"]
    success_status_codes: tuple[int, ...] | None
    headers: dict[str, str]
    auth: GenericWebhookAuthConfig
    payload: Any


@dataclass(frozen=True)
class AppConfig:
    location_name: str
    http: HttpConfig
    gpio: GpioConfig
    timing: TimingConfig
    delivery: DeliveryConfig
    destinations: tuple[DestinationConfig, ...]
    buttons: tuple[ButtonConfig, ...]

    def button_by_name(self, button_name: str) -> ButtonConfig:
        for button in self.buttons:
            if button.name == button_name:
                return button
        valid = ", ".join(button.name for button in self.buttons)
        raise ConfigError(f"Unknown button name: {button_name}. Valid: {valid}")


def load_config(path: str) -> AppConfig:
    config_path = Path(path)
    is_toml_example = config_path.name.endswith(".toml.example")
    if config_path.suffix.lower() not in CONFIG_SUFFIXES and not is_toml_example:
        raise ConfigError(f"Unsupported config format: {config_path.suffix}")
    with open(config_path, "rb") as file_obj:
        raw = tomllib.load(file_obj)
    return parse_config(raw)


def parse_config(raw: dict[str, Any]) -> AppConfig:
    location_name = _require_str(raw, "location_name")
    http_raw = _require_dict(raw, "http")
    gpio_raw = _require_dict(raw, "gpio")
    timing_raw = _require_dict(raw, "timing")
    delivery_raw = _require_dict(raw, "delivery")
    http = HttpConfig(
        user_agent=str(http_raw.get("user_agent", "ReceptionAlert/1.0")),
        request_timeout_seconds=_float(http_raw.get("request_timeout_seconds", 5)),
        verify_tls=bool(http_raw.get("verify_tls", True)),
        ca_bundle_path=str(http_raw.get("ca_bundle_path", "")).strip(),
    )
    gpio = GpioConfig(
        alive_led_gpio=_int(gpio_raw.get("alive_led_gpio", 5)),
        send_led_gpio=_int(gpio_raw.get("send_led_gpio", 27)),
    )
    timing = TimingConfig(
        bounce_seconds=_float(timing_raw.get("bounce_seconds", 0.08)),
        cooldown_seconds=_float(timing_raw.get("cooldown_seconds", 3)),
        success_hold_seconds=_float(timing_raw.get("success_hold_seconds", 30)),
        failure_blink_seconds=_float(timing_raw.get("failure_blink_seconds", 30)),
    )
    retry_delays = tuple(_float(value) for value in delivery_raw.get("retry_delays_seconds", [0, 1, 3]))
    delivery = DeliveryConfig(
        retry_delays_seconds=retry_delays,
        queue_capacity=_int(delivery_raw.get("queue_capacity", 8)),
        shutdown_grace_seconds=_float(delivery_raw.get("shutdown_grace_seconds", 6)),
    )
    raw_destinations = raw.get("destinations")
    if not isinstance(raw_destinations, list) or not raw_destinations:
        raise ConfigError("destinations must be a non-empty array")
    destinations = tuple(_parse_destination(entry) for entry in raw_destinations)
    raw_buttons = raw.get("buttons")
    if not isinstance(raw_buttons, list) or not raw_buttons:
        raise ConfigError("buttons must be a non-empty array")
    buttons = tuple(_parse_button(entry) for entry in raw_buttons)
    config = AppConfig(location_name, http, gpio, timing, delivery, destinations, buttons)
    _validate_app_config(config)
    return config


def _parse_destination(entry: Any) -> DestinationConfig:
    if not isinstance(entry, dict):
        raise ConfigError("each destination entry must be a table")
    destination_type = _require_str(entry, "type")
    common = {
        "type": destination_type,
        "name": _require_str(entry, "name"),
        "enabled": bool(entry.get("enabled", True)),
    }
    if destination_type == "nextcloud_talk":
        return NextcloudTalkConfig(
            **common,
            base_url=_require_str(entry, "base_url"),
            username=_require_str(entry, "username"),
            app_password=_require_str(entry, "app_password"),
            room_token=_require_str(entry, "room_token"),
        )
    if destination_type == "nextcloud_bot":
        return NextcloudBotConfig(
            **common,
            base_url=_require_str(entry, "base_url"),
            conversation_token=_require_str(entry, "conversation_token"),
            shared_secret=_require_str(entry, "shared_secret"),
            silent=bool(entry.get("silent", False)),
        )
    if destination_type == "discord_webhook":
        return DiscordWebhookConfig(**common, webhook_url=_require_str(entry, "webhook_url"))
    if destination_type == "slack_webhook":
        return SlackWebhookConfig(**common, webhook_url=_require_str(entry, "webhook_url"))
    if destination_type == "line_bot":
        return LineBotConfig(**common, channel_access_token=_require_str(entry, "channel_access_token"), to=_require_str(entry, "to"))
    if destination_type == "generic_webhook":
        auth = _parse_generic_auth(entry.get("auth", {"type": "none"}))
        success_codes_raw = entry.get("success_status_codes")
        success_codes = None
        if success_codes_raw is not None:
            if not isinstance(success_codes_raw, list) or not success_codes_raw:
                raise ConfigError(f"destination '{common['name']}' success_status_codes must be a non-empty array")
            success_codes = tuple(_int(code) for code in success_codes_raw)
        headers_raw = entry.get("headers", {})
        if not isinstance(headers_raw, dict):
            raise ConfigError(f"destination '{common['name']}' headers must be a table")
        return GenericWebhookConfig(
            **common,
            url=_require_str(entry, "url"),
            method=str(entry.get("method", "POST")).upper(),  # type: ignore[arg-type]
            content_type=str(entry.get("content_type", "json")),  # type: ignore[arg-type]
            success_status_codes=success_codes,
            headers={str(key): str(value) for key, value in headers_raw.items()},
            auth=auth,
            payload=entry.get("payload"),
        )
    raise ConfigError(f"Unknown destination type: {destination_type}")


def _parse_generic_auth(raw: Any) -> GenericWebhookAuthConfig:
    if not isinstance(raw, dict):
        raise ConfigError("generic_webhook auth must be a table")
    return GenericWebhookAuthConfig(
        type=str(raw.get("type", "none")),  # type: ignore[arg-type]
        token=_optional_str(raw.get("token")),
        username=_optional_str(raw.get("username")),
        password=_optional_str(raw.get("password")),
        header_name=_optional_str(raw.get("header_name")),
        header_value=_optional_str(raw.get("header_value")),
    )


def _parse_button(entry: Any) -> ButtonConfig:
    if not isinstance(entry, dict):
        raise ConfigError("each button entry must be a table")
    destinations_raw = entry.get("destinations")
    destinations = None
    if destinations_raw is not None:
        if not isinstance(destinations_raw, list) or not destinations_raw:
            raise ConfigError(f"button '{_require_str(entry, 'name')}' destinations must be a non-empty array")
        destinations = tuple(str(name) for name in destinations_raw)
    return ButtonConfig(
        name=_require_str(entry, "name"),
        gpio=_int(entry.get("gpio")),
        prefix=str(entry.get("prefix", "")).strip(),
        message=_require_str(entry, "message"),
        destinations=destinations,
    )


def _validate_app_config(config: AppConfig) -> None:
    if config.delivery.queue_capacity < 1:
        raise ConfigError("delivery.queue_capacity must be >= 1")
    _validate_positive("http.request_timeout_seconds", config.http.request_timeout_seconds)
    _validate_non_negative("timing.bounce_seconds", config.timing.bounce_seconds)
    _validate_non_negative("timing.cooldown_seconds", config.timing.cooldown_seconds)
    _validate_non_negative("timing.success_hold_seconds", config.timing.success_hold_seconds)
    _validate_non_negative("timing.failure_blink_seconds", config.timing.failure_blink_seconds)
    _validate_non_negative("delivery.shutdown_grace_seconds", config.delivery.shutdown_grace_seconds)
    if not config.delivery.retry_delays_seconds:
        raise ConfigError("delivery.retry_delays_seconds must not be empty")
    for index, delay in enumerate(config.delivery.retry_delays_seconds):
        _validate_non_negative(f"delivery.retry_delays_seconds[{index}]", delay)
    _validate_unique("destination.name", [destination.name for destination in config.destinations])
    _validate_unique("button.name", [button.name for button in config.buttons])
    _validate_unique("button.gpio", [str(button.gpio) for button in config.buttons])
    destination_names = {destination.name for destination in config.destinations}
    enabled_destination_names = {destination.name for destination in config.destinations if destination.enabled}
    if not any(destination.enabled for destination in config.destinations):
        raise ConfigError("At least one destination must be enabled")
    for button in config.buttons:
        if button.destinations is not None:
            _validate_unique(f"button '{button.name}' destinations", list(button.destinations))
            unknown = [name for name in button.destinations if name not in destination_names]
            if unknown:
                raise ConfigError(f"button '{button.name}' references unknown destinations: {', '.join(unknown)}")
            disabled = [name for name in button.destinations if name not in enabled_destination_names]
            if disabled:
                raise ConfigError(f"button '{button.name}' references disabled destinations: {', '.join(disabled)}")
    for destination in config.destinations:
        if isinstance(destination, GenericWebhookConfig):
            if destination.method not in GENERIC_METHODS:
                raise ConfigError(f"destination '{destination.name}' method must be one of: {', '.join(sorted(GENERIC_METHODS))}")
            if destination.content_type not in GENERIC_CONTENT_TYPES:
                raise ConfigError(f"destination '{destination.name}' content_type must be one of: {', '.join(sorted(GENERIC_CONTENT_TYPES))}")
            if destination.auth.type not in GENERIC_AUTH_TYPES:
                raise ConfigError(f"destination '{destination.name}' auth.type must be one of: {', '.join(sorted(GENERIC_AUTH_TYPES))}")
            _validate_generic_auth(destination)
            _validate_generic_payload(destination)


def _validate_generic_auth(destination: GenericWebhookConfig) -> None:
    auth = destination.auth
    if auth.type == "none":
        return
    if auth.type == "bearer":
        if not auth.token:
            raise ConfigError(f"destination '{destination.name}' auth.token must be set when auth.type=bearer")
        return
    if auth.type == "basic":
        if not auth.username:
            raise ConfigError(f"destination '{destination.name}' auth.username must be set when auth.type=basic")
        if not auth.password:
            raise ConfigError(f"destination '{destination.name}' auth.password must be set when auth.type=basic")
        return
    if auth.type == "header":
        if not auth.header_name:
            raise ConfigError(f"destination '{destination.name}' auth.header_name must be set when auth.type=header")
        return


def _validate_generic_payload(destination: GenericWebhookConfig) -> None:
    if destination.content_type == "text":
        if destination.payload is not None and not isinstance(destination.payload, str):
            raise ConfigError(f"destination '{destination.name}' payload must be a string for content_type=text")
    elif destination.content_type == "form":
        if destination.payload is not None and not isinstance(destination.payload, dict):
            raise ConfigError(f"destination '{destination.name}' payload must be a table for content_type=form")
    elif destination.content_type == "json":
        if destination.payload is not None and not isinstance(destination.payload, (dict, list, str, int, float, bool)):
            raise ConfigError(f"destination '{destination.name}' payload must be dict/list/scalar for content_type=json")


def _validate_unique(label: str, values: list[str]) -> None:
    duplicates = sorted({value for value in values if values.count(value) > 1})
    if duplicates:
        raise ConfigError(f"Duplicate {label}: {', '.join(duplicates)}")


def _validate_positive(label: str, value: float) -> None:
    if value <= 0:
        raise ConfigError(f"{label} must be > 0")


def _validate_non_negative(label: str, value: float) -> None:
    if value < 0:
        raise ConfigError(f"{label} must be >= 0")


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a non-empty string")
    return value.strip()


def _require_dict(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    if not isinstance(value, dict):
        raise ConfigError(f"{key} must be a table")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _float(value: Any) -> float:
    if isinstance(value, bool):
        raise ConfigError("boolean is not a valid float value")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid float value: {value!r}") from exc


def _int(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError("boolean is not a valid int value")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"invalid int value: {value!r}") from exc
