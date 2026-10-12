"""Bolt Socket Mode connection owned by the daemon."""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from slack_fd.config import Config

if TYPE_CHECKING:
    from slack_fd.daemon.service import DaemonService

log = logging.getLogger("slack_fd.slack")


class SlackBridge:
    """Owns Bolt App + Socket Mode handler; demuxes inbound to DaemonService."""

    def __init__(self, config: Config, service: DaemonService) -> None:
        self.config = config
        self.service = service
        self.bot_user_id: str | None = None
        self._app: Any = None
        self._handler: Any = None
        self._client: Any = None
        self._started = False

    @property
    def connected(self) -> bool:
        return self._started and self._handler is not None

    @property
    def client(self) -> Any:
        return self._client

    def start(self) -> None:
        if not self.config.slack_ready:
            raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN required")

        try:
            from slack_bolt import App
            from slack_bolt.adapter.socket_mode import SocketModeHandler
            from slack_sdk import WebClient
        except ImportError as e:
            raise RuntimeError(
                "slack-bolt/slack-sdk not installed; run: pip install -e '.[slack]'"
            ) from e

        client = WebClient(token=self.config.bot_token)
        auth = client.auth_test()
        if not auth.get("ok"):
            raise RuntimeError(f"auth.test failed: {auth.get('error')}")
        self.bot_user_id = auth.get("user_id")
        self._client = client
        log.info(
            "slack auth ok team=%s bot_user=%s",
            auth.get("team"),
            self.bot_user_id,
        )

        # process_before_response helps Socket Mode ack promptly under load.
        app = App(token=self.config.bot_token, process_before_response=True)

        @app.middleware
        def _log_envelope(body: dict[str, Any], next: Any) -> Any:  # noqa: A002
            etype = body.get("type")
            event = body.get("event") or {}
            if etype == "event_callback" or event:
                log.info(
                    "slack envelope type=%s event_type=%s subtype=%s channel=%s user=%s",
                    etype,
                    event.get("type"),
                    event.get("subtype"),
                    event.get("channel"),
                    event.get("user"),
                )
            return next()

        @app.event("message")
        def _on_message(event: dict[str, Any], logger: Any) -> None:
            log.info(
                "message event channel=%s channel_type=%s user=%s subtype=%s text=%r",
                event.get("channel"),
                event.get("channel_type"),
                event.get("user"),
                event.get("subtype"),
                (event.get("text") or "")[:80],
            )
            try:
                self.service.ingest_slack_event(event, bot_user_id=self.bot_user_id)
            except Exception:
                logger.exception("failed to ingest slack message")

        self._app = app
        handler = SocketModeHandler(app, self.config.app_token)
        handler.connect()
        self._handler = handler
        self._started = True
        log.info("socket mode connected")
        # Heal gaps + drain outbox without clients calling fetch/flush.
        threading.Thread(
            target=self._reconcile_bg,
            name="sfd-slack-reconcile",
            daemon=True,
        ).start()

    def _reconcile_bg(self) -> None:
        try:
            self.service.reconcile_after_connect()
        except Exception:
            log.exception("post-connect reconcile failed")

    def stop(self) -> None:
        if self._handler is not None:
            try:
                self._handler.close()
            except Exception:
                log.exception("error closing socket mode handler")
            self._handler = None
        self._started = False
