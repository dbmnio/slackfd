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


class ResolveError(SlackFdError):
    """Name could not be resolved to a Slack channel id."""


class AmbiguousResolve(ResolveError):
    """Multiple Slack objects matched the query."""

    def __init__(self, message: str, *, candidates: list[str]) -> None:
        super().__init__(message)
        self.candidates = candidates
