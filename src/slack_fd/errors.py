"""Small typed error set for slack_fd."""


class SlackFdError(Exception):
    """Base error for slack_fd."""


class InvalidKey(SlackFdError, ValueError):
    """Stream key could not be parsed or is invalid."""


class DaemonUnavailable(SlackFdError):
    """Daemon is not reachable and embed was not allowed."""


class Timeout(SlackFdError):
    """Blocking read timed out."""


class SendFailed(SlackFdError):
    """Outbound message failed to send."""
