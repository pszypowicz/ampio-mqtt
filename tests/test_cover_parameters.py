"""Tests for the roller section of a module's params blob."""

from __future__ import annotations

from ampio_mqtt import CoverParameters
from ampio_mqtt._protocol import COVER_PARAMS_LAYOUTS, parse_cover_parameters

# A four-channel section, stride 10, at offset 5. Channel 4 carries travel
# times above 255 s so the two-byte order is provable. The last four bytes
# are the unlabeled range the Designer never reads.
FOUR_CHANNEL_SECTION = (
    "01000000"  # work mode: channel 1 drives slats, the rest are plain
    "34001E001B002C01"  # opening: 52, 30, 27, 300 s
    "34001E001B002201"  # closing: 52, 30, 27, 290 s
    "0A0A0A0A"  # calibration
    "9600640064006400"  # slat movement: 150, 100, 100, 100 ticks
    "32323232"  # reversal lag: 50 ticks
    "1E140000"  # unlabeled
)
FOUR_CHANNEL = bytes.fromhex("00" * 5 + FOUR_CHANNEL_SECTION + "00" * 83)

# A one-channel section, stride 12, at offset 33. The two start lags exist
# on this board.
ONE_CHANNEL_SECTION = (
    "00"  # work mode: plain
    "2800"  # opening: 40 s
    "2800"  # closing: 40 s
    "0A"  # calibration
    "6400"  # slat movement: 100 ticks
    "32"  # reversal lag: 50 ticks
    "00"  # unlabeled
    "14"  # start lag, same direction: 20 ticks
    "0C"  # start lag, other direction: 12 ticks
)
ONE_CHANNEL = bytes.fromhex("00" * 33 + ONE_CHANNEL_SECTION)

FOUR_CHANNEL_LAYOUT = COVER_PARAMS_LAYOUTS[(3, 8)]
ONE_CHANNEL_LAYOUT = COVER_PARAMS_LAYOUTS[(24, 11)]


def test_a_four_channel_section_decodes() -> None:
    channels = parse_cover_parameters(FOUR_CHANNEL, FOUR_CHANNEL_LAYOUT)
    assert channels is not None
    assert len(channels) == 4
    assert channels[0] == CoverParameters(
        with_slats=True,
        open_time_s=52,
        close_time_s=52,
        calibration=10,
        slat_time_ms=1500,
        reversal_lag_ms=500,
        start_lag_same_ms=None,
        start_lag_other_ms=None,
    )
    assert channels[1].with_slats is False
    assert channels[1].open_time_s == 30
    assert channels[2].close_time_s == 27
    assert channels[3].slat_time_ms == 1000


def test_a_one_channel_section_decodes_the_start_lags() -> None:
    channels = parse_cover_parameters(ONE_CHANNEL, ONE_CHANNEL_LAYOUT)
    assert channels == (
        CoverParameters(
            with_slats=False,
            open_time_s=40,
            close_time_s=40,
            calibration=10,
            slat_time_ms=1000,
            reversal_lag_ms=500,
            start_lag_same_ms=200,
            start_lag_other_ms=120,
        ),
    )


def test_two_byte_fields_read_least_significant_byte_first() -> None:
    channels = parse_cover_parameters(FOUR_CHANNEL, FOUR_CHANNEL_LAYOUT)
    assert channels is not None
    assert channels[3].open_time_s == 300
    assert channels[3].close_time_s == 290


def test_a_stride_of_ten_holds_no_start_lags() -> None:
    """The section ends before them, so neither reaches past it."""
    channels = parse_cover_parameters(FOUR_CHANNEL, FOUR_CHANNEL_LAYOUT)
    assert channels is not None
    assert [c.start_lag_same_ms for c in channels] == [None] * 4
    assert [c.start_lag_other_ms for c in channels] == [None] * 4


def test_a_blob_too_short_for_the_section_reads_none() -> None:
    """A truncated blob must not decode into confident wrong values."""
    assert parse_cover_parameters(FOUR_CHANNEL[:44], FOUR_CHANNEL_LAYOUT) is None
    assert parse_cover_parameters(b"", ONE_CHANNEL_LAYOUT) is None


def test_only_proven_boards_carry_a_layout() -> None:
    assert set(COVER_PARAMS_LAYOUTS) == {(3, 8), (24, 11)}
