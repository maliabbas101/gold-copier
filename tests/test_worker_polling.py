import pytest

from goldcopier.platform_agent import worker_poll_interval


def test_default_poll_interval(monkeypatch):
    monkeypatch.delenv("WORKER_POLL_INTERVAL", raising=False)
    assert worker_poll_interval() == 0.2


@pytest.mark.parametrize("value", ["0", "-1", "0.01", "6", "nan", "inf", "bad"])
def test_invalid_poll_intervals_fail_before_worker_start(monkeypatch, value):
    monkeypatch.setenv("WORKER_POLL_INTERVAL", value)
    with pytest.raises(ValueError):
        worker_poll_interval()


@pytest.mark.parametrize("value", ["0.1", "0.5", "5"])
def test_supported_poll_intervals(monkeypatch, value):
    monkeypatch.setenv("WORKER_POLL_INTERVAL", value)
    assert worker_poll_interval() == float(value)
