"""quiet_hours is checked when the config loads (a bad one once stopped the Starlink watcher)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from scout import config


@pytest.mark.parametrize("bad", ["10pm-8am", "22:00", "22:00-8", "25:00-08:00", "22:00-08:00-09:00"])
def test_bad_quiet_hours_fall_back_to_the_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, bad: str
) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[pulse]\nquiet_hours = "{bad}"\n[starlink]\nquiet_hours = "{bad}"\n')
    with caplog.at_level(logging.WARNING, logger="scout.config"):
        cfg = config.load(path)
    assert cfg.pulse.quiet_hours == "22:00-08:00"
    assert cfg.starlink.quiet_hours == "22:00-08:00"
    assert "pulse.quiet_hours" in caplog.text and "starlink.quiet_hours" in caplog.text


@pytest.mark.parametrize("good", ["23:30-07:00", "", "09:00 - 17:00"])
def test_good_quiet_hours_are_kept(tmp_path: Path, caplog: pytest.LogCaptureFixture, good: str) -> None:
    path = tmp_path / "config.toml"
    path.write_text(f'[pulse]\nquiet_hours = "{good}"\n[starlink]\nquiet_hours = "{good}"\n')
    with caplog.at_level(logging.WARNING, logger="scout.config"):
        cfg = config.load(path)
    assert cfg.pulse.quiet_hours == good and cfg.starlink.quiet_hours == good
    assert "quiet_hours" not in caplog.text
