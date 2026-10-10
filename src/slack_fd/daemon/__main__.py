"""python -m slack_fd.daemon"""

from __future__ import annotations

import os
from pathlib import Path

from slack_fd.daemon.server import run
from slack_fd.paths import default_data_dir


def main() -> None:
    data = os.environ.get("SFD_DATA_DIR")
    run(Path(data).expanduser() if data else default_data_dir())


if __name__ == "__main__":
    main()
