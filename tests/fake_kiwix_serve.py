"""A stand-in for the kiwix-serve program, for kiwix.Server's tests: the same command
line (--address, --port, the ZIM files...), the fake API from zim_fixtures for the files
it is given, until it is stopped. FAKE_KIWIX_EXIT=<code> makes it exit straight away."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
from pathlib import Path

from zim_fixtures import BOOKS, FakeKiwix


def main() -> None:
    if sys.argv[1:] == ["--version"]:
        print("kiwix-tools 9.9.9\n\nlibkiwix 0.0.0")
        return
    if code := os.environ.get("FAKE_KIWIX_EXIT"):
        sys.exit(int(code))
    ap = argparse.ArgumentParser()
    ap.add_argument("--address")
    ap.add_argument("--port", type=int)
    ap.add_argument("--attachToProcess")
    ap.add_argument("--skipInvalid", action="store_true")
    ap.add_argument("files", nargs="*")
    a = ap.parse_args()
    names = {Path(f).name for f in a.files}
    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())
    with FakeKiwix([b for b in BOOKS if b.file in names], a.port):
        done.wait()


if __name__ == "__main__":
    main()
