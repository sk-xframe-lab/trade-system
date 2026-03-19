"""
Market State Engine パッケージ

市場状態を標準化・永続化するエンジン。
売買判断は行わない。状態コードと証拠を DB に保存する。

モジュール構成:
  schemas.py           : 内部データ構造（EvaluationContext, StateEvaluationResult 等）
  evaluator_base.py    : AbstractEvaluator 基底クラス
  time_window_evaluator.py : 時間帯状態評価（日本株現物向け）
  market_evaluator.py  : 市場トレンド状態評価（簡易ルールベース）
  repository.py        : DB 操作（save / upsert / query）
  engine.py            : MarketStateEngine（evaluator 呼び出し + 永続化）
"""
from trade_app.services.market_state.engine import MarketStateEngine

__all__ = ["MarketStateEngine"]
