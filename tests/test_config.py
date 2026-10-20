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


def test_helpers_sign_identity_loads_and_scripts_read_it(tmp_path: Path) -> None:
    """[helpers] sign_identity is a config key the app accepts, and scripts/sign.sh signs
    with it: SCOUT_SIGN_IDENTITY first, then the file, else "-" (ad hoc)."""
    import os
    import subprocess
    import sys

    p = tmp_path / "config.toml"
    p.write_text('[helpers]\nsign_identity = "Apple Development: Someone (ABCDE12345)"\n')
    assert load(p).helpers.sign_identity == "Apple Development: Someone (ABCDE12345)"
    sign = Path(__file__).resolve().parents[1] / "scripts" / "sign.sh"

    def resolve(home: Path, **env: str) -> str:
        base = {k: v for k, v in os.environ.items() if k != "SCOUT_SIGN_IDENTITY"}
        base["PATH"] = f"{Path(sys.executable).parent}{os.pathsep}{base.get('PATH', '')}"  # a tomllib python3
        r = subprocess.run(  # noqa: S603  our own script
            ["/bin/bash", "-c", f'. "{sign}"; scout_sign_identity'],
            env={**base, "SCOUT_HOME": str(home), **env},
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout.strip()

    assert resolve(tmp_path) == "Apple Development: Someone (ABCDE12345)"
    assert resolve(tmp_path, SCOUT_SIGN_IDENTITY="ABC123") == "ABC123"
    assert resolve(tmp_path / "none") == "-"
