"""Review fixes in the install scripts, run in a scratch folder with launchctl stubbed:
a failure inside a function reports the error, an install that failed part way is run
again, and a lock left by a killed installer doesn't block installs for good. The real
installer is never run (a stand-in install.sh records that it was started)."""

import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRATCH = REPO / ".tmp" / "fix-memory-tests"
INSTALL = (REPO / "scripts" / "install.sh").read_text()
BASH = "/bin/bash"

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="ensure-running.sh runs only on macOS")


@pytest.fixture
def box() -> Iterator[Path]:
    d = SCRATCH / uuid.uuid4().hex[:12]
    (d / "root" / "scripts").mkdir(parents=True)
    (d / "data" / "state").mkdir(parents=True)
    (d / "bin").mkdir()
    fake = d / "root" / "scripts" / "install.sh"
    fake.write_text('#!/bin/bash\ntouch "$SCOUT_HOME/install-ran"\n')
    fake.chmod(0o755)
    stub = d / "bin" / "launchctl"  # the Scout app is loaded; the old claude-voice one isn't
    stub.write_text('#!/bin/bash\ncase "$2" in */com.local.scout) exit 0 ;; *) exit 1 ;; esac\n')
    stub.chmod(0o755)
    yield d
    shutil.rmtree(d, ignore_errors=True)


def ensure_running(box: Path) -> bool:
    """Run the SessionStart hook; True if it started an install."""
    env = {k: v for k, v in os.environ.items() if k != "SCOUT_ROOM"}
    env.update(
        CLAUDE_PLUGIN_ROOT=str(box / "root"),
        SCOUT_HOME=str(box / "data"),
        PATH=f"{box / 'bin'}:{env['PATH']}",
    )
    out = subprocess.run(  # noqa: S603  our own script
        [BASH, str(REPO / "scripts" / "ensure-running.sh")],
        env=env,
        check=True,
        timeout=10,
        capture_output=True,
    )
    if b"setting up" not in out.stdout:
        return False
    ran = box / "data" / "install-ran"
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not ran.exists():
        time.sleep(0.05)
    assert ran.exists()
    return True


def installed(box: Path, status: str) -> None:
    root = (box / "root").resolve()
    (box / "data" / "current").symlink_to(root)
    (box / "data" / "state" / "install.json").write_text(
        f'{{"status": "{status}", "message": "x", "code": "{root}", "at": "2026-10-12T00:00:00Z"}}\n'
    )


# --- #41 ----------------------------------------------------------------------------


def test_a_failure_inside_a_function_reports_the_error(box: Path) -> None:
    set_line = re.search(r"^set -\S+ pipefail.*$", INSTALL, re.M)
    trap_line = re.search(r"^trap .* ERR$", INSTALL, re.M)
    assert set_line and trap_line
    script = "\n".join([
        set_line.group(0),
        'status() { echo "status $1: $2"; }',
        trap_line.group(0),
        "build_if_changed() { false; }  # swiftc failed",
        "build_if_changed",
    ])  # fmt: skip
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=10)  # noqa: S603
    assert out.returncode != 0
    assert "status error: setup failed" in out.stdout


def test_an_install_that_failed_part_way_is_run_again(box: Path) -> None:
    installed(box, "error")  # current already points here and the old app is still loaded
    assert ensure_running(box) is True


def test_a_finished_install_is_left_alone(box: Path) -> None:
    installed(box, "ready")
    assert ensure_running(box) is False


# --- #42 ----------------------------------------------------------------------------


def gone_pid() -> int:
    p = subprocess.Popen(["/usr/bin/true"])
    p.wait()
    return p.pid


def test_a_stale_lock_does_not_block_setup(box: Path) -> None:
    lock = box / "data" / ".install.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{gone_pid()}\n")
    assert ensure_running(box) is True


def test_a_live_install_is_not_started_twice(box: Path) -> None:
    lock = box / "data" / ".install.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")
    assert ensure_running(box) is False


def take_lock(box: Path) -> tuple[int, str]:
    fn = re.search(r"^take_lock\(\) \{\n.*?^\}\n", INSTALL, re.M | re.S)
    assert fn, "install.sh has no take_lock function"
    lock = box / "data" / ".install.lock"
    script = f'LOCK="{lock}"\n{fn.group(0)}take_lock; rc=$?; cat "$LOCK/pid" 2>/dev/null; exit $rc'
    out = subprocess.run([BASH, "-c", script], capture_output=True, text=True, timeout=10)  # noqa: S603
    return out.returncode, out.stdout


@pytest.mark.parametrize("pid", ["gone", "live", "none"])
def test_install_takes_over_only_a_stale_lock(box: Path, pid: str) -> None:
    lock = box / "data" / ".install.lock"
    lock.mkdir()
    if pid != "none":
        (lock / "pid").write_text(f"{gone_pid() if pid == 'gone' else os.getpid()}\n")
    rc, out = take_lock(box)
    if pid == "gone":
        assert rc == 0 and "clearing a stale install lock" in out
        assert (lock / "pid").read_text().strip().isdigit()
    else:  # a running installer, or one that took the lock a moment ago
        assert rc == 1
