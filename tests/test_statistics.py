"""Tests for importing the meter's interval history as statistics."""

import asyncio
from collections.abc import Iterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock

from aio2030dot5 import (
    Channel,
    IntervalValue,
    MeterConnectionError,
    MeterProfile,
    ReadingSetInfo,
)
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components.recorder import Recorder, get_instance
from homeassistant.components.recorder.statistics import (
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.core import HomeAssistant
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.ieee_2030_5.const import DOMAIN

from . import MOCK_SFDI, SLOW_POLLS, poll, setup_integration

IMPORTED = f"{DOMAIN}:{MOCK_SFDI}_energy_imported"
EXPORTED = f"{DOMAIN}:{MOCK_SFDI}_energy_exported"
T0 = datetime(2026, 9, 1, tzinfo=UTC)
QUARTER = timedelta(minutes=15)


def reading_set(index: int, hours: int = 2) -> ReadingSetInfo:
    """A set of `hours` hours of intervals, starting `index * hours` after T0."""
    return ReadingSetInfo(
        href=f"/rs/{index}",
        description=None,
        start=T0 + timedelta(hours=index * hours),
        duration=hours * 3600,
        reading_list_href=f"/rs/{index}/r",
        reading_count=hours * 4,
    )


def intervals(
    reading: ReadingSetInfo, wh: float, *, unit: str = "Wh", skip: int | None = None
) -> list[IntervalValue]:
    """The set's 15-minute intervals of `wh` each (interval `skip` missing)."""
    assert reading.start is not None and reading.duration is not None
    count = reading.duration // 900
    return [
        IntervalValue(
            start=reading.start + i * QUARTER,
            end=reading.start + (i + 1) * QUARTER,
            value=wh,
            unit=unit,
        )
        for i in range(count)
        if i != skip
    ]


def serve(
    meter: AsyncMock, history: dict[str, list[tuple[ReadingSetInfo, list[Any]]]]
) -> None:
    """The meter's interval history per channel key: sets, newest first."""

    async def list_sets(channel: Channel) -> list[ReadingSetInfo]:
        return [s for s, _ in reversed(history[channel.key])]

    async def read_sets(
        channel: Channel, sets: Iterable[ReadingSetInfo], **kwargs: Any
    ) -> list[IntervalValue]:
        by_href = {s.href: values for s, values in history[channel.key]}
        return [v for s in sets for v in by_href[s.href]]

    meter.list_interval_sets.side_effect = list_sets
    meter.read_interval_sets.side_effect = read_sets


async def sums(hass: HomeAssistant, statistic_id: str) -> list[tuple[datetime, float]]:
    """(start, sum) of every hour imported."""
    await async_wait_recording_done(hass)
    rows = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        T0 - timedelta(days=1),
        None,
        {statistic_id},
        "hour",
        None,
        {"sum"},
    )
    return [
        (datetime.fromtimestamp(r["start"], UTC), r["sum"])
        for r in rows.get(statistic_id, [])
    ]


async def setup_and_import(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """Set up; the import runs in the background."""
    await setup_integration(hass, entry)
    await hass.async_block_till_done(wait_background_tasks=True)


@pytest.mark.usefixtures("recorder_mock")
async def test_the_stored_history_is_imported_once(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Every complete hour, both directions, read two sets at a time."""
    sets = [reading_set(i) for i in range(3)]
    serve(
        mock_meter,
        {
            "energy_interval_delivered": [
                (sets[0], intervals(sets[0], 100.0)),
                (sets[1], intervals(sets[1], 50.0, skip=5)),  # 01-02 h incomplete
                (sets[2], intervals(sets[2], 25.0)),
            ],
            "energy_interval_received": [(s, intervals(s, 10.0)) for s in sets],
        },
    )
    await setup_and_import(hass, mock_config_entry)

    hour = timedelta(hours=1)
    assert await sums(hass, IMPORTED) == [
        (T0, 400.0),
        (T0 + hour, 800.0),
        (T0 + 2 * hour, 1000.0),  # 03:00 is missing an interval: skipped
        (T0 + 4 * hour, 1100.0),
        (T0 + 5 * hour, 1200.0),
    ]
    assert [s for _, s in await sums(hass, EXPORTED)] == [40.0 * n for n in range(1, 7)]
    assert mock_meter.read_interval_sets.await_count == 4  # 2 + 1 sets, twice
    metadata = await get_instance(hass).async_add_executor_job(
        list_statistic_ids, hass, {IMPORTED}
    )
    assert metadata[0]["source"] == DOMAIN
    assert metadata[0]["statistics_unit_of_measurement"] == "Wh"
    assert metadata[0]["name"] == f"Electric meter {MOCK_SFDI} energy imported"


@pytest.mark.usefixtures("recorder_mock")
async def test_new_hours_continue_the_sum(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A slow poll reads only the sets with new hours."""
    sets = [reading_set(i) for i in range(3)]
    history = {
        "energy_interval_delivered": [(s, intervals(s, 100.0)) for s in sets[:2]],
        "energy_interval_received": [],
    }
    serve(mock_meter, history)
    await setup_and_import(hass, mock_config_entry)
    assert (await sums(hass, IMPORTED))[-1] == (T0 + timedelta(hours=3), 1600.0)

    history["energy_interval_delivered"].append((sets[2], intervals(sets[2], 1.0)))
    mock_meter.read_interval_sets.reset_mock()
    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)

    assert (await sums(hass, IMPORTED))[-2:] == [
        (T0 + timedelta(hours=4), 1604.0),
        (T0 + timedelta(hours=5), 1608.0),
    ]
    read = mock_meter.read_interval_sets.await_args_list
    assert [[s.href for s in call.args[1]] for call in read] == [["/rs/2"]]


@pytest.mark.usefixtures("recorder_mock")
async def test_a_set_still_filling_is_read_again(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Its hours imported before are not imported twice."""
    current = reading_set(0)
    filling = intervals(current, 100.0)[:6]  # 00:00-01:30 so far
    history = {
        "energy_interval_delivered": [(current, filling)],
        "energy_interval_received": [],
    }
    serve(mock_meter, history)
    await setup_and_import(hass, mock_config_entry)
    assert await sums(hass, IMPORTED) == [(T0, 400.0)]

    history["energy_interval_delivered"] = [(current, intervals(current, 100.0))]
    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert await sums(hass, IMPORTED) == [
        (T0, 400.0),
        (T0 + timedelta(hours=1), 800.0),
    ]


@pytest.mark.usefixtures("recorder_mock")
async def test_a_failed_read_keeps_what_came_before(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """The hours read before a timeout stay; the next run reads only the rest."""
    sets = [reading_set(i) for i in range(3)]
    serve(
        mock_meter,
        {
            "energy_interval_delivered": [(s, intervals(s, 100.0)) for s in sets],
            "energy_interval_received": [],
        },
    )
    read = mock_meter.read_interval_sets.side_effect
    calls: list[list[str]] = []

    async def flaky(channel: Channel, chunk: Any, **kwargs: Any) -> Any:
        calls.append([s.href for s in chunk])
        if len(calls) == 2:
            raise MeterConnectionError("timed out")
        return await read(channel, chunk)

    mock_meter.read_interval_sets.side_effect = flaky
    await setup_and_import(hass, mock_config_entry)
    # Sets 0 and 1, but their last hour waits to be summed with set 2.
    assert len(await sums(hass, IMPORTED)) == 3

    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert [s for _, s in await sums(hass, IMPORTED)] == [
        400.0 * n for n in range(1, 7)
    ]
    assert calls == [["/rs/0", "/rs/1"], ["/rs/2"], ["/rs/1", "/rs/2"]]


@pytest.mark.usefixtures("recorder_mock")
async def test_an_hour_split_between_reads_is_summed_whole(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
) -> None:
    """Sets need not end on the hour: 45-minute sets, two per read."""
    sets = [
        replace(
            reading_set(0),
            href=f"/rs/{i}",
            start=T0 + i * 3 * QUARTER,
            duration=3 * 900,
        )
        for i in range(4)
    ]
    serve(
        mock_meter,
        {
            "energy_interval_delivered": [(s, intervals(s, 100.0)) for s in sets],
            "energy_interval_received": [],
        },
    )
    await setup_and_import(hass, mock_config_entry)
    assert await sums(hass, IMPORTED) == [
        (T0, 400.0),
        (T0 + timedelta(hours=1), 800.0),
        (T0 + timedelta(hours=2), 1200.0),
    ]


@pytest.mark.usefixtures("recorder_mock")
async def test_an_unreachable_meter_is_tried_again(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """Nothing is imported until the history can be read."""
    sets = [reading_set(0)]
    serve(
        mock_meter,
        {
            "energy_interval_delivered": [(sets[0], intervals(sets[0], 100.0))],
            "energy_interval_received": [],
        },
    )
    mock_meter.read_interval_sets.side_effect = MeterConnectionError("down")
    await setup_and_import(hass, mock_config_entry)
    assert await sums(hass, IMPORTED) == []

    serve(
        mock_meter,
        {
            "energy_interval_delivered": [(sets[0], intervals(sets[0], 100.0))],
            "energy_interval_received": [],
        },
    )
    await poll(hass, freezer, SLOW_POLLS)
    await hass.async_block_till_done(wait_background_tasks=True)
    assert len(await sums(hass, IMPORTED)) == 2


@pytest.mark.parametrize(
    ("values", "message"),
    [
        pytest.param(
            [
                IntervalValue(
                    start=T0 + timedelta(minutes=50),
                    end=T0 + timedelta(minutes=65),
                    value=1.0,
                    unit="Wh",
                )
            ],
            "Cannot sum the meter's energy_imported intervals",
            id="straddles-an-hour",
        ),
        pytest.param(
            intervals(reading_set(0), 1.0, unit="VAh"),
            "The meter's energy_imported is in VAh, not energy",
            id="not-energy",
        ),
    ],
)
@pytest.mark.usefixtures("recorder_mock")
async def test_intervals_that_cannot_be_imported(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    caplog: pytest.LogCaptureFixture,
    values: list[IntervalValue],
    message: str,
) -> None:
    """Nothing is made up from intervals that do not fit."""
    serve(
        mock_meter,
        {
            "energy_interval_delivered": [(reading_set(0), values)],
            "energy_interval_received": [],
        },
    )
    await setup_and_import(hass, mock_config_entry)
    assert await sums(hass, IMPORTED) == []
    assert message in caplog.text


@pytest.mark.usefixtures("recorder_mock")
async def test_a_meter_without_interval_data(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    profile: MeterProfile,
) -> None:
    """Nothing to import, nothing asked."""
    mock_meter.discover.return_value = replace(
        profile,
        channels=tuple(c for c in profile.channels if "interval" not in c.key),
    )
    await setup_and_import(hass, mock_config_entry)
    mock_meter.list_interval_sets.assert_not_awaited()


@pytest.mark.usefixtures("recorder_mock")
async def test_one_import_at_a_time(
    hass: HomeAssistant,
    mock_meter: AsyncMock,
    mock_config_entry: MockConfigEntry,
    freezer: FrozenDateTimeFactory,
) -> None:
    """A slow poll while the history is still being read starts no second one."""
    release = asyncio.Event()

    async def slow_list(channel: Channel) -> list[ReadingSetInfo]:
        await release.wait()
        return []

    mock_meter.list_interval_sets.side_effect = slow_list
    await setup_integration(hass, mock_config_entry)
    await poll(hass, freezer, SLOW_POLLS)
    assert mock_meter.list_interval_sets.await_count == 1
    release.set()
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_meter.list_interval_sets.await_count == 2  # then the next channel


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(
    recorder_mock: Recorder, enable_custom_integrations: None
) -> None:
    """The recorder first: it must start before Home Assistant does."""
