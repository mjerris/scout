"""A stand-in for native/voiceio that speaks the same stdio protocol, for
tests/test_audio_apple.py. No audio devices are touched.

    python fake_voiceio.py [mode]

Modes: normal (default), crash (dies after a few mic chunks), fail (reports
`failed` instead of `ready`), silent (never becomes ready), hang (ignores quit
and closed stdin, so the caller must kill it), device-change (reports a
device change right after ready), fail-later (reports `failed` after ready,
then exits).
"""

from __future__ import annotations

import json
import os
import struct
import sys
import threading
import time
from typing import Any

HEADER = struct.Struct("<cI")
MIC_CHUNK = 1000  # bytes; deliberately not a multiple of the 960-byte frame

mode = sys.argv[1] if len(sys.argv) > 1 else "normal"
out = sys.stdout.buffer
out_lock = threading.Lock()
state_lock = threading.Lock()
pending: dict[int, int] = {}  # play id -> generation it was queued in
generation = 0
playhead = 0.0


def send(kind: bytes, payload: bytes = b"") -> None:
    with out_lock:
        out.write(HEADER.pack(kind, len(payload)) + payload)
        out.flush()


def event(name: str, **fields: Any) -> None:
    send(b"E", json.dumps({"event": name, **fields}).encode())


def done(play_id: int, played: bool) -> None:
    send(b"D", struct.pack("<IB", play_id, 1 if played else 0))


def read_exactly(n: int) -> bytes | None:
    data = b""
    while len(data) < n:
        chunk = sys.stdin.buffer.read(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def mic_loop() -> None:
    counter = 0
    sent = 0
    while True:
        samples = [(counter + i) % 30000 for i in range(MIC_CHUNK // 2)]
        counter += MIC_CHUNK // 2
        send(b"M", struct.pack(f"<{len(samples)}h", *samples))
        sent += 1
        if mode == "crash" and sent == 5:
            sys.stderr.write("fake voiceio: boom\n")
            sys.stderr.flush()
            os._exit(3)
        time.sleep(0.01)


def finish_later(play_id: int, gen: int, at: float) -> None:
    time.sleep(max(0.0, at - time.monotonic()))
    with state_lock:
        if pending.get(play_id) != gen:
            return
        del pending[play_id]
    done(play_id, True)


def main() -> None:
    global generation, playhead
    while True:
        header = read_exactly(HEADER.size)
        if header is None:
            break
        kind, length = HEADER.unpack(header)
        payload = read_exactly(length) if length else b""
        if payload is None:
            break
        if kind == b"C":
            config = json.loads(payload)
            if mode == "fail":
                event("failed", error="fake failure")
                sys.exit(1)
            if mode == "silent":
                continue
            event("ready", input_device="Fake Mic", output_device="Fake Speaker", config=config)
            event("stats", mic_chunks=0)
            if mode == "device-change":
                event("device_changed", input_device="Other Mic", output_device="Fake Speaker")
            if mode == "fail-later":
                time.sleep(0.1)
                event("failed", error="device gone for good")
                sys.exit(1)
            threading.Thread(target=mic_loop, daemon=True).start()
        elif kind == b"P":
            play_id, rate = struct.unpack_from("<II", payload)
            seconds = (len(payload) - 8) / 4 / rate
            with state_lock:
                pending[play_id] = generation
                playhead = max(playhead, time.monotonic()) + seconds
                at = playhead
            threading.Thread(target=finish_later, args=(play_id, generation, at), daemon=True).start()
        elif kind == b"S":
            with state_lock:
                stopped = sorted(pending)
                pending.clear()
                generation += 1
                playhead = time.monotonic()
            for play_id in stopped:
                done(play_id, False)
        elif kind == b"Q":
            if mode != "hang":
                return
    if mode == "hang":
        while True:
            time.sleep(1)


if __name__ == "__main__":
    main()
