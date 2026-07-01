"""Tests for src.data_fetcher volume normalization, CSV I/O, and merge logic."""

from __future__ import annotations

import copy
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.data_fetcher import (  # noqa: E402
    KlineBar,
    _auto_normalize_volume,
    _bars_to_csv,
    _calc_scale_from_median,
    _merge_bars,
    _normalize_volume_units,
)

CSV_HEADER = "datetime,open,close,high,low,volume,amount,change_pct,change"


def _day_str(base: str, offset: int) -> str:
    """Generate YYYY-MM-DD strings stepping by calendar days."""
    dt = datetime.strptime(base, "%Y-%m-%d") + timedelta(days=offset)
    return dt.strftime("%Y-%m-%d")


def make_kline_bars(
    n: int,
    vol_old: int,
    vol_new: int,
    boundary: int,
    *,
    old_amount: float = 0.0,
    new_amount: float = 0.0,
    start_date: str = "2024-01-01",
    base_price: float = 10.0,
) -> list[KlineBar]:
    """Build synthetic KlineBar series with a volume/amount regime change at boundary."""
    bars: list[KlineBar] = []
    for i in range(n):
        amount = old_amount if i < boundary else new_amount
        volume = vol_old if i < boundary else vol_new
        bars.append(
            KlineBar(
                datetime=_day_str(start_date, i),
                open=base_price,
                close=base_price + 0.1,
                high=base_price + 0.2,
                low=base_price - 0.1,
                volume=volume,
                amount=amount,
                change_pct=0.0,
                change=0.0,
            )
        )
    return bars


def parse_csv(text: str) -> list[KlineBar]:
    """Parse CSV produced by _bars_to_csv back into KlineBar objects."""
    lines = [ln for ln in text.strip().split("\n") if ln.strip()]
    bars: list[KlineBar] = []
    for line in lines[1:]:
        parts = line.split(",")
        bars.append(
            KlineBar(
                datetime=parts[0],
                open=float(parts[1]),
                close=float(parts[2]),
                high=float(parts[3]),
                low=float(parts[4]),
                volume=int(float(parts[5])),
                amount=float(parts[6]) if len(parts) > 6 else 0.0,
                change_pct=float(parts[7]) if len(parts) > 7 else 0.0,
                change=float(parts[8]) if len(parts) > 8 else 0.0,
            )
        )
    return bars


def write_csv(path: Path, bars: list[KlineBar]) -> None:
    path.write_text(_bars_to_csv(bars), encoding="utf-8")


class TestNormalizeVolumeUnits:
    def test_direction2_daily_api_change(self):
        """Tencent daily API changed ~2026-03-03: volume unit shifted ~100x."""
        boundary = 20
        bars = make_kline_bars(
            40,
            vol_old=100,
            vol_new=10_000,
            boundary=boundary,
            old_amount=1_000.0,
            new_amount=0.0,
        )
        original_new_vols = [b.volume for b in bars[boundary:]]

        _normalize_volume_units(bars)

        for b in bars[:boundary]:
            assert b.volume == 100
        for before, bar in zip(original_new_vols, bars[boundary:]):
            assert bar.amount == 0.0
            assert bar.volume == int(before / 100)

    def test_direction1_intraday_pattern(self):
        """Intraday: amount=0 era should be scaled up to match amount>0 era."""
        boundary = 20
        vol_old = 15_000
        vol_new = 10_000
        bars = make_kline_bars(
            40,
            vol_old=vol_old,
            vol_new=vol_new,
            boundary=boundary,
            old_amount=0.0,
            new_amount=500.0,
        )

        _normalize_volume_units(bars)

        expected = int(vol_old * vol_new / vol_old)
        for b in bars[:boundary]:
            assert b.volume == expected
        for b in bars[boundary:]:
            assert b.volume == vol_new

    def test_no_normalization_when_ratio_small(self):
        """When old/new volume ratio is < 5x, no normalization needed."""
        boundary = 20
        bars = make_kline_bars(
            40,
            vol_old=100,
            vol_new=200,
            boundary=boundary,
            old_amount=1_000.0,
            new_amount=0.0,
        )
        expected = copy.deepcopy(bars)

        _normalize_volume_units(bars)

        for got, want in zip(bars, expected):
            assert got.volume == want.volume
            assert got.amount == want.amount

    def test_no_normalization_insufficient_data(self):
        """When either side of boundary has < 10 bars, skip normalization."""
        boundary = 15
        bars = make_kline_bars(
            20,
            vol_old=100,
            vol_new=10_000,
            boundary=boundary,
            old_amount=1_000.0,
            new_amount=0.0,
        )
        expected = copy.deepcopy(bars)

        _normalize_volume_units(bars)

        for got, want in zip(bars, expected):
            assert got.volume == want.volume

    def test_direction1_skipped_when_boundary_too_late(self):
        """Direction 1 needs >10 bars after boundary; late boundary falls through."""
        boundary = 28
        vol_old = 15_000
        bars = make_kline_bars(
            35,
            vol_old=vol_old,
            vol_new=10_000,
            boundary=boundary,
            old_amount=0.0,
            new_amount=500.0,
        )

        _normalize_volume_units(bars)

        for b in bars[:boundary]:
            assert b.volume == vol_old

    def test_only_amount_zero_bars_scaled_in_direction2(self):
        """Direction 2 scales only amount==0 bars; amount>0 realtime bars stay intact."""
        boundary = 20
        bars = make_kline_bars(
            35,
            vol_old=100,
            vol_new=10_000,
            boundary=boundary,
            old_amount=1_000.0,
            new_amount=0.0,
        )
        # Simulate Sina realtime bar mixed into the post-boundary era.
        sina_bar = KlineBar(
            datetime=_day_str("2024-01-01", 30),
            open=10.0,
            close=10.1,
            high=10.2,
            low=9.9,
            volume=5_000,
            amount=50_000.0,
            change_pct=0.0,
            change=0.0,
        )
        bars[30] = sina_bar

        _normalize_volume_units(bars)

        assert bars[30].volume == 5_000
        assert bars[30].amount == 50_000.0
        for b in bars[boundary:]:
            if b.amount == 0.0:
                assert b.volume == 100


class TestCalcScaleFromMedian:
    def test_returns_none_when_ratio_small(self):
        boundary = 20
        bars = make_kline_bars(40, vol_old=100, vol_new=80, boundary=boundary)
        assert _calc_scale_from_median(bars, boundary) is None

    def test_returns_scale_when_ratio_large_enough(self):
        boundary = 20
        vol_old = 15_000
        vol_new = 10_000
        bars = make_kline_bars(40, vol_old=vol_old, vol_new=vol_new, boundary=boundary)
        scale = _calc_scale_from_median(bars, boundary)
        assert scale == pytest.approx(vol_new / vol_old)


class TestBarsToCsv:
    def test_csv_roundtrip(self):
        bars = [
            KlineBar(
                datetime="2024-06-01",
                open=1.1,
                close=1.2,
                high=1.3,
                low=1.0,
                volume=12345,
                amount=67890.5,
                change_pct=0.5,
                change=0.01,
            ),
            KlineBar(
                datetime="2024-06-02 10:30:00",
                open=2.0,
                close=2.1,
                high=2.2,
                low=1.9,
                volume=999,
                amount=0.0,
                change_pct=-0.3,
                change=-0.01,
            ),
        ]

        roundtrip = parse_csv(_bars_to_csv(bars))

        assert len(roundtrip) == len(bars)
        for got, want in zip(roundtrip, bars):
            assert got.datetime == want.datetime
            assert got.open == want.open
            assert got.close == want.close
            assert got.high == want.high
            assert got.low == want.low
            assert got.volume == want.volume
            assert got.amount == want.amount
            assert got.change_pct == want.change_pct
            assert got.change == want.change

    def test_csv_header(self):
        csv_text = _bars_to_csv([])
        assert csv_text.split("\n")[0] == CSV_HEADER


class TestMergeBars:
    def test_merge_no_collision(self, tmp_path: Path):
        existing = make_kline_bars(
            5,
            vol_old=100,
            vol_new=100,
            boundary=5,
            old_amount=1.0,
            new_amount=1.0,
            start_date="2024-01-01",
        )
        new_bars = make_kline_bars(
            3,
            vol_old=200,
            vol_new=200,
            boundary=3,
            old_amount=1.0,
            new_amount=1.0,
            start_date="2024-01-06",
        )
        csv_path = tmp_path / "daily.csv"
        write_csv(csv_path, existing)

        merged = _merge_bars(str(csv_path), new_bars)

        assert len(merged) == 8
        assert merged[0].datetime == "2024-01-01"
        assert merged[-1].datetime == "2024-01-08"
        assert merged[4].volume == 100
        assert merged[5].volume == 200

    def test_merge_collision_new_wins(self, tmp_path: Path):
        existing = [
            KlineBar(
                datetime="2024-03-01",
                open=10.0,
                close=10.0,
                high=10.5,
                low=9.5,
                volume=100,
                amount=1.0,
            )
        ]
        new_bars = [
            KlineBar(
                datetime="2024-03-01",
                open=11.0,
                close=11.5,
                high=12.0,
                low=10.8,
                volume=200,
                amount=2.0,
            )
        ]
        csv_path = tmp_path / "daily.csv"
        write_csv(csv_path, existing)

        merged = _merge_bars(str(csv_path), new_bars)

        assert len(merged) == 1
        assert merged[0].close == 11.5
        assert merged[0].volume == 200

    def test_merge_sorted_output(self, tmp_path: Path):
        existing = make_kline_bars(
            2,
            vol_old=100,
            vol_new=100,
            boundary=2,
            old_amount=1.0,
            new_amount=1.0,
            start_date="2024-05-01",
        )
        new_bars = [
            make_kline_bars(
                1,
                vol_old=150,
                vol_new=150,
                boundary=1,
                old_amount=1.0,
                new_amount=1.0,
                start_date="2024-04-15",
            )[0],
            make_kline_bars(
                1,
                vol_old=160,
                vol_new=160,
                boundary=1,
                old_amount=1.0,
                new_amount=1.0,
                start_date="2024-05-10",
            )[0],
        ]
        csv_path = tmp_path / "daily.csv"
        write_csv(csv_path, existing)

        merged = _merge_bars(str(csv_path), new_bars)
        datetimes = [b.datetime for b in merged]

        assert datetimes == sorted(datetimes)
        assert len(merged) == 4


class TestAutoNormalizeVolume:
    """Tests for _auto_normalize_volume — heuristic volume unit detection."""

    def _make_bars(self, close: float, volumes: list[int]) -> list[KlineBar]:
        return [
            KlineBar(
                datetime=_day_str("2026-06-01", i),
                open=close, close=close, high=close + 0.01, low=close - 0.01,
                volume=v, amount=0.0,
            )
            for i, v in enumerate(volumes)
        ]

    def test_bulk_shares_to_lots(self):
        """588000-like: low price + huge volume → volume is in 股, should /100."""
        # close=2.3, volume=3B → turnover = 6.9B >> 5B threshold
        bars = self._make_bars(2.3, [3_000_000_000] * 20)
        _auto_normalize_volume(bars)
        assert all(b.volume == 30_000_000 for b in bars)

    def test_lots_unchanged(self):
        """510300-like: moderate price + normal volume → already in 手, no change."""
        # close=5.0, volume=10M → turnover = 50M << 5B threshold
        bars = self._make_bars(5.0, [10_000_000] * 20)
        _auto_normalize_volume(bars)
        assert all(b.volume == 10_000_000 for b in bars)

    def test_mixed_single_bar_spike(self):
        """Single bar has 100x volume spike → normalize just that bar."""
        vols = [10_000] * 19 + [1_000_000]
        bars = self._make_bars(5.0, vols)
        _auto_normalize_volume(bars)
        assert bars[-1].volume == 10_000
        assert bars[0].volume == 10_000

    def test_mixed_single_bar_drop(self):
        """Single bar has 100x volume drop → scale up that bar."""
        vols = [1_000_000] * 19 + [10_000]
        bars = self._make_bars(5.0, vols)
        _auto_normalize_volume(bars)
        assert bars[-1].volume == 1_000_000
        assert bars[0].volume == 1_000_000

    def test_too_few_bars_no_change(self):
        """With < 5 bars, no normalization should be attempted."""
        bars = self._make_bars(2.3, [3_000_000_000] * 3)
        original = [b.volume for b in bars]
        _auto_normalize_volume(bars)
        assert [b.volume for b in bars] == original

    def test_zero_volume_bars_ignored(self):
        """Bars with zero volume should not cause division errors."""
        vols = [0, 10_000_000, 0, 10_000_000, 10_000_000]
        bars = self._make_bars(5.0, vols)
        _auto_normalize_volume(bars)
        assert bars[1].volume == 10_000_000
