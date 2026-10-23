"""リスク管理（発注前チェック・サイジング・キルスイッチ）のテスト."""

from __future__ import annotations

import pytest

from strata.api.models import OrderSide
from strata.config import RiskSettings
from strata.risk.checks import RiskContext, RiskEngine, SubmitSpec
from strata.risk.killswitch import KillSwitch


@pytest.fixture()
def engine() -> RiskEngine:
    return RiskEngine(
        RiskSettings(
            max_qty_per_order=1000,
            max_notional_per_order=1_000_000,
            max_open_positions=2,
            max_total_risk=100_000,
        )
    )


def spec(**kw: object) -> SubmitSpec:
    base: dict[str, object] = {
        "symbol": "8306",
        "side": OrderSide.BUY,
        "qty": 100,
        "price": 0,
        "ref_price": 1000,
        "stop_width": 10,
    }
    base.update(kw)
    return SubmitSpec(**base)  # type: ignore[arg-type]


class TestCheck:
    def test_すべて合格(self, engine: RiskEngine) -> None:
        verdict = engine.check(spec(), RiskContext())
        assert verdict.ok

    def test_数量上限(self, engine: RiskEngine) -> None:
        verdict = engine.check(spec(qty=1001), RiskContext())
        assert not verdict.ok
        assert any("数量上限" in r for r in verdict.reasons)

    def test_金額上限は指値価格を優先評価(self, engine: RiskEngine) -> None:
        # 指値200円 x 9000株 = 1,800,000 > 上限1,000,000
        verdict = engine.check(spec(price=200, ref_price=100, qty=9000), RiskContext())
        assert not verdict.ok
        assert any("金額上限" in r for r in verdict.reasons)
        # 評価額（ref_price）ベースでも判定される
        assert not engine.check(spec(price=0, ref_price=1200, qty=900), RiskContext()).ok

    def test_ポジション数上限(self, engine: RiskEngine) -> None:
        verdict = engine.check(spec(), RiskContext(open_positions=2))
        assert not verdict.ok
        assert any("ポジション上限" in r for r in verdict.reasons)

    def test_リスク幅未設定は拒否(self, engine: RiskEngine) -> None:
        verdict = engine.check(spec(stop_width=None), RiskContext())
        assert not verdict.ok
        assert any("想定リスク" in r or "ロスカット幅" in r for r in verdict.reasons)

    def test_想定リスク合計上限(self, engine: RiskEngine) -> None:
        # 100株 x 10円 = 1,000。既に99,500コミット済み → 100,500 > 100,000
        verdict = engine.check(spec(), RiskContext(committed_risk=99_500))
        assert not verdict.ok
        assert any("想定リスク合計" in r for r in verdict.reasons)

    def test_評価価格不明は拒否(self, engine: RiskEngine) -> None:
        verdict = engine.check(spec(price=0, ref_price=0), RiskContext())
        assert not verdict.ok


class TestSizing:
    def test_単元切り下げ(self, engine: RiskEngine) -> None:
        assert engine.size_position(250_000, 30, 100) == 8300

    def test_ちょうど単元の倍数(self, engine: RiskEngine) -> None:
        assert engine.size_position(250_000, 25, 100) == 10000

    def test_1単元未満は0(self, engine: RiskEngine) -> None:
        assert engine.size_position(900, 10, 100) == 0

    def test_ゼロ除算防御(self, engine: RiskEngine) -> None:
        assert engine.size_position(100_000, 0, 100) == 0
        assert engine.size_position(0, 10, 100) == 0
        assert engine.size_position(100_000, 10, 0) == 0


class TestKillSwitch:
    def test_日次損失で作動(self) -> None:
        ks = KillSwitch(max_daily_loss=50_000)
        assert ks.check_daily_pl(-10_000) is False
        assert ks.check_daily_pl(-50_000) is True
        assert ks.active
        assert "日次損失" in ks.reasons[0]

    def test_通信失敗の連続で作動(self) -> None:
        ks = KillSwitch(max_daily_loss=50_000, consecutive_failure_threshold=3)
        assert ks.note_network_failure() is False
        assert ks.note_network_failure() is False
        assert ks.note_network_failure() is True
        assert ks.active

    def test_成功で連続カウントリセット(self) -> None:
        ks = KillSwitch(max_daily_loss=50_000, consecutive_failure_threshold=3)
        ks.note_network_failure()
        ks.note_network_failure()
        ks.note_success()
        ks.note_network_failure()
        ks.note_network_failure()
        assert not ks.active

    def test_作動は冪等で理由が累積(self) -> None:
        ks = KillSwitch(max_daily_loss=1)
        ks.activate("理由A")
        ks.activate("理由B")
        ks.activate("理由A")
        assert ks.reasons == ("理由A", "理由B")

    def test_手動解除(self) -> None:
        ks = KillSwitch(max_daily_loss=1)
        ks.activate("x")
        ks.reset()
        assert not ks.active
        assert ks.reasons == ()
