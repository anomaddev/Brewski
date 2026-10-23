import sqlite3
import time

import pytest

from brewski.store import Store
from brewski.timeline import CONDENSE_AFTER_MS, CONDENSED_MS, to_display, to_real, window_start


def test_a_short_gap_stays_and_a_long_one_is_recorded(store):
    start = 1_700_000_000_000
    store.add_timeline_point([70.0, 60.0], 165, start)
    store.add_timeline_point([71.0, 61.0], 165, start + 10_000)
    store.add_timeline_point([71.2, 61.0], 165, start + 10_000 + 30_000)
    jumped = start + 10_000 + 30_000 + 31 * 60 * 1000
    store.add_timeline_point([72.0, 62.0], 166, jumped)

    view = store.timeline(start, jumped, 100)
    assert view["decimated"] is False
    assert [point["temps_f"] for point in view["points"]] == [[70.0], [71.0], [71.2], [72.0]]
    assert view["points"][-1]["target_temp_f"] == 166
    assert view["earliest"] == start
    assert view["latest"] == jumped
    assert view["gaps"] == [{"from": start + 40_000, "to": jumped}]


def test_a_dense_range_is_thinned_without_losing_its_ends_or_a_hole(store):
    start = 1_700_000_000_000
    for index in range(80):
        store.add_timeline_point([70, None], 165, start + index * 1000)
    edge = start + 79_000
    resume = edge + 31 * 60 * 1000
    for index in range(80):
        store.add_timeline_point([80, None], 165, resume + index * 1000)

    view = store.timeline(start, resume + 79_000, 10)
    assert view["decimated"] is True
    assert len(view["points"]) < 80
    stamps = [point["at"] for point in view["points"]]
    assert stamps[0] == start
    assert stamps[-1] == resume + 79_000
    assert edge in stamps
    assert resume in stamps


def test_old_readings_are_thinned_and_a_recent_one_stays(store):
    now = 1_800_000_000_000
    day = 24 * 60 * 60 * 1000
    base = now - 3 * day
    for index in range(120):
        store.add_timeline_point([70, None], 165, base + index * 5000)
    store.add_timeline_point([90, None], 165, now - 1000)

    store.compact_timeline(now)
    old = store.timeline(base, base + 119 * 5000, 5000)
    assert 8 <= len(old["points"]) <= 20
    recent = store.timeline(now - 10_000, now, 100)
    assert recent["points"][-1]["at"] == now - 1000
    assert recent["points"][-1]["temps_f"] == [90.0]


def test_samples_are_copied_onto_an_empty_timeline_once(tmp_path):
    path = tmp_path / "brewski.db"
    store = Store(path)
    run = store.create_run(store.get_settings())
    store.add_sample(run["id"], 0, [70, None], [False, False], 165)
    store.add_sample(run["id"], 5, [80, None], [True, False], 165)
    with sqlite3.connect(path) as connection:
        ids = [row[0] for row in connection.execute("SELECT id FROM samples ORDER BY id")]
        connection.execute(
            "UPDATE samples SET recorded_at = ? WHERE id = ?",
            ("2024-01-01T00:00:00+00:00", ids[0]),
        )
        connection.execute(
            "UPDATE samples SET recorded_at = ? WHERE id = ?",
            ("2024-01-01T03:00:00+00:00", ids[1]),
        )

    reopened = Store(path)
    view = reopened.timeline(0, 2_000_000_000_000, 100)
    assert [point["temps_f"] for point in view["points"]] == [[70.0], [80.0]]
    assert len(view["gaps"]) == 1
    assert len(Store(path).timeline(0, 2_000_000_000_000, 100)["points"]) == 2


def test_reset_clears_the_timeline(store):
    store.add_timeline_point([70, None], 165, 1_700_000_000_000)
    store.add_timeline_point([71, None], 165, 1_700_000_000_000 + 2 * 60 * 60 * 1000)
    store.reset()
    view = store.timeline(0, 2_000_000_000_000, 100)
    assert view["points"] == []
    assert view["gaps"] == []
    assert view["earliest"] is None


def test_thirty_minutes_stays_on_the_clock_and_a_longer_outage_collapses():
    half_hour = (0, 30 * 60 * 1000)
    assert to_display(half_hour[1], [half_hour]) - to_display(0, [half_hour]) == half_hour[1]
    longer = (0, CONDENSE_AFTER_MS + 1)
    assert to_display(longer[1], [longer]) - to_display(0, [longer]) == pytest.approx(CONDENSED_MS)


def test_a_twenty_minute_window_reaches_through_a_long_outage():
    gap_from = 20 * 60 * 1000
    gap_to = gap_from + 2 * 60 * 60 * 1000
    end = gap_to + 5 * 60 * 1000
    gaps = [(gap_from, gap_to)]
    assert window_start(end, 20 * 60 * 1000, gaps, earliest=0) == gap_from - 13 * 60 * 1000
    moment = gap_from - 13 * 60 * 1000
    assert to_real(to_display(moment, gaps), gaps, 0, end) == moment


def test_idle_and_paused_time_stay_on_the_timeline(client, controller):
    now = int(time.time() * 1000) + 5000
    before = client.get("/api/timeline", query_string={"from": 0, "to": now, "max": 200}).get_json()
    assert before["points"]
    controller.tick(5)
    client.post("/api/runs/start")
    client.post("/api/runs/pause")
    run_id = client.get("/api/status").get_json()["run_id"]
    samples = len(client.get(f"/api/runs/{run_id}").get_json()["samples"])
    controller.tick(5)
    assert len(client.get(f"/api/runs/{run_id}").get_json()["samples"]) == samples
    after = client.get(
        "/api/timeline",
        query_string={"from": 0, "to": int(time.time() * 1000) + 5000, "max": 200},
    ).get_json()
    assert len(after["points"]) > len(before["points"])
    assert client.get("/api/timeline").status_code == 400
    assert client.get("/api/timeline", query_string={"from": "10", "to": "1"}).status_code == 400
    assert client.get("/api/timeline", query_string={"from": "nope", "to": "2"}).status_code == 400
