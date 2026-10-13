"""Config sections load from config.toml."""

from __future__ import annotations

from pathlib import Path

from scout.config import load


def test_phone_section_loads(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        '[phone]\nenabled = false\nspace = "x.signalwire.com"\nproject_id = "p"\napi_token = ""\nsigning_key = ""\n'
    )
    cfg = load(p)
    assert cfg.phone.space == "x.signalwire.com" and cfg.phone.enabled is False
