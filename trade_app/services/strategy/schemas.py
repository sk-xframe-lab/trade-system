"""
Strategy Engine 内部データ構造

外部 API レスポンスではなく、エンジン・評価器内部で使うデータクラス。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class StrategyDecisionResult:
    """
    1つの strategy に対する判定結果。

    説明可能性:
      - matched_required_states  : 成立した required 条件
      - matched_forbidden_states : 成立した forbidden 条件（= ブロック理由）
      - missing_required_states  : 未成立の required 条件（= ブロック理由）
      - blocking_reasons         : ブロック理由の全リスト
      - applied_size_modifier    : 適用された size_modifier（複数の場合は最小値）
    """
    strategy_id: str
    strategy_code: str
    strategy_name: str
    ticker: str | None
    evaluation_time: datetime
    is_active: bool
    entry_allowed: bool
    size_ratio: float
    matched_required_states: list[str] = field(default_factory=list)
    matched_forbidden_states: list[str] = field(default_factory=list)
    missing_required_states: list[str] = field(default_factory=list)
    blocking_reasons: list[str] = field(default_factory=list)
    applied_size_modifier: float = 1.0
    evidence: dict[str, Any] = field(default_factory=dict)
