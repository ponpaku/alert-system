from __future__ import annotations

from config import (
    DestinationConfig,
    DiscordWebhookConfig,
    GenericWebhookConfig,
    LineBotConfig,
    NextcloudBotConfig,
    NextcloudTalkConfig,
    SlackWebhookConfig,
)
from transport import HttpTransport

from .base import Destination
from .discord_webhook import DiscordWebhookDestination
from .generic_webhook import GenericWebhookDestination
from .line_bot import LineBotDestination
from .nextcloud_bot import NextcloudBotDestination
from .nextcloud_talk import NextcloudTalkDestination
from .slack_webhook import SlackWebhookDestination


def build_destination(config: DestinationConfig, transport: HttpTransport) -> Destination:
    if isinstance(config, NextcloudTalkConfig):
        return NextcloudTalkDestination(config, transport)
    if isinstance(config, NextcloudBotConfig):
        return NextcloudBotDestination(config, transport)
    if isinstance(config, DiscordWebhookConfig):
        return DiscordWebhookDestination(config, transport)
    if isinstance(config, SlackWebhookConfig):
        return SlackWebhookDestination(config, transport)
    if isinstance(config, LineBotConfig):
        return LineBotDestination(config, transport)
    if isinstance(config, GenericWebhookConfig):
        return GenericWebhookDestination(config, transport)
    raise TypeError(f"Unsupported destination config: {config!r}")
