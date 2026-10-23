"""日足バーの品質バリデータ.

出典が何を信じさせるデータであってもDBへ入れる前に通す関門。
- 却下（DB採用不可）: high<low、NaN、日付不正、重複日
- 警告（採用するが報告）: 出来高0、日付の大きなギャップ
- 中断判定: 当日（または直近期待日）のバー presence
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from strata.data.base import DailyBar

#: 連続するバーの間隔がこの日数を超過したら欠損疑い（週末3日+祝日連発の余裕を見る）
GAP_SUSPECT_DAYS = 7


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


@dataclass(frozen=True)
class RejectedBar:
    """採用を見送ったバーと理由."""

    bar: DailyBar
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class QualityReport:
    """バリデーション結果."""

    #: 採用可（日付昇順・重複なし）
    valid: list[DailyBar] = field(default_factory=list)
    rejected: list[RejectedBar] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: 期待される当日バーが存在しなかった（取引中断の判断に使う）
    missing_today: bool = False
    #: 直近有効バーの日付（当日バーが無い場合はその代替基準）
    latest_date: str | None = None

    @property
    def is_clean(self) -> bool:
        return not self.rejected and not self.warnings and not self.missing_today


def _bar_problems(bar: DailyBar) -> list[str]:
    problems: list[str] = []
    try:
        _parse_date(bar.date)
    except ValueError:
        problems.append(f"日付形式が不正です ({bar.date})")
    for name in ("open", "high", "low", "close"):
        value = getattr(bar, name)
        if math.isnan(value) or math.isinf(value):
            problems.append(f"{name} が NaN/無限値です")
    if not problems and bar.high < bar.low:
        problems.append(f"高値が安値を下回ります (high={bar.high} < low={bar.low})")
    return problems


def validate_bars(
    bars: Sequence[DailyBar],
    *,
    today: date | str | None = None,
    gap_suspect_days: int = GAP_SUSPECT_DAYS,
) -> QualityReport:
    """バー群を検証し、採否と警告・当日バー不在を報告する.

    Args:
        bars: 検証対象（順不同でよい）
        today: 当日バーを期待する日。指定時は無視可能な週末/祝日を
            考慮して「当日または直近の有効バーがあるか」を判定する
        gap_suspect_days: 営業日ギャップの警告閾値（カレンダー日数）
    """
    valid: list[DailyBar] = []
    rejected: list[RejectedBar] = []
    warnings: list[str] = []
    seen: dict[str, DailyBar] = {}

    ordered = sorted(bars, key=lambda b: b.date)
    for bar in ordered:
        problems = _bar_problems(bar)
        if problems:
            rejected.append(RejectedBar(bar=bar, reasons=tuple(problems)))
            continue
        if bar.date in seen:
            rejected.append(RejectedBar(bar=bar, reasons=("同日の重複バー",)))
            continue
        seen[bar.date] = bar
        valid.append(bar)

    for bar in valid:
        if bar.volume == 0:
            warnings.append(f"{bar.date}: 出来高0のバーが含まれます")

    parsed_dates = [_parse_date(b.date) for b in valid]
    for prev, curr in itertools.pairwise(parsed_dates):
        gap = (curr - prev).days
        if gap > gap_suspect_days:
            warnings.append(f"{prev}〜{curr}: {gap}日のギャップ（データ欠損の可能性）")

    missing_today = False
    if today is not None:
        today_d = _parse_date(today) if isinstance(today, str) else today
        missing_today = not parsed_dates or parsed_dates[-1] < _expected_latest(today_d)

    return QualityReport(
        valid=valid,
        rejected=rejected,
        warnings=warnings,
        missing_today=missing_today,
        latest_date=valid[-1].date if valid else None,
    )


def _expected_latest(today: date) -> date:
    """当日にバーが存在しうる最も新しい日付（当日が週末なら直近金曜）."""
    d = today
    while d.weekday() >= 5:  # 5=土, 6=日
        d -= timedelta(days=1)
    return d


#: JST（証券の「当日」の基準）
JST = timezone(timedelta(hours=9))


def jst_today() -> date:
    """JST基準の今日（サーバーがUTCでも証券の「当日」に合わせる）."""
    return datetime.now(JST).date()
