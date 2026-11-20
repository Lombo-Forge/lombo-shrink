"""Alerts to any webhook. The body carries the text under both "content" (Discord) and "text"
(Slack and compatible), so either works. For ntfy, Gotify or email, point this at an Apprise
or similar bridge. No service is built in and no URL is ever hard-coded."""

import json
import urllib.error
import urllib.request


class NotifyError(Exception):
    """The webhook did not accept the message."""


def send(url: str, message: str, timeout: float = 10) -> None:
    body = json.dumps({"content": message, "text": message}).encode()
    # S310 is silenced here and below: config.py only accepts http(s) URLs.
    req = urllib.request.Request(  # noqa: S310
        url, data=body, method="POST", headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):  # noqa: S310
            pass
    except (urllib.error.URLError, OSError) as e:
        raise NotifyError(f"webhook failed: {e}") from e
