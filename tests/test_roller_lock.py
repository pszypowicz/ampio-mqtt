"""Which covers can take a roller lock write, and how wide the mask is."""

from ampio_mqtt._protocol import roller_lock_channels
from ampio_mqtt.models import ModuleFunction

ROLLER = int(ModuleFunction.ROLLER)
OUT_BIN = int(ModuleFunction.OUT_BIN)
IN_BIN = int(ModuleFunction.IN_BIN)

# A module that answers the lock: it advertises four roller channels.
ANSWERS = {OUT_BIN: 8, ROLLER: 4, IN_BIN: 8}
# A module that drops the lock: it answered the sweep and advertises no
# roller count.
DROPS = {OUT_BIN: 4, IN_BIN: 8}


def test_an_advertised_count_covering_the_channel_sizes_the_mask() -> None:
    assert roller_lock_channels(ANSWERS, 3) == 4


def test_no_advertised_count_sizes_no_mask() -> None:
    assert roller_lock_channels(DROPS, 0) is None


def test_a_channel_past_the_advertised_count_sizes_no_mask() -> None:
    assert roller_lock_channels(ANSWERS, 4) is None


def test_a_zero_count_sizes_no_mask() -> None:
    assert roller_lock_channels({ROLLER: 0}, 0) is None
