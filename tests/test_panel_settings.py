"""Tests for the touch panel section of a module's params blob."""

from __future__ import annotations

from ampio_mqtt import ModuleFunction, PanelLightSignal, PanelSettings
from ampio_mqtt._protocol import parse_panel_settings, resolve_panel_settings

# Both blobs are what the live baseline install returns, verbatim. The
# trailing zeros are the power-on defaults for outputs and flags, which
# sit after the panel section and are not read here.
FOUR_FIELD = bytes.fromhex("000000FFFF0A0A0101010102FFFF03000A32" + "00" * 35)
EIGHTEEN_FIELD = bytes.fromhex(
    "000000FFFF0A0A" + "01" * 18 + "02FFFFFFFFFFFF000000000A32" + "00" * 35
)


def test_a_four_field_panel_decodes() -> None:
    settings = parse_panel_settings(FOUR_FIELD, fields=4)
    assert settings == PanelSettings(
        touch_field_color=(0, 0, 0, 255),
        status_color=(255, 10, 10),
        light_signal=(1, 1, 1, 1),
        beep_time=2,
        sound_signal=(True, True, True, True),
        backlight_active=(True, True, True, True),
        # The operator assigned the touch lock to fields 1 and 2 in Designer.
        multitouch_lock=(True, True, False, False),
        multitouch_send_count=False,
        dim_after_s=10,
        dim_brightness=50,
    )
    assert settings.light_signal[0] == PanelLightSignal.CHANGE_STATE


def test_an_eighteen_field_panel_decodes() -> None:
    settings = parse_panel_settings(EIGHTEEN_FIELD, fields=18)
    assert settings is not None
    assert settings.touch_field_color == (0, 0, 0, 255)
    assert settings.status_color == (255, 10, 10)
    assert settings.light_signal == (1,) * 18
    assert settings.beep_time == 2
    # Three mask bytes for 18 fields, and every per-field tuple is N long.
    assert settings.sound_signal == (True,) * 18
    assert settings.backlight_active == (True,) * 18
    assert settings.multitouch_lock == (False,) * 18
    assert settings.dim_after_s == 10
    assert settings.dim_brightness == 50


def test_mask_bits_read_least_significant_first() -> None:
    blob = bytearray(EIGHTEEN_FIELD)
    blob[29:32] = bytes([0b00000101, 0x00, 0b00000010])  # fields 1, 3 and 18
    settings = parse_panel_settings(bytes(blob), fields=18)
    assert settings is not None
    lit = [i + 1 for i, on in enumerate(settings.backlight_active) if on]
    assert lit == [1, 3, 18]


def test_a_blob_too_short_for_the_section_reads_none() -> None:
    """A truncated blob must not decode into confident wrong values."""
    assert parse_panel_settings(EIGHTEEN_FIELD[:37], fields=18) is None
    assert parse_panel_settings(b"", fields=4) is None


def test_resolve_needs_a_proven_layout_and_a_field_count() -> None:
    params = {1: FOUR_FIELD, 2: FOUR_FIELD, 3: FOUR_FIELD}
    caps = {
        1: {ModuleFunction.BACKLIGHT_RGBW: 4},
        2: {ModuleFunction.BACKLIGHT_RGBW: 4},
        3: {},  # advertises no backlight, so no field count
    }
    hardware = {1: (8, 4), 2: (9, 4), 3: (8, 4)}  # (9, 4) is an unproven board
    resolved = resolve_panel_settings(params, caps, hardware, frozenset())
    assert list(resolved) == [1]
    assert resolved[1].dim_after_s == 10


def test_resolve_skips_colliding_macs() -> None:
    params = {1: FOUR_FIELD}
    caps = {1: {ModuleFunction.BACKLIGHT_RGBW: 4}}
    assert resolve_panel_settings(params, caps, {1: (8, 4)}, frozenset({1})) == {}
