"""Review fixes in microphone handling: the first switch away is announced (and the
apple helper's key is read), the apple backend gets one device name, and a numeric
device spec is checked before it is used."""

from typing import Any

import pytest

from scout import _input_name, _mic_switch, device_name, microphone_present
from scout.audio_plain import resolve_device

DEVICES = [
    {"name": "MacBook Pro Microphone", "max_input_channels": 1, "max_output_channels": 0},
    {"name": "MacBook Pro Speakers", "max_input_channels": 0, "max_output_channels": 2},
    {"name": "OBSBOT Tiny 3 Microphone", "max_input_channels": 2, "max_output_channels": 0},
]


class _IO:
    def __init__(self, stats: dict[str, Any], ready: dict[str, Any] | None = None) -> None:
        self._stats = stats
        if ready is not None:
            self.ready_info = ready

    def stats(self) -> dict[str, Any]:
        return self._stats


def test_losing_the_starting_mic_is_announced() -> None:
    mic = {"name": _input_name(_IO({"input": "OBSBOT Tiny 3 Microphone"}))}
    said = _mic_switch(mic, {"input": "MacBook Pro Microphone"}, "OBSBOT")
    assert said == "I lost the OBSBOT Tiny 3 microphone, so I'm listening on the MacBook Pro one."


def test_apple_backend_switches_are_announced() -> None:
    io = _IO({"devices": {}}, ready={"input_device": "OBSBOT Tiny 3 Microphone"})
    mic = {"name": _input_name(io)}
    said = _mic_switch(mic, {"input_device": "MacBook Pro Microphone", "reason": "devices"}, "OBSBOT")
    assert said is not None and said.startswith("I lost the OBSBOT")
    assert _mic_switch(mic, {"input_device": "MacBook Pro Microphone"}, "OBSBOT") is None  # no change


@pytest.mark.parametrize(
    ("spec", "kind", "name"),
    [
        ("OBSBOT|any", "input", "OBSBOT Tiny 3 Microphone"),
        ("|any", "input", "MacBook Pro Microphone"),  # the default
        ("Rode|any", "input", "MacBook Pro Microphone"),
        ("Rode", "input", "Rode"),  # one name passes through unchanged
        ("|any", "output", "MacBook Pro Speakers"),
    ],
)
def test_apple_backend_gets_one_device_name(spec: str, kind: str, name: str) -> None:
    default = 0 if kind == "input" else 1
    assert device_name(spec, kind, DEVICES, default) == name


def test_apple_backend_without_a_match_uses_the_system_default() -> None:
    assert device_name("Rode|Shure", "input", DEVICES, 0) == ""


@pytest.mark.parametrize("spec", ["3|any", "1|any"])  # past the end; an output-only device
def test_a_stale_device_index_falls_back(spec: str) -> None:
    assert resolve_device(spec, "input", DEVICES, 0) == 0


def test_a_stale_device_index_does_not_crash_startup() -> None:
    assert microphone_present("3", DEVICES, 0) is False
    assert microphone_present("3|any", DEVICES, 0) is True
    assert microphone_present("2", DEVICES, 0) is True
