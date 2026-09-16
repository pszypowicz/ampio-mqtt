"""Which covers can take a roller lock write, and how wide the mask is."""

from ampio_mqtt._protocol import resolve_roller_lock_support, roller_lock_channels
from ampio_mqtt.models import AmpioObject, ModuleAddress, ModuleFunction

ROLLER = int(ModuleFunction.ROLLER)
OUT_BIN = int(ModuleFunction.OUT_BIN)
IN_BIN = int(ModuleFunction.IN_BIN)

# A module that answers the lock: it advertises four roller channels.
ANSWERS = {OUT_BIN: 8, ROLLER: 4, IN_BIN: 8}
# A module that drops the lock: it answered the sweep and advertises no
# roller count.
DROPS = {OUT_BIN: 4, IN_BIN: 8}


def _object(**over: object) -> AmpioObject:
    """An object carrying the catalogue columns every row serves."""
    row: dict[str, object] = {
        "id": 1,
        "typ_komponentu": "roleta_procenty",
        "interpretacja": 0,
        "funkcja": 1,
        "address": ModuleAddress(mac=0xCB89, channel=0, sf_id=5, sub_sf_id=0),
        "leaf_key": "leaf_0_cb89_5_0_0",
    }
    return AmpioObject(**{**row, **over})  # type: ignore[arg-type]


def test_an_advertised_count_covering_the_channel_sizes_the_mask() -> None:
    assert roller_lock_channels(ANSWERS, 3) == 4


def test_no_advertised_count_sizes_no_mask() -> None:
    assert roller_lock_channels(DROPS, 0) is None


def test_a_channel_past_the_advertised_count_sizes_no_mask() -> None:
    assert roller_lock_channels(ANSWERS, 4) is None


def test_a_zero_count_sizes_no_mask() -> None:
    assert roller_lock_channels({ROLLER: 0}, 0) is None


def test_a_cover_on_an_advertising_module_takes_the_lock() -> None:
    objects = {10: _object(id=10)}
    assert resolve_roller_lock_support(objects, {0xCB89: ANSWERS}) == {10: True}


def test_a_cover_on_a_module_that_advertises_no_count_does_not() -> None:
    objects = {10: _object(id=10)}
    assert resolve_roller_lock_support(objects, {0xCB89: DROPS}) == {10: False}


def test_a_channel_past_the_advertised_count_does_not() -> None:
    objects = {
        10: _object(
            id=10, address=ModuleAddress(mac=0xCB89, channel=9, sf_id=5, sub_sf_id=0)
        )
    }
    assert resolve_roller_lock_support(objects, {0xCB89: ANSWERS}) == {10: False}


def test_a_module_the_sweep_left_out_resolves_nothing() -> None:
    objects = {
        10: _object(
            id=10, address=ModuleAddress(mac=0xDEAD, channel=0, sf_id=5, sub_sf_id=0)
        )
    }
    assert resolve_roller_lock_support(objects, {0xCB89: ANSWERS}) == {}


def test_a_module_that_answered_with_nothing_still_resolves() -> None:
    objects = {10: _object(id=10)}
    assert resolve_roller_lock_support(objects, {0xCB89: {}}) == {10: False}


def test_a_kind_outside_the_roller_class_resolves_nothing() -> None:
    objects = {
        1: _object(
            id=1,
            typ_komponentu="przekaznik",
            address=ModuleAddress(mac=0xCB89, channel=0, sf_id=257, sub_sf_id=2),
        )
    }
    assert resolve_roller_lock_support(objects, {0xCB89: ANSWERS}) == {}
