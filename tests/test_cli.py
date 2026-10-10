import io
import json
from pathlib import Path

import pytest

from slack_fd.cli import apply_key_sugar, main


@pytest.fixture(autouse=True)
def _force_fake_transport(monkeypatch):
    monkeypatch.setenv("SFD_TRANSPORT", "fake")


def test_cli_write_inject_read_log(tmp_path: Path, capsys):
    key = "thread:C9:1.0"
    data = str(tmp_path)

    try:
        main(["--data-dir", data, "write", key, "out-msg"])
    except SystemExit as e:
        assert e.code == 0

    try:
        main(["--data-dir", data, "inject", key, "in-msg"])
    except SystemExit as e:
        assert e.code == 0

    try:
        main(["--data-dir", data, "--json", "read", key, "--timeout", "0.5"])
    except SystemExit as e:
        assert e.code == 0

    out = capsys.readouterr().out.strip()
    rec = json.loads(out)
    assert rec["text"] == "in-msg"
    assert rec["direction"] == "in"

    try:
        main(["--data-dir", data, "log", key, "--which", "both"])
    except SystemExit as e:
        assert e.code == 0
    log_out = capsys.readouterr().out
    assert "out-msg" in log_out
    assert "in-msg" in log_out


def test_cli_invalid_key():
    try:
        main(["read", "not-a-key", "--timeout", "0.1"])
    except SystemExit as e:
        assert e.code == 2


def test_cli_daemon_lifecycle(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.delenv("SFD_TRANSPORT", raising=False)
    data = str(tmp_path)
    try:
        main(["--data-dir", data, "daemon", "start"])
    except SystemExit as e:
        assert e.code == 0
    capsys.readouterr()

    try:
        main(["--data-dir", data, "inject", "dm:U1", "hi"])
    except SystemExit as e:
        assert e.code == 0

    try:
        main(["--data-dir", data, "read", "dm:U1", "--timeout", "1"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "hi"

    try:
        main(["--data-dir", data, "daemon", "stop"])
    except SystemExit as e:
        assert e.code == 0


def test_apply_key_sugar_read_and_write():
    assert apply_key_sugar(["app:U1"], stdin_is_tty=True) == ["read", "app:U1"]
    assert apply_key_sugar(["app:U1", "--timeout", "5"], stdin_is_tty=True) == [
        "read",
        "app:U1",
        "--timeout",
        "5",
    ]
    assert apply_key_sugar(["app:U1"], stdin_is_tty=False) == ["write", "app:U1"]
    assert apply_key_sugar(["--data-dir", "/tmp", "app:U1"], stdin_is_tty=True) == [
        "--data-dir",
        "/tmp",
        "read",
        "app:U1",
    ]
    assert apply_key_sugar(["read", "app:U1"], stdin_is_tty=False) == ["read", "app:U1"]


def test_cli_key_sugar_pipe_write_and_tty_read(tmp_path: Path, monkeypatch, capsys):
    key = "dm:U99"
    data = str(tmp_path)

    monkeypatch.setattr("sys.stdin", io.StringIO("piped-hi\n"))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    try:
        main(["--data-dir", data, key])
    except SystemExit as e:
        assert e.code == 0

    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    try:
        main(["--data-dir", data, "inject", key, "incoming"])
    except SystemExit as e:
        assert e.code == 0
    try:
        main(["--data-dir", data, key, "--timeout", "0.5"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == "incoming"
