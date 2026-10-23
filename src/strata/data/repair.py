"""データ修復（研究検証用・未登録分割とスパイクの除去）.

実運用パス（1日エンジン）は validate.py が異常値で取引中断する安全側設計。
一方、10年以上のバックテストでは以下が系列を壊す:

1. **プロバイダが知らない株式分割**（Yahoo の events に無く、adjclose も
   遡及調整しない）→ 分割日が -90% の真実の暴落として残る
2. **単発の不正ティック**（数日間だけ 1/10 レベルの応答が混入し、その後
   元のレベルへ戻ることがある）

判定は「ジャンプ後 persist 営業日その水準が持続するか」:
- 持続 → 分割とみなし、**その日より前の全履歴を同率で再スケール**
- 一過性 → 不正ティックとして前日値に置換

原則はクローズの相対レベルの修復のみ（出来高・OHL は同日倍率で追随）。
"""

from __future__ import annotations

from strata.data.base import DailyBar


def repair_split_jumps(
    bars: list[DailyBar],
    *,
    jump_threshold: float = 0.55,
    persist_days: int = 5,
    persist_tolerance: float = 0.15,
) -> list[DailyBar]:
    """未登録分割を再スケールし、一過性の異常ティックを置換する.

    jump_threshold: 前日比この倍率未満（または逆 >1/threshold 超）をジャンプ候補。
    元の値を保持したまま、close の相対系列のみを修復した新しいバー列を返す。
    """
    if not bars:
        return []
    adj = [b.close for b in bars]
    for i in range(1, len(adj)):
        if adj[i - 1] <= 0:
            continue
        ratio = adj[i] / adj[i - 1]
        if jump_threshold <= ratio <= 1.0 / jump_threshold:
            continue
        window = adj[i : i + persist_days]
        sustained = (
            adj[i] > 0
            and bool(window)
            and all(abs(c / adj[i] - 1.0) < persist_tolerance for c in window)
        )
        if sustained:
            for j in range(i):
                adj[j] *= ratio
        else:
            adj[i] = adj[i - 1]
    repaired: list[DailyBar] = []
    for bar, close in zip(bars, adj, strict=True):
        factor = close / bar.close if bar.close > 0 else 1.0
        repaired.append(
            bar.model_copy(
                update={
                    "close": close,
                    "open": bar.open * factor,
                    "high": bar.high * factor,
                    "low": bar.low * factor,
                }
            )
        )
    return repaired
