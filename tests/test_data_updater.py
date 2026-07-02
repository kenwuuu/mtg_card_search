import dataclasses
import json
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data_updater
from data_updater import DataSanityError


def test_acquire_lock_blocks_concurrent_run(tmp_path, monkeypatch):
    lock_path = tmp_path / ".data_updater.lock"
    monkeypatch.setattr(data_updater, "LOCK_PATH", lock_path)

    holder_script = textwrap.dedent(f"""
        import fcntl, time
        f = open({str(lock_path)!r}, "w")
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        print("locked", flush=True)
        time.sleep(2)
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", holder_script],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout.readline().strip() == "locked"

    blocked = data_updater.acquire_lock()
    assert blocked is None

    proc.wait(timeout=5)

    free = data_updater.acquire_lock()
    assert free is not None
    free.close()


def _write_fixture(folder: Path, name: str, count: int) -> None:
    items = [{"name": f"Card {i}"} for i in range(count)]
    (folder / f"{name}.json").write_text(json.dumps(items))


def _isolate(tmp_path, monkeypatch, dataset_name="fakeset"):
    monkeypatch.setattr(data_updater, "FOLDER", str(tmp_path))
    monkeypatch.setattr(data_updater, "BULK_DATA_TYPES", [dataset_name])
    monkeypatch.setattr(data_updater, "COUNTS_PATH", tmp_path / ".dataset_counts.json")
    return dataset_name


def test_convert_json_to_ndjson_rejects_sharp_drop(tmp_path, monkeypatch):
    dataset_name = _isolate(tmp_path, monkeypatch)

    _write_fixture(tmp_path, dataset_name, 10)
    baseline_counts = data_updater.convert_json_to_ndjson()
    assert baseline_counts[dataset_name] == 10
    ndjson_before = (tmp_path / f"{dataset_name}.ndjson").read_text()
    counts_before = json.loads((tmp_path / ".dataset_counts.json").read_text())

    _write_fixture(tmp_path, dataset_name, 1)
    try:
        data_updater.convert_json_to_ndjson()
        assert False, "expected DataSanityError"
    except DataSanityError:
        pass

    assert (tmp_path / f"{dataset_name}.ndjson").read_text() == ndjson_before
    assert json.loads((tmp_path / ".dataset_counts.json").read_text()) == counts_before
    assert not (tmp_path / f"{dataset_name}.ndjson_new").exists()


def test_convert_json_to_ndjson_accepts_growth(tmp_path, monkeypatch):
    dataset_name = _isolate(tmp_path, monkeypatch)

    _write_fixture(tmp_path, dataset_name, 10)
    first_counts = data_updater.convert_json_to_ndjson()
    assert first_counts[dataset_name] == 10

    _write_fixture(tmp_path, dataset_name, 12)
    second_counts = data_updater.convert_json_to_ndjson()
    assert second_counts[dataset_name] == 12
    assert json.loads((tmp_path / ".dataset_counts.json").read_text())[dataset_name] == 12
    assert len((tmp_path / f"{dataset_name}.ndjson").read_text().splitlines()) == 12


def _patched_settings(monkeypatch, **overrides):
    new_settings = dataclasses.replace(data_updater.settings, **overrides)
    monkeypatch.setattr(data_updater, "settings", new_settings)
    return new_settings


def test_ping_healthcheck_sequence(monkeypatch):
    _patched_settings(monkeypatch, healthcheck_ping_url="https://hc-ping.com/abc123")

    calls = []
    monkeypatch.setattr(
        data_updater.requests, "get",
        lambda url, timeout=None: calls.append(url) or None,
    )

    data_updater.ping_healthcheck("/start")
    data_updater.ping_healthcheck()
    data_updater.ping_healthcheck("/fail")

    assert calls == [
        "https://hc-ping.com/abc123/start",
        "https://hc-ping.com/abc123",
        "https://hc-ping.com/abc123/fail",
    ]


def test_ping_healthcheck_noop_without_url(monkeypatch):
    _patched_settings(monkeypatch, healthcheck_ping_url=None)

    calls = []
    monkeypatch.setattr(
        data_updater.requests, "get",
        lambda url, timeout=None: calls.append(url) or None,
    )

    data_updater.ping_healthcheck()
    assert calls == []


def test_init_posthog_noop_without_key(monkeypatch):
    _patched_settings(monkeypatch, posthog_api_key=None)
    assert data_updater.init_posthog() is False


def test_notify_posthog_noop_when_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(data_updater.posthog, "capture", lambda **kw: calls.append(kw))

    data_updater.notify_posthog(False, "data_update_succeeded", {"counts": {}})
    assert calls == []


def test_init_sentry_noop_without_dsn(monkeypatch):
    _patched_settings(monkeypatch, sentry_dsn=None)
    assert data_updater.init_sentry() is None
