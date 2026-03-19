"""
Strategy Engine 初期 seed データ

2つの strategy を seed する:
  A. long_morning_trend       — 朝の上昇トレンドで long エントリー
  B. short_risk_off_rebound   — リスクオフ時の反発 short

再現可能な形で残す（migration ではなく実行時 seed）。
重複実行した場合は strategy_code の UNIQUE 制約でスキップ（べき等）。

使い方:
  from trade_app.services.strategy.seed import seed_strategies
  await seed_strategies(db)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from trade_app.models.strategy_condition import StrategyCondition
from trade_app.models.strategy_definition import StrategyDefinition

logger = logging.getLogger(__name__)

# ─── seed 定義 ─────────────────────────────────────────────────────────────

_STRATEGIES = [
    {
        "strategy_code": "long_morning_trend",
        "strategy_name": "朝の上昇トレンド long",
        "description": (
            "morning_trend_zone かつ symbol_trend_up の場合に long エントリー許可。"
            "opening_auction_risk または risk_off の場合はブロック。"
            "wide_spread 時は size を 50% に縮小。"
        ),
        "direction": "long",
        "priority": 10,
        "is_enabled": True,
        "max_size_ratio": 1.0,
        "conditions": [
            {
                "condition_type": "required_state",
                "layer": "time_window",
                "state_code": "morning_trend_zone",
                "operator": "exists",
                "notes": "朝のトレンド時間帯であること",
            },
            {
                "condition_type": "required_state",
                "layer": "symbol",
                "state_code": "symbol_trend_up",
                "operator": "exists",
                "notes": "銘柄が上昇トレンドにあること",
            },
            {
                "condition_type": "forbidden_state",
                "layer": "time_window",
                "state_code": "opening_auction_risk",
                "operator": "exists",
                "notes": "寄り付き直後のオークションリスクあり → ブロック",
            },
            {
                "condition_type": "forbidden_state",
                "layer": "market",
                "state_code": "risk_off",
                "operator": "exists",
                "notes": "市場全体がリスクオフ → ブロック",
            },
            {
                "condition_type": "size_modifier",
                "layer": "symbol",
                "state_code": "wide_spread",
                "operator": "exists",
                "size_modifier": 0.5,
                "notes": "スプレッドが広い場合はポジションを 50% に縮小",
            },
        ],
    },
    {
        "strategy_code": "short_risk_off_rebound",
        "strategy_name": "リスクオフ時の反発 short",
        "description": (
            "market が trend_down かつ symbol が symbol_volatility_high の場合に short エントリー許可。"
            "midday_low_liquidity 時はブロック。"
        ),
        "direction": "short",
        "priority": 5,
        "is_enabled": True,
        "max_size_ratio": 0.8,
        "conditions": [
            {
                "condition_type": "required_state",
                "layer": "market",
                "state_code": "trend_down",
                "operator": "exists",
                "notes": "市場全体が下落トレンドにあること",
            },
            {
                "condition_type": "required_state",
                "layer": "symbol",
                "state_code": "symbol_volatility_high",
                "operator": "exists",
                "notes": "銘柄のボラティリティが高いこと",
            },
            {
                "condition_type": "forbidden_state",
                "layer": "time_window",
                "state_code": "midday_low_liquidity",
                "operator": "exists",
                "notes": "昼休み前後の流動性低下 → ブロック",
            },
        ],
    },
]


async def seed_strategies(db: AsyncSession) -> list[StrategyDefinition]:
    """
    初期 strategy を seed する（べき等: strategy_code 重複は INSERT スキップ）。

    Returns:
        seed した（または既存の）StrategyDefinition のリスト
    """
    now = datetime.now(timezone.utc)
    seeded: list[StrategyDefinition] = []

    for spec in _STRATEGIES:
        # 既存チェック
        existing = await db.execute(
            select(StrategyDefinition).where(
                StrategyDefinition.strategy_code == spec["strategy_code"]
            )
        )
        definition = existing.scalar_one_or_none()

        if definition is None:
            definition = StrategyDefinition(
                strategy_code=spec["strategy_code"],
                strategy_name=spec["strategy_name"],
                description=spec.get("description"),
                direction=spec.get("direction", "both"),
                priority=spec.get("priority", 0),
                is_enabled=spec.get("is_enabled", True),
                max_size_ratio=spec.get("max_size_ratio", 1.0),
                created_at=now,
                updated_at=now,
            )
            db.add(definition)
            await db.flush()

            for cond_spec in spec.get("conditions", []):
                cond = StrategyCondition(
                    strategy_id=definition.id,
                    condition_type=cond_spec["condition_type"],
                    layer=cond_spec["layer"],
                    state_code=cond_spec["state_code"],
                    operator=cond_spec.get("operator", "exists"),
                    threshold_value=cond_spec.get("threshold_value"),
                    size_modifier=cond_spec.get("size_modifier"),
                    notes=cond_spec.get("notes"),
                    created_at=now,
                )
                db.add(cond)

            logger.info(
                "Strategy seed: inserted strategy_code=%s", spec["strategy_code"]
            )
        else:
            logger.debug(
                "Strategy seed: skipped (already exists) strategy_code=%s",
                spec["strategy_code"],
            )

        seeded.append(definition)

    await db.flush()
    return seeded
