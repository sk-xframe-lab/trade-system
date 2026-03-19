"""
AbstractStateEvaluator — 評価器基底クラス

すべての Evaluator はこのインターフェースを実装すること。
evaluate() は純粋関数に近い設計（DB 不要）。
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from trade_app.services.market_state.schemas import EvaluationContext, StateEvaluationResult


class AbstractStateEvaluator(ABC):
    """
    Market State Engine が呼び出す評価器の基底クラス。

    各 Evaluator は独立して動作し、DB への書き込みは行わない。
    Engine が collect した結果を一括して repository に保存する。
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """評価器の名前（ログ用）"""
        ...

    @abstractmethod
    def evaluate(self, ctx: EvaluationContext) -> list[StateEvaluationResult]:
        """
        コンテキストを受け取り、評価結果リストを返す。

        Args:
            ctx: 評価コンテキスト（評価時刻・市場データ等）

        Returns:
            評価結果のリスト。状態が判定できない場合は空リストを返す。
        """
        ...
