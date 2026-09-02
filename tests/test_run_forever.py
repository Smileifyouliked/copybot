"""The screen wrapper's restart policy.

Under systemd, `Restart=always` is right because systemd has its own backoff
and its own visibility. Under `screen` this script is the only thing that
restarts a crashed bot, and it has neither -- so it has to tell a crash apart
from a refusal. Restarting a refusal produces a 10-second loop that buries the
one-line message explaining what to fix.

These run the real script against a stub interpreter that exits with a chosen
code, so the policy is tested rather than read.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run-forever.sh"


@pytest.fixture
def fake_install(tmp_path):
    """A directory shaped like the project, with a scriptable 'python'."""
    (tmp_path / ".venv" / "bin").mkdir(parents=True)
    (tmp_path / "logs").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "config.yaml").write_text("mode: paper\n")
    (tmp_path / "scripts" / "run-forever.sh").write_text(SCRIPT.read_text())
    os.chmod(tmp_path / "scripts" / "run-forever.sh", 0o755)
    return tmp_path


def stub_python(root: Path, exit_code: int) -> None:
    """A 'python' that records each invocation and exits with `exit_code`."""
    path = root / ".venv" / "bin" / "python"
    path.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "run" >> "{root}/calls.log"\n'
        f"exit {exit_code}\n"
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def run(root: Path, timeout=8):
    return subprocess.run(
        [str(root / "scripts" / "run-forever.sh")],
        capture_output=True, text=True, timeout=timeout, cwd=str(root),
    )


def calls(root: Path) -> int:
    log = root / "calls.log"
    return len(log.read_text().splitlines()) if log.exists() else 0


# --- exits that must NOT be restarted --------------------------------------

def test_a_clean_shutdown_is_not_restarted(fake_install):
    stub_python(fake_install, 0)
    result = run(fake_install)
    assert result.returncode == 0
    assert calls(fake_install) == 1
    assert "Not restarting" in result.stdout


def test_a_rejected_config_is_not_restarted(fake_install):
    """A bad config does not heal in ten seconds. Looping on it buries the
    message that says which setting is wrong."""
    stub_python(fake_install, 2)
    result = run(fake_install)
    assert result.returncode == 2
    assert calls(fake_install) == 1, "must not retry a config error"
    assert "Fix config.yaml" in result.stderr


def test_a_held_lock_is_not_restarted(fake_install):
    """Another bot holds the database. Waiting cannot win a lock someone else
    is holding, and the whole point of the lock is to stop a second writer --
    so retrying it forever would defeat the thing it is protecting."""
    stub_python(fake_install, 3)
    result = run(fake_install)
    assert result.returncode == 3
    assert calls(fake_install) == 1, "must not retry against a held lock"
    assert "already running" in result.stderr
    assert (fake_install / "logs" / "restarts.log").exists() is False, \
        "a refusal is not a crash and must not be logged as one"


# --- exits that MUST be restarted ------------------------------------------

def test_a_crash_is_restarted_and_logged(fake_install):
    """An unexpected exit is what this script exists for."""
    stub_python(fake_install, 1)
    with pytest.raises(subprocess.TimeoutExpired):
        # It restarts forever by design, so it has to be killed to be observed.
        run(fake_install, timeout=3)
    assert calls(fake_install) >= 1
    log = fake_install / "logs" / "restarts.log"
    assert log.exists() and "restart #1 (exit 1)" in log.read_text()


# --- refusing to start at all ----------------------------------------------

def test_a_missing_virtualenv_says_so_instead_of_looping(fake_install):
    result = run(fake_install)
    assert result.returncode == 1
    assert "no virtualenv" in result.stderr
    assert calls(fake_install) == 0
