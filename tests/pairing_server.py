"""Scout's web server (web.start) in a process of its own, with the stand-in assistant
from test_web.py and a fake tailscaled: what test_pairing_integration.py runs against.
Never the live app: its own SCOUT_HOME (set by the caller), its own port, no Bonjour
advertisement and no alert on the Mac.

    SCOUT_HOME=<dir> python tests/pairing_server.py <port>
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from test_web import ADDRESS, StubApp, fake_tailscale

from scout import tailnet, web
from scout.config import PhoneConfig, WebConfig


async def main(port: int) -> None:
    tailnet._default = fake_tailscale()
    web.MAC_ALERT = ""
    web.DNS_SD = "/nonexistent/dns-sd"  # never advertise next to the live app
    app = StubApp()
    app.pair_answer = None  # the room never answers; another channel must
    phone = PhoneConfig(enabled=True, address=ADDRESS)
    runner = await web.start(WebConfig(hosts=["127.0.0.1"], port=port), app, phone)
    print("READY", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))
