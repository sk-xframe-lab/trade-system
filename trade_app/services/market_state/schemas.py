"""
Market State Engine 内部データ構造

外部 API レスポンスではなく、エンジン内部で使うデータクラス。
Pydantic は使わず dataclass で軽量化する。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class EvaluationContext:
    """
    各 Evaluator に渡す評価コンテキスト。

    将来的に market_data（OHLCV 等）を追加しても
    Evaluator インターフェースを変えずに済む設計。
    """
    evaluation_time: datetime
    # 将来拡張: 指数 OHLCV データなどを追加
    market_data: dict[str, Any] = field(default_factory=dict)
    # 将来拡張: 個別銘柄データ
    symbol_data: dict[str, Any] = field(default_factory=dict)
    # Phase C+1: 遷移ベース記録のために engine が evaluator 実行前に設定する。
    # key=ticker, value=前回サイクルの active_states_json に含まれていた state_code の集合。
    # engine が CurrentStateSnapshot から取得して注入する。未設定の場合は空 dict（= 初回扱い）。
    prev_active_states_by_ticker: dict[str, set[str]] = field(default_factory=dict)
    # Phase G: observability — SymbolStateEvaluator が評価後に設定する rule 診断サマリ。
    # key=ticker, value={state_code → {status, reason?, ...metrics}}
    # engine が _update_symbol_snapshots() で state_summary_json["rule_diagnostics"] に書き込む。
    rule_diagnostics_by_ticker: dict[str, dict[str, dict[str, Any]]] = field(default_factory=dict)


@dataclass
class StateEvaluationResult:
    """
    1つの状態評価結果。
    Evaluator が返すリストの1要素として使用する。
    """
    layer: str                          # "market" | "symbol" | "time_window"
    target_type: str                    # "market" | "index" | "symbol" | "time_window"
    target_code: str | None             # 銘柄コード等。グローバルな状態は None
    state_code: str                     # 例: "morning_trend_zone"
    score: float = 1.0                  # 0.0〜1.0: 状態強度
    confidence: float = 1.0            # 0.0〜1.0: 評価信頼度
    evidence: dict[str, Any] = field(default_factory=dict)  # 判定根拠（必須）
    expires_at: datetime | None = None  # 有効期限（None = 次の評価まで）
    # Phase C+1: False = 前サイクルから継続中（engine が INSERT しない）。True = 新規活性化。
    is_new_activation: bool = True
