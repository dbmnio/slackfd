"""Background daemon (Phase 1+)."""

from slack_fd.daemon.lifecycle import is_running, start, status, stop

__all__ = ["is_running", "start", "status", "stop"]
