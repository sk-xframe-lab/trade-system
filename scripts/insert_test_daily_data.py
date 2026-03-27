#!/usr/bin/env python3
"""
insert_test_daily_data.py — テスト用日次データを手動投入するスクリプト

J-Quants API キーがない場合に daily_price_history テーブルを
仮データで埋めて DailyMetricsComputer の動作確認を行う。

使用方法:
  docker compose exec trade_app python3 /app/scripts/insert_test_daily_data.py

投入内容:
  - 7203 / 6758 各 30 取引日分
  - close は固定値から微変動するダミーデータ
  - source = "test_manual"

実際の J-Quants データに置き換える場合は seed_daily_price.py を使用すること。
"""
import asyncio
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, "/app")


async def insert_dummy_data() -> None:
    from trade_app.models.database import AsyncSessionLocal
    from trade_app.models.daily_price_history import DailyPriceHistory  # noqa
    from sqlalchemy import text

    # 各銘柄の基準価格
    base_prices = {
        "7203": 3414.0,   # トヨタ自動車（実測値付近）
        "6758": 3222.0,   # ソニーグループ（実測値付近）
    }

    today = date(2026, 3, 27)  # 実施日に合わせる
    n_days = 30

    async with AsyncSessionLocal() as db:
        inserted_total = 0

        for ticker, base_close in base_prices.items():
            print(f"Inserting {n_days} rows for {ticker}...")

            for i in range(n_days):
                trading_date = today - timedelta(days=i)
                # 土日はスキップ（簡易的に土日除外）
                if trading_date.weekday() >= 5:
                    continue

                # ダミー価格: 微小変動 ±0.5% 以内
                import random
                random.seed(ticker + str(trading_date))
                delta_pct = (random.random() - 0.5) * 0.01  # ±0.5%
                close = round(base_close * (1 + delta_pct), 0)
                high = round(close * 1.005, 0)   # +0.5%
                low = round(close * 0.995, 0)    # -0.5%
                open_ = round((close + low) / 2, 0)
                volume = int(5_000_000 + random.random() * 5_000_000)

                # CONFLICT は無視（既存行があればスキップ）
                exist = await db.execute(
                    text("SELECT 1 FROM daily_price_history WHERE ticker=:t AND trading_date=:d"),
                    {"t": ticker, "d": trading_date},
                )
                if exist.fetchone():
                    continue

                await db.execute(
                    text("""
                        INSERT INTO daily_price_history
                          (id, ticker, trading_date, open, high, low, close, volume, source, created_at)
                        VALUES
                          (:id, :ticker, :trading_date, :open, :high, :low, :close, :volume, :source, :now)
                    """),
                    {
                        "id": str(uuid.uuid4()),
                        "ticker": ticker,
                        "trading_date": trading_date,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "source": "test_manual",
                        "now": datetime.now(timezone.utc),
                    },
                )
                inserted_total += 1

        await db.commit()
        print(f"Done. Total inserted: {inserted_total} rows")


if __name__ == "__main__":
    asyncio.run(insert_dummy_data())
