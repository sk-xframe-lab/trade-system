"""
BaseSizer — ベースサイズ計算 + lot 丸め

責務:
  - signal.quantity をベースサイズとして取得
  - strategy size_ratio を適用して縮小後サイズを算出
  - lot_size の倍数に切り下げ（日本株単元株単位）
  - サイズ増量は行わない（安全側原則）
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class SizeResult:
    """
    BaseSizer.calculate() の計算結果。

    base_qty → after_ratio_qty の過程を記録する。
    lot 丸めは別途 round_to_lot() で行う。
    """
    base_qty: int            # signal.quantity そのまま
    after_ratio_qty: int     # size_ratio 適用後（float → int 切り捨て）
    applied_size_ratio: float


class BaseSizer:
    """
    ベースサイズ計算クラス。

    以下のルールを厳守:
      - サイズ増量は行わない（after_ratio_qty <= base_qty を保証）
      - 小数切り捨て（保守的）
      - lot_size 丸めは切り下げ（partial lot での発注禁止）
    """

    def calculate(self, base_qty: int, size_ratio: float) -> SizeResult:
        """
        ベースサイズに size_ratio を適用する。

        Args:
            base_qty: signal.quantity（発注希望数量）
            size_ratio: strategy gate が算出した比率（0.0〜1.0）

        Returns:
            SizeResult
        """
        # size_ratio は 0〜1 の範囲にクランプ（増量防止）
        clamped_ratio = max(0.0, min(1.0, size_ratio))
        # 小数切り捨て（保守的）
        after_ratio = int(math.floor(base_qty * clamped_ratio))

        return SizeResult(
            base_qty=base_qty,
            after_ratio_qty=after_ratio,
            applied_size_ratio=clamped_ratio,
        )

    def round_to_lot(self, qty: int, lot_size: int) -> int:
        """
        数量を lot_size の倍数に切り下げる。

        日本株は単元株（通常 100 株）単位での発注が必要。
        例: qty=150, lot_size=100 → 100
            qty=50,  lot_size=100 → 0（→ PLANNED_SIZE_ZERO で reject）
            qty=0,   lot_size=100 → 0

        Args:
            qty: 丸め前の数量
            lot_size: 単元株数（1 以上）

        Returns:
            lot_size の倍数に切り下げた数量
        """
        if lot_size <= 0:
            return qty
        return (qty // lot_size) * lot_size
