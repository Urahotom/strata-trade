"""発注前リスクチェックとポジションサイジング."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from strata.api.models import OrderSide
from strata.config import RiskSettings


@dataclass(frozen=True)
class SubmitSpec:
    """発注意図の発注前評価に必要な情報."""

    symbol: str
    side: OrderSide
    qty: int
    #: 指値価格（成行のときは0）
    price: float
    #: 発注評価価格（成行のときは現在値等の参照価格、必ず正）
    ref_price: float
    #: ロスカット価格幅（1株あたり、正）。想定リスク = qty x stop_width
    stop_width: float | None = None


@dataclass(frozen=True)
class RiskContext:
    """発注時点の口座・台帳状態."""

    #: 現在の同時保有ポジション数（非終端意図の銘柄数など）
    open_positions: int = 0
    #: 既に発注済みの想定リスク合計（円）。今回の発注に上乗せ評価される
    committed_risk: float = 0.0


@dataclass(frozen=True)
class RiskVerdict:
    ok: bool
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def approve(cls) -> RiskVerdict:
        return cls(ok=True)

    @classmethod
    def reject(cls, *reasons: str) -> RiskVerdict:
        return cls(ok=False, reasons=tuple(reasons))


class RiskEngine:
    """設定されたリスク上限による発注前チェック."""

    def __init__(self, settings: RiskSettings) -> None:
        self._s = settings

    @property
    def settings(self) -> RiskSettings:
        """設定の参照用（エンジンがサイジング枠の計算に使う）."""
        return self._s

    def check(self, spec: SubmitSpec, ctx: RiskContext) -> RiskVerdict:
        """すべてのチェックを通らなければ発注可（一つでも失敗で拒否）."""
        reasons: list[str] = []
        if spec.qty <= 0:
            reasons.append(f"数量が不正です ({spec.qty})")
        if spec.qty > self._s.max_qty_per_order:
            reasons.append(f"1回数量上限 {self._s.max_qty_per_order} を超過 ({spec.qty})")
        notional = (spec.price if spec.price > 0 else spec.ref_price) * spec.qty
        if spec.ref_price <= 0 and spec.price <= 0:
            reasons.append("評価価格が不明のため金額チェック不能")
        elif notional > self._s.max_notional_per_order:
            reasons.append(
                f"1回金額上限 {self._s.max_notional_per_order:,.0f} を超過 ({notional:,.0f})"
            )
        # 売りはポジションを減らす方向なので件数上限から除外
        if spec.side is not OrderSide.SELL and ctx.open_positions >= self._s.max_open_positions:
            reasons.append(
                f"同時ポジション上限 {self._s.max_open_positions} に到達 ({ctx.open_positions})"
            )
        if spec.stop_width is None and spec.side is OrderSide.SELL:
            pass  # 売りはリスクを減らす方向のためロスカット幅評価不要
        elif spec.stop_width is None:
            reasons.append("ロスカット幅が未設定のため想定リスク評価不能")
        elif spec.stop_width <= 0:
            reasons.append(f"ロスカット幅が不正です ({spec.stop_width})")
        else:
            est_risk = spec.qty * spec.stop_width
            total = ctx.committed_risk + est_risk
            if total > self._s.max_total_risk:
                reasons.append(
                    f"想定リスク合計 {total:,.0f} が上限 {self._s.max_total_risk:,.0f} を超過"
                )
        return RiskVerdict.reject(*reasons) if reasons else RiskVerdict.approve()

    def size_position(self, risk_budget: float, stop_width: float, trading_unit: int) -> int:
        """許容リスク額とロスカット幅から株数を算出（単元切り下げ）.

        1単元に満たない（0になる）場合は発注不可の合意で0を返す。
        """
        if stop_width <= 0 or risk_budget <= 0 or trading_unit <= 0:
            return 0
        raw = risk_budget / stop_width
        if math.isnan(raw) or math.isinf(raw):  # pragma: no cover - 防御
            return 0
        return int(raw // trading_unit) * trading_unit
