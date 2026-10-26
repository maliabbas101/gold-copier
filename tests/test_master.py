import pytest
from goldcopier.master_bridge import changes
from goldcopier.signals_v2 import parse_signal


def test_partial_master_change_uses_remaining_position_percentage():
    before = {"reference": "master-123-entry", "volume": 0.1, "sl": 2695, "tp": 2720}
    after = {**before, "volume": 0.05, "sl": 2700}
    events = changes(before, after)
    assert [e[0] for e in events] == ["modify", "partial"]
    assert parse_signal(events[1][1])["percent"] == 50
    assert parse_signal(changes(before, None)[0][1])["action"] == "close"


def test_scale_in_is_never_guessed():
    before = {"reference": "master-123-entry", "volume": 0.1, "sl": 2695, "tp": 2720}
    with pytest.raises(ValueError):
        changes(before, {**before, "volume": 0.2})
