"""
DailyMetrics — 日次メトリクス供給モジュール

DailyMetricsRepository: daily_price_history から直近 N 取引日を取得する。
DailyMetricsComputer:   取得した行から MA5 / MA20 / ATR14 / RSI14 を計算し
                        symbol_data に注入するための dict を返す。

計算定義:
  ma5   = 直近5取引日 close の単純平均
  ma20  = 直近20取引日 close の単純平均
  atr   = 直近14取引日の Wilder ATR（14本）
            TR_i = max(H-L, |H-prev_close|, |L-prev_close|)
            先頭行（prev_close なし）は TR = H - L
  rsi   = 直近14期間 RSI（close 変化量ベース）
            必要行数: 15（14変化を得るため）

Stale ポリシー:
  最新 trading_date が today_jst - stale_threshold_days より古い場合
  すべてのメトリクスを None とする（部分的な古い値の使用禁止）。

行数不足ポリシー:
  ma5   : rows < 5  → None
  ma20  : rows < 20 → None
  atr   : rows < 14 → None
  rsi   : rows < 15 → None
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.daily_price_history import DailyPriceHistory

logger = logging.getLogger(__name__)

# Stale 判定のデフォルト閾値（カレンダー日数）
# 週末（月曜日に前取引日=金曜日 = 3日前）+ 1バッファ = 4日
_DEFAULT_STALE_THRESHOLD_DAYS: int = 4


# ─── 内部データ行型 ────────────────────────────────────────────────────────────

@dataclass
class DailyPriceRow:
    """1 取引日分の OHLCV。計算層（Computer）が ORM から切り離されるよう定義する。"""
    trading_date: date
    open: float | None
    high: float | None
    low: float | None
    close: float    # NOT NULL
    volume: int | None


# ─── Repository ───────────────────────────────────────────────────────────────

class DailyMetricsRepository:
    """
    daily_price_history テーブルから直近 N 行を取得する。

    返却順: trading_date DESC（最新が先頭）
    DailyMetricsComputer が受け取る形式（list[DailyPriceRow]）に変換して返す。
    """

    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get_recent_rows(self, ticker: str, n: int = 21) -> list[DailyPriceRow]:
        """
        ticker の直近 n 取引日分を trading_date DESC 順で返す。

        daily_price_history にデータが存在しない場合は空リストを返す（例外なし）。
        """
        stmt = (
            select(DailyPriceHistory)
            .where(DailyPriceHistory.ticker == ticker)
            .order_by(DailyPriceHistory.trading_date.desc())
            .limit(n)
        )
        result = await self._db.execute(stmt)
        rows_orm = result.scalars().all()

        return [
            DailyPriceRow(
                trading_date=r.trading_date,
                open=float(r.open) if r.open is not None else None,
                high=float(r.high) if r.high is not None else None,
                low=float(r.low) if r.low is not None else None,
                close=float(r.close),
                volume=r.volume,
            )
            for r in rows_orm
        ]


# ─── Computer ─────────────────────────────────────────────────────────────────

class DailyMetricsComputer:
    """
    DailyPriceRow のリストから日次テクニカル指標を計算する。

    入力は trading_date DESC 順（最新が先頭）を想定する。
    compute() はクラスメソッドとして提供し、インスタンス不要で呼び出せる。
    """

    @classmethod
    def compute(
        cls,
        rows: list[DailyPriceRow],
        today_jst: date,
        stale_threshold_days: int = _DEFAULT_STALE_THRESHOLD_DAYS,
    ) -> dict[str, float | None]:
        """
        ma5 / ma20 / atr / rsi を計算して返す。

        stale 判定:
          rows が空 OR rows[0].trading_date < today_jst - stale_threshold_days
          → {"ma5": None, "ma20": None, "atr": None, "rsi": None}

        行数不足:
          各メトリクスに必要な行数を下回る場合は個別に None を返す。
          （stale でなくても行数不足は起こりうる — 初期データ蓄積中など）
        """
        _none_result: dict[str, float | None] = {
            "ma5": None,
            "ma20": None,
            "atr": None,
            "rsi": None,
        }

        # ─── Stale チェック ──────────────────────────────────────────────
        if not rows:
            logger.debug("DailyMetricsComputer: rows is empty — all None")
            return _none_result

        stale_cutoff = today_jst - timedelta(days=stale_threshold_days)
        if rows[0].trading_date < stale_cutoff:
            logger.warning(
                "DailyMetricsComputer: stale — latest trading_date=%s < cutoff=%s",
                rows[0].trading_date, stale_cutoff,
            )
            return _none_result

        # ─── 計算用リスト構築（close が None の行は除外しない — close は NOT NULL）
        # rows は DESC 順。MA / RSI には closes リスト（DESC）を使い、最新が末尾になるよう反転。
        closes_desc = [r.close for r in rows]   # 最新が先頭
        closes_asc = list(reversed(closes_desc))  # 最古が先頭

        # ─── MA5 ─────────────────────────────────────────────────────────
        ma5 = cls._compute_ma(closes_desc, n=5)

        # ─── MA20 ────────────────────────────────────────────────────────
        ma20 = cls._compute_ma(closes_desc, n=20)

        # ─── ATR14 ───────────────────────────────────────────────────────
        rows_asc = list(reversed(rows))  # 最古が先頭
        atr = cls._compute_atr(rows_asc, n=14)

        # ─── RSI14 ───────────────────────────────────────────────────────
        rsi = cls._compute_rsi(closes_asc, n=14)

        return {"ma5": ma5, "ma20": ma20, "atr": atr, "rsi": rsi}

    # ─── 内部計算関数 ─────────────────────────────────────────────────────────

    @staticmethod
    def _compute_ma(closes_desc: list[float], n: int) -> float | None:
        """直近 n 日の単純移動平均。closes_desc は最新が先頭。"""
        if len(closes_desc) < n:
            return None
        return sum(closes_desc[:n]) / n

    @staticmethod
    def _compute_atr(rows_asc: list[DailyPriceRow], n: int = 14) -> float | None:
        """
        Wilder ATR (n 期間)。rows_asc は最古が先頭。

        先頭行（prev_close なし）の TR は high - low を使用する。
        high または low が None の行が含まれる場合は None を返す。
        """
        if len(rows_asc) < n:
            return None
        rows = rows_asc[-n:]   # 直近 n 行（最古→最新）
        trs: list[float] = []
        for i, row in enumerate(rows):
            if row.high is None or row.low is None:
                return None
            if i == 0:
                tr = row.high - row.low
            else:
                prev_close = rows[i - 1].close
                tr = max(
                    row.high - row.low,
                    abs(row.high - prev_close),
                    abs(row.low - prev_close),
                )
            trs.append(tr)
        return sum(trs) / len(trs)

    @staticmethod
    def _compute_rsi(closes_asc: list[float], n: int = 14) -> float | None:
        """
        RSI (n 期間)。closes_asc は最古が先頭。n+1 行以上が必要。

        Wilder 初期値: gains / losses の単純平均。
        """
        if len(closes_asc) < n + 1:
            return None
        closes = closes_asc[-(n + 1):]   # 直近 n+1 行
        changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        gains = [max(0.0, c) for c in changes]
        losses = [max(0.0, -c) for c in changes]
        avg_gain = sum(gains) / n
        avg_loss = sum(losses) / n
        if avg_loss == 0.0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)
