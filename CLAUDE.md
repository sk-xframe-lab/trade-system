# CLAUDE.md — 日本株自動売買システム 引き継ぎ資料

> **次回セッション開始時は必ずこのファイルを読んでから作業を開始すること。**
> 実装・修正・設計変更を行ったら作業完了時に必ず更新すること。

---

## プロジェクト概要

- 分析システムが生成した売買シグナルを受信し、リスクチェック → 発注 → ポジション管理 → 決済を自動で行う
- 分析システム（外部）と執行システム（本リポジトリ）は REST API で疎結合
- **1ユーザー1サーバー**の専用構成。マルチテナントは考慮しない
- 対象市場: 日本株（東証）。立花証券 e_api 経由で発注予定（Phase 5）

---

## システム構成

```
分析システム ──POST /api/signals──> FastAPI (trade_app)
                                        │
                                   ┌────┴──────────────────────────────────┐
                                   │  SignalPipeline                        │
                                   │  SignalStrategyGate → RiskManager      │
                                   │                     → OrderRouter      │
                                   └────────────────────────────────────────┘
                                        │
                              ┌─────────┼─────────────┐
                         PostgreSQL   Redis        BrokerAdapter
                         (trade_db)  (lock/idem)   mock / tachibana
```

### 主要コンポーネント

| コンポーネント | ファイル | 役割 |
|---|---|---|
| FastAPI app | `trade_app/main.py` | エントリーポイント・lifespan 管理 |
| SignalReceiver | `services/signal_receiver.py` | シグナル受信・冪等性チェック |
| RiskManager | `services/risk_manager.py` | 発注前リスクチェック（halt 含む） |
| OrderRouter | `services/order_router.py` | ブローカーへの発注 |
| OrderPoller | `services/order_poller.py` | 約定確認・exit注文処理 |
| ExitWatcher | `services/exit_watcher.py` | TP/SL/TimeStop 判定・exit 開始 |
| PositionManager | `services/position_manager.py` | ポジション開設・CLOSING・CLOSED |
| HaltManager | `services/halt_manager.py` | 取引停止の発動・解除・照会 |
| ExitPolicies | `services/exit_policies.py` | TP/SL/TimeStop 判定ポリシー |
| MarketStateEngine | `services/market_state/engine.py` | 市場状態評価の実行・永続化 |
| TimeWindowStateEvaluator | `services/market_state/time_window_evaluator.py` | JST 時間帯→TimeWindow 状態評価 |
| MarketStateEvaluator | `services/market_state/market_evaluator.py` | 指数変動率→MarketCondition 状態評価 |
| StrategyEngine | `services/strategy/engine.py` | strategy 判定エンジン（発注禁止・評価のみ） |
| StrategyEvaluator | `services/strategy/evaluator.py` | 純粋判定ロジック（required/forbidden/size_modifier） |
| StrategyRunner | `services/strategy/runner.py` | 60秒周期バックグラウンド実行・失敗分離 |
| DecisionRepository | `services/strategy/decision_repository.py` | current_strategy_decisions UPSERT/取得 |
| SignalStrategyGate | `services/signal_strategy_gate.py` | entry signal に strategy gate を適用（RiskManager の前段） |

---

## 実装済みフェーズ

### Phase 1 ✅

- **DB (migration 001)**: `trade_signals`, `orders`, `positions`, `trade_results`, `audit_logs`
- SignalReceiver: Redis + DB 2層冪等性（`Idempotency-Key` ヘッダー）
- RiskManager: 市場時間 / 残高 / ポジション上限 / 日次損失 / 銘柄集中 / 未解決注文チェック
- OrderRouter: Redis 分散ロック付き発注
- PositionManager: `open_position()` / `close_position()` (旧フロー)
- BrokerAdapter 抽象 + MockBrokerAdapter (FillBehavior シナリオ付き)
- API: `POST /api/signals`, `GET /api/signals/{id}`, `GET /api/orders`, `GET /api/positions`, `GET /health`

### Phase 2 ✅

- **DB (migration 002)**: `executions`, `broker_requests`, `broker_responses`, `system_events`, `order_state_transitions`
- OrderPoller: バックグラウンドタスク（5秒間隔）。SUBMITTED/PARTIAL 照会 → FILLED/CANCELLED/REJECTED/UNKNOWN 処理
- RecoveryManager: 起動時に未解決注文をブローカーに再照会して整合
- AuditLogger: APPEND ONLY 監査ログ
- BrokerCallLogger: ブローカー API 呼び出しの全記録

### Phase 3 ✅ (実装日: 2026-03-15)

- **DB (migration 003)**: `trading_halts`, `position_exit_transitions` + `orders` テーブル変更
- TradingHalt モデル + HaltManager サービス（halt 状態の **DB 正本管理**）
- ExitWatcher（10秒間隔バックグラウンドタスク）
- TakeProfitPolicy / StopLossPolicy / TimeStopPolicy
- PositionManager 拡張: `initiate_exit()`, `finalize_exit()`, `update_unrealized_pnl()`
- OrderPoller: exit注文 fill 処理 + Execution 重複防止（`broker_execution_id` 事前チェック）
- RiskManager: `_check_trading_halt()` を `check()` の最優先ステップとして追加
- HaltManager.`check_and_halt_if_needed()`: 日次損失 / 連続損失を決済後に自動チェック
- 管理者 API: `GET/POST/DELETE /api/admin/halts`, `GET /api/admin/status`, `POST /api/admin/positions/{id}/close`
- BrokerAdapter: `get_market_price()` 抽象メソッド追加
- MockBrokerAdapter: `get_market_price()` + `set_price()` (テスト用価格制御)
- Config 追加: `CONSECUTIVE_LOSSES_STOP=3`, `EXIT_WATCHER_INTERVAL_SEC=10`

### Phase 3 完了判定 ✅ (2026-03-16 確認)

| 完了条件 | 状態 |
|---|---|
| exit PARTIAL/FILLED/CANCELLED/REJECTED/UNKNOWN 対応済み | ✅ OrderPoller + RecoveryManager |
| `remaining_qty` 導入（migration 004）| ✅ DB 適用確認済み |
| Execution 重複防止（broker_execution_id dedup）| ✅ OrderPoller + RecoveryManager |
| Recovery / Reconcile 対応（exit注文 FILLED/CANCELLED 含む）| ✅ |
| halt 管理（DB正本・自動発動・手動解除）| ✅ HaltManager |
| Phase 3 テスト（test_exit_policies/halt_manager/exit_watcher/order_poller）| ✅ 106件追加 |
| RiskManager テスト修正（halt mock 対応）| ✅ 12件 |
| .env.example 整備（CONSECUTIVE_LOSSES_STOP / EXIT_WATCHER_INTERVAL_SEC）| ✅ |
| migration 004 Docker 適用確認 | ✅ 2026-03-16 実施 |

**Phase 3 は全完了条件を満たした。** (Phase 4 実装済み)

### Phase 4 ✅ Market State Engine (実装日: 2026-03-16)

- **DB (migration 005)**: `state_definitions`, `state_evaluations`, `current_state_snapshots`
- MarketStateEngine: 複数 Evaluator を実行し結果を DB に永続化（発注判断は行わない）
- TimeWindowStateEvaluator: JST タイムゾーンで 8 時間帯（pre_open〜after_hours）を評価。TSE 昼休み廃止 (2024-11) 対応済み
- MarketStateEvaluator: `index_change_pct` ±0.5% 閾値による `normal/volatile_up/volatile_down` 判定
- StateEvaluation: APPEND ONLY 時系列ログ（`is_active` で軟式失効）
- CurrentStateSnapshot: マテリアライズドビュー的 UPSERT（select-then-update-or-insert、SQLite テスト互換）
- API: `GET /api/v1/market-state/current`, `GET /api/v1/market-state/history`
- インフラ修正（テスト環境整備）:
  - `JSONB` → `sa.JSON` 全モデルで置換（SQLite テスト互換）
  - 重複インデックス定義削除（`index=True` と `Index()` の二重定義）
  - Order↔Position 関係の `foreign_keys` 明示（AmbiguousForeignKeysError 解消）
  - SQLAlchemy 2.x: `default=lambda:` は FLUSH 時に適用 → テストで `id=str(uuid.uuid4())` を明示

### Phase 4 完了判定 ✅ (2026-03-16 確認)

| 完了条件 | 状態 |
|---|---|
| migration 005 (3 テーブル) | ✅ |
| TimeWindowStateEvaluator (8 時間帯 / JST) | ✅ |
| MarketStateEvaluator (±0.5% 閾値) | ✅ |
| MarketStateEngine (Evaluator 実行 → DB 永続化) | ✅ |
| StateEvaluation APPEND ONLY + is_active 軟式失効 | ✅ |
| CurrentStateSnapshot UPSERT (SQLite 互換) | ✅ |
| API 2 エンドポイント実装 | ✅ |
| テスト 31 件追加 (全 173 件通過) | ✅ |
| JSONB→JSON 全モデル修正 | ✅ |
| 重複 Index 定義削除 | ✅ |
| Order↔Position foreign_keys 明示 | ✅ |

**Phase 4 は全完了条件を満たした。Phase 5 に着手可能。**

### Phase 5 ✅ Symbol State Engine (実装日: 2026-03-16)

- **DB (migration 006)**: `state_evaluations` に `ix_state_eval_layer_target_time` インデックス追加
  - 既存 `ix_state_evaluations_target_time` (target_type, target_code, evaluation_time DESC) との重複なし
  - 追加: (layer, target_code, evaluation_time DESC) → `GET /symbols/{ticker}` クエリ最適化
- **SymbolStateEvaluator**: 11状態を評価（`gap_up_open`, `gap_down_open`, `symbol_trend_up`, `symbol_trend_down`, `symbol_range`, `high_relative_volume`, `low_liquidity`, `wide_spread`, `symbol_volatility_high`, `breakout_candidate`, `overextended`）
- **MarketStateEngine 修正**: `save_evaluations` を (layer, target_type, target_code) 単位でグループ化してソフト失効 → 同一銘柄複数状態の正しい保存を保証
- **MarketStateRunner**: 60秒周期バックグラウンドタスク（ExitWatcher:10秒・OrderPoller:5秒とは独立）
- **Config 追加**: `MARKET_STATE_INTERVAL_SEC=60`, `WATCHED_SYMBOLS=""`
- **API 追加**: `GET /api/v1/market-state/symbols/{ticker}` — active_states / score / confidence / evidence_list
- **テスト**: 52件追加 (全 225 件通過)

### Phase 5 完了判定 ✅ (2026-03-16 確認)

| 完了条件 | 状態 |
|---|---|
| migration 006 (index 追加、重複なし確認) | ✅ |
| SymbolStateEvaluator (11 状態) | ✅ |
| save_evaluations グループ化修正 | ✅ |
| MarketStateRunner 60秒周期 | ✅ |
| MARKET_STATE_INTERVAL_SEC / WATCHED_SYMBOLS 設定追加 | ✅ |
| GET /api/v1/market-state/symbols/{ticker} API | ✅ |
| テスト 52 件追加 (全 225 件通過) | ✅ |
| Phase 5 補強: リグレッションテスト 16 件追加 (全 241 件通過) | ✅ |

**Phase 5 は全完了条件を満たした。Phase 6 に着手可能。**

### Phase 5 補強 ✅ (実装日: 2026-03-16)

**save_evaluations グループ化バグ再発防止・失敗分離・WATCHED_SYMBOLS 挙動の明示テスト化**

| 補強内容 | 詳細 |
|---|---|
| save_evaluations グループ化バグ (旧: Phase 4→5 修正済み) | for-result ループで soft-expire すると同一 (layer, target_type, target_code) の 2件目以降が 1件目を消した。グループ化で修正済み。本テストで再発防止固定。 |
| Engine 評価 Evaluator 失敗分離 | 1 Evaluator が RuntimeError を raise しても他 Evaluator の結果は DB に保存される |
| SymbolStateEvaluator ticker 単位失敗分離 | 1 ticker のデータが不正でも他 ticker の評価は継続される |
| WATCHED_SYMBOLS 未設定時の Runner 挙動 | 空文字列 → watched=[] → symbol_data={} → SymbolStateEvaluator は [] を返すのみで例外なし |
| WATCHED_SYMBOLS 空時ログ最小化 | `_warned_empty_symbols` フラグで初回のみ info、以降はサイレント |
| リグレッションテスト | `tests/test_phase5_regression.py` 16件追加 (全 241 件通過) |

### Phase 6 ✅ Strategy Engine Phase 1 (実装日: 2026-03-16)

**Strategy Engine — 評価して返すだけ。発注しない。**

| 実装内容 | 詳細 |
|---|---|
| DB (migration 007) | strategy_definitions / strategy_conditions / strategy_evaluations 追加 |
| StrategyDefinition モデル | strategy_code / direction / priority / is_enabled / max_size_ratio |
| StrategyCondition モデル | condition_type (required/forbidden/size_modifier) / layer / state_code / operator / size_modifier |
| StrategyEvaluation モデル | 判定ログ APPEND ONLY。matched/missing/blocking/evidence を全保存 |
| StrategyEvaluator | 純粋関数。DB アクセスなし。required/forbidden/size_modifier を評価 |
| StrategyEngine | snapshot 取得 → safety check → 評価 → DB 保存。発注禁止。 |
| snapshot safety-first | market/time_window 未存在→blocked、symbol 未存在 (ticker 評価時)→blocked、stale→blocked |
| stale 検出 | STRATEGY_MAX_STATE_AGE_SEC=180 秒。snapshot.updated_at が古い場合 "state_snapshot_stale:{layer}" |
| Config 追加 | `STRATEGY_MAX_STATE_AGE_SEC=180` |
| Seed | `long_morning_trend` / `short_risk_off_rebound` 2 strategy (seed.py で べき等 seed) |
| API 追加 (3本) | GET /current / GET /symbols/{ticker} / POST /recalculate |
| テスト | 36 件追加 (全 277 件通過) |

### Phase 6 補強 ✅ (実装日: 2026-03-16)

**運用明確化 + size_ratio=0 安全チェック追加**

| 補強内容 | 詳細 |
|---|---|
| `__init__.py` パス確認 | `trade_app/services/strategy/__init__.py` が正しく存在することを確認（表記ゆれなし） |
| GET /current の symbol 条件挙動の明文化 | ticker=None 評価では layer=symbol の条件は常に missing_required_state になる（設計仕様。symbol 評価は GET /symbols/{ticker} で行うこと） |
| size_ratio=0 安全チェック追加 | `size_modifier=0.0` や `max_size_ratio=0.0` → `size_ratio=0.0` → `entry_allowed=False` + `"size_ratio_zero"` を blocking_reasons に追加（Signal Router 接続時の曖昧さを排除） |
| stale 判定基準時刻の明文化 | stale 判定は `engine.run()` の `evaluation_time`（引数）を基準にする。API 呼び出し時刻ではない。`snapshot.updated_at` と `evaluation_time` の差が `STRATEGY_MAX_STATE_AGE_SEC` を超えると stale |
| テスト 8 件追加 | size_ratio=0(4件) / GET /current symbol 挙動(2件) / stale 基準時刻(2件) → 全 285 件通過 |

### Phase 6 完了判定 ✅ (2026-03-16 確認)

| 完了条件 | 状態 |
|---|---|
| migration 007 (3テーブル + index) | ✅ |
| StrategyDefinition / Condition / Evaluation モデル | ✅ |
| StrategyEvaluator (required/forbidden/size_modifier) | ✅ |
| StrategyEngine (safety-first + 評価 + 保存) | ✅ |
| snapshot missing/stale → entry_allowed=False | ✅ |
| size_ratio=0 → entry_allowed=False + "size_ratio_zero" | ✅ |
| GET /current の symbol 条件挙動が明文化されている | ✅ |
| stale 判定基準 = engine evaluation_time (API 時刻ではない) | ✅ |
| STRATEGY_MAX_STATE_AGE_SEC config 追加 | ✅ |
| Seed 2 策略 (long_morning_trend / short_risk_off_rebound) | ✅ |
| GET /current / GET /symbols/{ticker} / POST /recalculate API | ✅ |
| Strategy Engine 発注禁止設計 (OrderRouter 等に依存しない) | ✅ |
| テスト 44 件追加 (全 285 件通過) | ✅ |

**Phase 6 は全完了条件を満たした。Phase 7 に着手可能。**

### Phase 7 ✅ Strategy Runner + Decision Gateway Phase 1 (実装日: 2026-03-16)

**StrategyRunner 定期実行 + current_strategy_decisions 正本テーブル**

| 実装内容 | 詳細 |
|---|---|
| DB (migration 008) | `current_strategy_decisions` テーブル追加 (strategy_id, ticker) 単位の正本 |
| CurrentStrategyDecision モデル | strategy_code 非正規化・JSON 全説明カラム・evaluation_time + updated_at |
| Config 追加 | `STRATEGY_RUNNER_INTERVAL_SEC=60` |
| DecisionRepository | `upsert_decisions()` (select-then-update-or-insert) / `get_latest_decisions(ticker)` / `get_history(ticker, strategy_code, limit)` |
| StrategyRunner | 60秒周期バックグラウンドタスク。global + per-ticker を独立 try/except + 独立 DB セッションで失敗分離。session_factory 注入可能（テスト用） |
| StrategyEngine 更新 | `run()` 後に `DecisionRepository.upsert_decisions()` を呼ぶ。失敗してもループ継続 |
| API 追加 (3本) | GET /latest (銘柄横断) / GET /latest/{ticker} / GET /history?ticker&strategy_code&limit |
| Admin API 追加 | POST /api/admin/strategies/init — seed 投入（べき等。自動起動なし） |
| main.py 更新 | StrategyRunner を lifespan で start/stop |
| テスト | 24 件追加 (全 309 件通過) ※ Phase 8 で 341 件、Phase 9 で 377 件に増加 |

**失敗分離設計:**
- global 評価 (ticker=None) の失敗 → per-ticker 評価に影響しない
- 1 ticker の失敗 → 他 ticker 評価に影響しない
- 各評価は独立 DB セッション使用（セッション汚染防止）
- upsert_decisions 失敗 → commit はスキップするが evaluations 保存は継続

### Phase 8 ✅ Signal Router Integration Gate (実装日: 2026-03-16)

**SignalStrategyGate — current_strategy_decisions を参照して entry signal を前段でフィルタリングする**

| 実装内容 | 詳細 |
|---|---|
| DB (migration 009) | `signal_strategy_decisions` APPEND ONLY 監査テーブル追加 |
| SignalStrategyDecision モデル | signal ごとの gate 判定結果を永続化。global_decision_id / symbol_decision_id 参照 |
| Config 追加 | `SIGNAL_MAX_DECISION_AGE_SEC=180` — decision の最大許容古さ（秒） |
| SignalStrategyGate | `check(signal)` — missing / stale / entry_allowed=False / size_ratio<=0 を安全側で reject。exit signal はバイパス |
| AuditEventType 追加 | `STRATEGY_GATE_REJECTED` を enums.py に追加 |
| pipeline.py 更新 | SignalStrategyGate → RiskManager → OrderRouter の順で実行 |
| API 追加 | `GET /api/signals/{signal_id}/strategy-decision` — gate 判定結果を照会（最新レコードを返す） |
| テスト | 32 件追加 (全 341 件通過)。既存 test_pipeline.py を gate mock 対応に修正 |

**Gate ロジック（entry signal のみ適用）:**
1. `signal.side` → `direction` 変換: `buy → long`, `sell → short`
2. global (ticker=NULL) + symbol (ticker=signal.ticker) の両方の decisions を取得
3. `evidence_json.get("direction", "both")` でフィルタ（`both` は全方向に適合）
4. missing → `decision_missing:global` / `decision_missing:symbol`
5. stale (> SIGNAL_MAX_DECISION_AGE_SEC) → `decision_stale:{layer}:{strategy_code}`
6. `entry_allowed=False` → `decision_blocked:{layer}:{strategy_code}`
   ※ `is_active` は StrategyEvaluator が常に `entry_allowed` と同値で設定するため（evaluator.py:138 `is_active=entry_allowed`）、`entry_allowed` チェックで両方を網羅する
7. `size_ratio = min(all relevant decisions)`, `size_ratio <= 0` → `size_ratio_zero`
8. 判定結果（pass / reject 両方）を `signal_strategy_decisions` に INSERT

**設計制約（禁止）:**
- BrokerAdapter / OrderRouter / PositionManager を直接呼ばない
- RiskManager を置き換えない（前段ゲートとして動く）
- strategy decision だけで発注可否を最終決定しない

**残存技術的負債:**
- `signal_strategy_decisions.global_decision_id` / `symbol_decision_id` は同一 layer で複数 strategy が decision を持つ場合、先頭 (`[0]`) の ID のみを記録する。全 decision の詳細は `evidence_json` に保存されるため機能的影響はないが、FK 参照として不完全な点に注意。将来的には `global_decision_ids: JSONB` へのスキーマ変更で解消できる。

### Phase 9 ✅ Signal Planning Layer (実装日: 2026-03-16)

**SignalPlanningService — Gate 通過後のサイズ調整・執行パラメータ計画層**

| 実装内容 | 詳細 |
|---|---|
| DB (migration 010) | `signal_plans` + `signal_plan_reasons` APPEND ONLY 監査テーブル追加 |
| SignalPlan モデル | planning_status, planned_order_qty, execution params 候補, planning_trace_json を保存 |
| SignalPlanReason モデル | 縮小・拒否理由を個別レコードで保存（APPEND ONLY） |
| PlanningReasonCode | 11 rejectコード + 5 reduction コード（`reasons.py`） |
| PlannerContext | 全入力を一箇所に集約。市場データは optional で安全デフォルト |
| PlannerContextBuilder | DB から最新 SignalStrategyDecision を取得して context 構築 |
| BaseSizer | size_ratio 適用（clamp 0〜1）+ lot 丸め（切り捨て） |
| LiquidityAdjuster | volume_ratio < 0.3 → 50%, < 0.1 → 25% 縮小 |
| SpreadAdjuster | spread_bps >= 100 → reject, >= 50 → 50% 縮小 |
| VolatilityAdjuster | ATR/price > 3% → 50% 縮小, volatility > 4% → 50% 縮小 |
| ExecutionParamsBuilder | market → slippage_bps=30, low liquidity → participation_cap=5%, timeout=300秒 |
| SignalPlanningService | 10-step オーケストレーター。reject は SignalPlanRejectedError を送出 |
| risk_manager.py 更新 | `check(signal, planned_qty=None)` — planned_qty で発注金額計算 |
| order_router.py 更新 | `route(signal, planned_qty=None)` — planned_qty で Order.quantity を設定 |
| pipeline.py 更新 | Gate → Planning → Risk → Order の順序確立 |
| テスト | 36 件追加 (全 377 件通過) |

**Pipeline フロー（Phase 9 以降）:**
```
SignalStrategyGate.check(signal) → gate_result.size_ratio
  → PlannerContextBuilder.build(signal, size_ratio=gate_result.size_ratio)
    → SignalPlanningService.plan(signal, ctx)
      → RiskManager.check(signal, planned_qty=plan.planned_order_qty)
        → OrderRouter.route(signal, planned_qty=plan.planned_order_qty)
```

**Planning ステップ（entry signal のみ。exit はバイパス）:**
1. Decision 検証 (DECISION_MISSING / DECISION_STALE)
2. Base size + size_ratio 適用
3. Market/Symbol tradability チェック
4. Liquidity 調整
5. Spread 調整
6. Volatility/ATR 調整
7. Lot 丸め（切り捨て）
8. Zero check (PLANNED_SIZE_ZERO)
9. ExecutionParams 生成
10. ACCEPTED / REDUCED / REJECTED 判定 → DB 保存

**設計制約（Phase 9 全体）:**
- BrokerAdapter / OrderRouter / PositionManager を直接呼ばない
- OrderRouter / RiskManager を置き換えない（前段計画層として動く）
- サイズ増量は行わない（size_ratio > 1.0 は 1.0 にクランプ）
- Phase 9 では市場データ (spread_bps / volume_ratio / ATR / volatility) は optional。Phase 10 で実データ接続予定

### Phase 10 ❌ 未着手

- TachibanaBrokerAdapter 実装（`get_market_price()` 含む）
- 立花証券 e_api 認証・セッション管理
- Planning Layer への実市場データ注入
- 本番環境設定・監視

---

## 今回の作業サマリー (Phase 9)

### 実装したこと

1. **Signal Planning Layer**: Gate 通過後のサイズ調整・執行パラメータ計画層を新設。BrokerAdapter / OrderRouter には依存しない。
2. **10-step オーケストレーター**: Decision 検証 → Base size → Tradability → Liquidity → Spread → Volatility → Lot rounding → Zero check → Execution params → Status determination。各 step が AdjustmentResult を返しトレースを蓄積。
3. **APPEND ONLY 監査**: `signal_plans` + `signal_plan_reasons` に全決定・全縮小理由を記録。reject 時も保存してから例外送出。
4. **risk_manager / order_router 更新**: `planned_qty: int | None = None` を追加。None の場合は `signal.quantity` を使用（後方互換性維持）。
5. **pipeline 統合**: Gate → Planning → Risk → Order の明確な責務分離。Planning reject も `signal.status=REJECTED` + audit log で記録。
6. **テスト修正**: `test_pipeline.py` の 4 件と `test_signal_strategy_gate.py` の 1 件に planning mock を追加。`mock_risk_check(s, planned_qty=None)` シグネチャ修正。

### 採用しなかった代替案

- **Planning で RiskManager 統合**: 単一責任原則を守るため分離を維持
- **市場データを Phase 9 で実装**: データ整備を待たずに safe defaults で先行実装し、Phase 10 で実データ接続
- **lot_size を DB から取得**: Phase 9 はデフォルト 100 株（日本株単元）固定。Phase 10 で symbol master から注入予定

---

## 今回の作業サマリー (Phase 8)

### 実装したこと

1. **SignalStrategyGate**: `current_strategy_decisions` を参照して entry signal を RiskManager の前段でフィルタリングする。exit signal はバイパス（`signal_type != "entry"` → 即 pass）。
2. **二層チェック設計**: global (ticker=NULL) と symbol (ticker 固有) の両方に方向適合 decision が存在し、どちらも fresh かつ entry_allowed=True かつ size_ratio > 0 の場合のみ通過。
3. **direction フィルタ**: `evidence_json.get("direction", "both")` を使い、signal の方向（buy=long, sell=short）と一致する decision のみを評価対象とする。キーなしは "both" 扱いで安全側。
4. **APPEND ONLY 監査テーブル**: pass / reject 両方を `signal_strategy_decisions` に記録。`GET /api/signals/{id}/strategy-decision` で照会可能。
5. **pipeline 統合**: `SignalStrategyGate.check()` → `RiskManager.check()` → `OrderRouter.route()` の順序を確立。gate reject は `signal.status=REJECTED` + audit log。
6. **既存テスト修正**: `test_pipeline.py` の 4 件は strategy decisions 未設定のため gate に弾かれた。`SignalStrategyGate.check` を mock（`_GATE_PASS`）して gate とは独立した RiskManager/OrderRouter テストを維持。
7. **`is_active` と `entry_allowed` の同値性**: `StrategyEvaluator` は `is_active=entry_allowed` と常に同値で設定する（evaluator.py:138）。よって gate が `entry_allowed` のみをチェックすれば `is_active=False → reject` の要件も自動的に充足される。
8. **`global_decision_id` の限界**: 同一 layer で複数 strategy が存在する場合、`_save_decision` は `relevant_global[0]` の ID のみを `global_decision_id` に記録する。`evidence_json` には全 strategy の codes/entry_allowed/size_ratios を配列で保存しているため監査上の情報は完全。

### 採用しなかった代替案

- **exit signal にも gate 適用**: exit は strategy 観点の方向チェック不要のためバイパス設計を維持
- **global のみで判断**: 銘柄固有リスク（低流動性等）を考慮するため symbol decision も必須とした
- **direction を strategy_definitions テーブルから JOIN**: `evidence_json` に非正規化済みの direction を利用することで JOIN コストなしに判定可能

---

## 今回の作業サマリー (Phase 7)

### 実装したこと

1. **current_strategy_decisions 正本テーブル**: strategy_evaluations（APPEND ONLY 時系列）と別に、(strategy_id, ticker) ごとの最新 decision を UPSERT で保持。取引ロジックが高速に `entry_allowed` を参照できる。
2. **DecisionRepository**: アプリケーションレベルの UPSERT（select-then-update-or-insert）を採用し、PostgreSQL の UNIQUE 制約が nullable column に使えない問題を回避しつつ SQLite テスト互換を確保。
3. **StrategyRunner の失敗分離**: global 評価と各 ticker 評価を独立した try/except + 独立 DB セッションで包む。1件の失敗でループ全体が止まらない。session_factory 注入でテストが DB なしで動く。
4. **Admin seed エンドポイント**: 自動起動を避け、明示的な `POST /api/admin/strategies/init` でのみ seed を投入。べき等設計（2回叩いても重複しない）。
5. **history API**: `GET /history?ticker&strategy_code&limit` — strategy_evaluations から時系列履歴を返す。DecisionRepository.get_history() に集約。

### 採用しなかった代替案

- **PostgreSQL ON CONFLICT による UPSERT**: nullable ticker 列に DB-level UNIQUE が使えないため select-then-update-or-insert を選択
- **StrategyRunner を MarketStateRunner に統合**: 関心事が異なるため独立したクラスとして分離を維持
- **自動起動時 seed**: 本番環境での誤 seed 防止のため admin endpoint 経由に限定

---

## 今回の作業サマリー (Phase 6)

### 実装したこと

1. **Strategy Engine**: current_state_snapshots を参照して strategy を評価。発注は一切行わない。BrokerAdapter/OrderRouter/PositionManager/RiskManager には依存しない。
2. **StrategyEvaluator (純粋関数)**: required_state/forbidden_state/size_modifier 条件を評価。複数 size_modifier が成立した場合は最小値採用（保守的）。DB アクセスなし。
3. **snapshot safety-first**: market/time_window/symbol(ticker 評価時) の snapshot が missing または stale の場合は全 strategy で entry_allowed=False。blocking_reasons に "state_snapshot_missing:{layer}" / "state_snapshot_stale:{layer}" を記録。
4. **Seed 策略**: `long_morning_trend` / `short_risk_off_rebound` を seed.py で管理（べき等、重複 INSERT なし）。
5. **説明可能性**: matched_required_states / matched_forbidden_states / missing_required_states / blocking_reasons / applied_size_modifier / evaluation_time を全件 JSON 保存。
6. **将来拡張余地**: priority / direction / max_size_ratio / operator フィールドをスキーマに組み込み済み。Phase 2 以降で GTE/LTE 演算子・競合解決に活用可能。

### 採用しなかった代替案

- **Strategy Engine から直接発注**: 設計制約違反。Signal Router 経由でのみ発注する前提を維持。
- **Snapshot を毎回 DB から再取得せず in-memory キャッシュ**: Phase 1 はシンプルに毎回 DB クエリ。Phase 2 以降でキャッシュ層を追加可能。

---

## 今回の作業サマリー (Phase 5)

### 実装したこと

1. **SymbolStateEvaluator**: 11種類の銘柄状態を評価。ctx.symbol_data が空なら即 [] を返す設計で既存テストに影響なし。
2. **save_evaluations バグ修正**: 同一 (layer, target_type, target_code) の複数状態（例: gap_up + high_volume）を保存する際、結果をグループ化してから 1 回のソフト失効 → 全件 INSERT に変更。旧コードでは 2 件目の INSERT が 1 件目を上書きする潜在バグがあった。
3. **MarketStateRunner**: ExitWatcher / OrderPoller と独立した 60 秒周期のバックグラウンドタスク。Phase 1 は symbol_data 空（時間帯・市場状態のみ評価）。WATCHED_SYMBOLS 設定でフィルタ可能。
4. **symbol インデックス**: `ix_state_eval_layer_target_time` (layer, target_code, evaluation_time DESC) を migration 006 で追加。既存インデックスとの重複なし。
5. **銘柄 API**: `GET /api/v1/market-state/symbols/{ticker}` → active_states / score / confidence / evidence_list / updated_at を返す。データなしは 404。

### 採用しなかった代替案

- **Symbol データを DB テーブルで管理**: `WATCHED_SYMBOLS` 環境変数の config 方式を選択（Phase 1 はシンプルに）。Phase 2 以降で DB 管理に切り替え可能
- **Evaluator 内で soft-expiry**: Engine 外でソフト失効するとトランザクション管理が複雑になるため Engine/Repository に一元化を維持

---

## 今回の作業サマリー (Phase 4)

### 実装したこと

1. **Market State Engine**: `EvaluationContext` を受け取る `StateEvaluator` 基底クラス + 2つの具体 Evaluator。Engine がリストを回して結果を DB 保存する疎結合設計。
2. **時間帯評価 (TimeWindowStateEvaluator)**: JST 変換で 8 時間帯を判定。東証昼休み廃止 (2024-11) を反映し `lunch_break` ゾーンを削除。
3. **市場状態評価 (MarketStateEvaluator)**: `index_change_pct` ±0.5% で `normal/volatile_up/volatile_down` を判定。Phase 1 は単純閾値のみ。
4. **StateEvaluation の APPEND ONLY 設計**: 評価ログを書き換えず、古い行を `is_active=False` にして新行を INSERT。時系列トレースが可能。
5. **CurrentStateSnapshot の UPSERT**: PostgreSQL ネイティブ ON CONFLICT を使わず select-then-insert-or-update でSQLite テスト互換を確保。
6. **インフラ修正 (テスト全件復旧)**: JSONB→JSON 置換・重複 Index 削除・foreign_keys 明示・SQLAlchemy 2.x default タイミング問題対応。これらにより Phase 1-3 の 142 件の隠れた失敗が全て解消。

### 採用しなかった代替案

- **PostgreSQL ON CONFLICT による UPSERT**: SQLite テストが壊れるため select-then-update-or-insert を選択
- **Evaluator が直接 DB に書く設計**: Engine の責務（永続化）と Evaluator の責務（判定）を分離するため Engine に集約

---

## 今回の作業サマリー (Phase 3)

### 実装したこと

1. **取引停止（halt）のDB永続化**: `trading_halts` テーブルを新設。halt 状態はDBが唯一の正本。再起動後も状態を正しく復元できる。
2. **ポジションクローズの完全フロー**: `OPEN → CLOSING（exit注文送信） → CLOSED（約定確認後）` という2段階遷移を実装。以前は価格を直接指定する即時クローズ (`close_position()`) のみだった。
3. **ExitWatcher**: `BrokerAdapter.get_market_price()` を呼び出し、TP/SL/Timeout 条件を評価。条件成立時に `initiate_exit()` を呼ぶ。Broker 差し替え時も ExitWatcher 本体の変更は不要。
4. **Execution 重複防止**: OrderPoller の fill 処理で `broker_execution_id` をDBで事前検索。既存なら Execution を作成しない（DB の UNIQUE 制約も二重安全網として維持）。
5. **連続損失 / 日次損失 halt**: `finalize_exit()` / `close_position()` 後に `check_and_halt_if_needed()` を呼び出し、閾値超過時に halt を自動発動。

### 採用しなかった代替案

- **Redis で halt 状態をキャッシュ**: 再起動時の状態消失リスクがあるためDB正本方式を採用
- **PriceSource 別インターフェース**: 現時点は `BrokerAdapter.get_market_price()` に一本化。WebSocket 等に差し替える場合は ExitWatcher の `_get_broker()` を差し替えるか、`PriceSource` Protocol として抽出する（Phase 5 以降の検討事項）

---

## 追加/変更ファイル一覧

### Phase 6 新規作成

| ファイル | 概要 |
|---|---|
| `alembic/versions/007_strategy_tables.py` | strategy_definitions / strategy_conditions / strategy_evaluations 新設 |
| `trade_app/models/strategy_definition.py` | StrategyDefinition モデル (direction/priority/is_enabled/max_size_ratio) |
| `trade_app/models/strategy_condition.py` | StrategyCondition モデル (required/forbidden/size_modifier) |
| `trade_app/models/strategy_evaluation.py` | StrategyEvaluation モデル (APPEND ONLY 判定ログ) |
| `trade_app/services/strategy/__init__.py` | パッケージ公開 |
| `trade_app/services/strategy/schemas.py` | StrategyDecisionResult dataclass |
| `trade_app/services/strategy/evaluator.py` | StrategyEvaluator (純粋関数) |
| `trade_app/services/strategy/repository.py` | StrategyRepository (DB 操作) |
| `trade_app/services/strategy/engine.py` | StrategyEngine (snapshot 取得 → safety check → 評価 → 保存) |
| `trade_app/services/strategy/seed.py` | 初期 strategy seed (long_morning_trend / short_risk_off_rebound) |
| `trade_app/routes/strategy.py` | GET /current / GET /symbols/{ticker} / POST /recalculate |
| `tests/test_strategy_engine.py` | Strategy Engine 36 件テスト |

### Phase 6 変更

| ファイル | 変更内容 |
|---|---|
| `trade_app/models/enums.py` | `StrategyDirection` / `StrategyConditionType` / `StrategyOperator` 追加 |
| `trade_app/config.py` | `STRATEGY_MAX_STATE_AGE_SEC=180` 追加 |
| `trade_app/main.py` | strategy ルート登録追加 |
| `tests/conftest.py` | Phase 6 モデル 3 件 import 追加 |

### Phase 5 新規作成

| ファイル | 概要 |
|---|---|
| `alembic/versions/006_symbol_state_index.py` | state_evaluations に ix_state_eval_layer_target_time 追加 |
| `trade_app/services/market_state/symbol_evaluator.py` | SymbolStateEvaluator (11 状態) |
| `trade_app/services/market_state/runner.py` | MarketStateRunner (60秒周期バックグラウンドタスク) |
| `tests/test_symbol_state.py` | Symbol State Engine 52 件テスト |

### Phase 5 変更

| ファイル | 変更内容 |
|---|---|
| `trade_app/config.py` | `MARKET_STATE_INTERVAL_SEC=60`, `WATCHED_SYMBOLS=""` 追加 |
| `trade_app/services/market_state/repository.py` | `save_evaluations` グループ化修正 / `get_symbol_snapshot` / `get_symbol_active_evaluations` 追加 |
| `trade_app/services/market_state/engine.py` | `SymbolStateEvaluator` をデフォルト Evaluator リストに追加 |
| `trade_app/routes/market_state.py` | `GET /api/v1/market-state/symbols/{ticker}` 追加 |
| `trade_app/main.py` | MarketStateRunner 起動・停止追加 |

### Phase 4 新規作成

| ファイル | 概要 |
|---|---|
| `alembic/versions/005_market_state_tables.py` | state_definitions / state_evaluations / current_state_snapshots 新設 |
| `trade_app/models/state_definition.py` | StateDefinition モデル |
| `trade_app/models/state_evaluation.py` | StateEvaluation モデル（APPEND ONLY） |
| `trade_app/models/current_state_snapshot.py` | CurrentStateSnapshot モデル（UPSERT） |
| `trade_app/services/market_state/__init__.py` | パッケージ公開 |
| `trade_app/services/market_state/schemas.py` | EvaluationContext / StateEvaluationResult |
| `trade_app/services/market_state/evaluator_base.py` | StateEvaluator 基底クラス |
| `trade_app/services/market_state/time_window_evaluator.py` | TimeWindowStateEvaluator |
| `trade_app/services/market_state/market_evaluator.py` | MarketStateEvaluator |
| `trade_app/services/market_state/repository.py` | DB 操作（save_evaluations / upsert_snapshot / get_current_states / get_evaluation_history） |
| `trade_app/services/market_state/engine.py` | MarketStateEngine |
| `trade_app/routes/market_state.py` | GET /api/v1/market-state/current・history |
| `tests/test_market_state.py` | 市場状態 31 件テスト |

### Phase 4 変更

| ファイル | 変更内容 |
|---|---|
| `trade_app/models/enums.py` | `StateLayer`, `StateSeverity`, `TimeWindow`, `MarketCondition` enum 追加 |
| `trade_app/main.py` | market_state router 登録 |
| `alembic/env.py` | state_definition / state_evaluation / current_state_snapshot モデル import 追加 |
| `tests/conftest.py` | 3 モデルの明示的 import 追加 / `create_all(checkfirst=True)` |
| `trade_app/models/signal.py` | JSONB→JSON / 重複 Index 削除 |
| `trade_app/models/audit_log.py` | JSONB→JSON |
| `trade_app/models/broker_request.py` | JSONB→JSON / 重複 Index 削除 |
| `trade_app/models/broker_response.py` | JSONB→JSON / 重複 Index 削除 |
| `trade_app/models/system_event.py` | JSONB→JSON |
| `trade_app/models/trading_halt.py` | JSONB→JSON |
| `trade_app/models/position_exit_transition.py` | JSONB→JSON |
| `trade_app/models/execution.py` | 重複 Index 削除 |
| `trade_app/models/order_state_transition.py` | 重複 Index 削除 |
| `trade_app/models/order.py` | `positions` relationship に `foreign_keys="Position.order_id"` 追加 |
| `trade_app/models/position.py` | `order` relationship に `foreign_keys="Position.order_id"` 追加 |

### Phase 3 新規作成

| ファイル | 概要 |
|---|---|
| `alembic/versions/003_phase3_tables.py` | trading_halts / position_exit_transitions 新設 + orders 変更 |
| `trade_app/models/trading_halt.py` | TradingHalt モデル |
| `trade_app/models/position_exit_transition.py` | PositionExitTransition モデル（APPEND ONLY） |
| `trade_app/services/exit_policies.py` | TakeProfitPolicy / StopLossPolicy / TimeStopPolicy |
| `trade_app/services/halt_manager.py` | HaltManager サービス |
| `trade_app/services/exit_watcher.py` | ExitWatcher バックグラウンドタスク |
| `trade_app/routes/admin.py` | 管理者 API ルーター |

### Phase 3 変更

| ファイル | 変更内容 |
|---|---|
| `trade_app/models/enums.py` | `HaltType`, `AuditEventType.HALT_ACTIVATED/POSITION_CLOSING`, `SystemEventType.WATCHER_START/WATCHER_ERROR/HALT_*` 追加 |
| `trade_app/models/order.py` | `signal_id` nullable 化 / `position_id` FK 追加 / `is_exit_order` bool 追加 |
| `trade_app/brokers/base.py` | `get_market_price(ticker) -> Optional[float]` 抽象メソッド追加 |
| `trade_app/brokers/mock_broker.py` | `get_market_price()` / `set_price()` / `clear_price()` 追加、`_price_overrides` dict 追加 |
| `trade_app/brokers/tachibana/adapter.py` | `get_market_price()` stub 追加（NotImplementedError） |
| `trade_app/config.py` | `CONSECUTIVE_LOSSES_STOP=3` / `EXIT_WATCHER_INTERVAL_SEC=10` 追加 |
| `trade_app/services/position_manager.py` | `initiate_exit()` / `finalize_exit()` / `update_unrealized_pnl()` 追加 |
| `trade_app/services/order_poller.py` | `_handle_filled()` を entry/exit 分岐 / Execution 重複防止 / exit注文キャンセル時の CLOSING→OPEN 巻き戻し |
| `trade_app/services/risk_manager.py` | `_check_trading_halt()` を `check()` の先頭ステップとして追加 |
| `trade_app/main.py` | ExitWatcher 起動・停止 / admin router 登録 |
| `alembic/env.py` | `trading_halt`, `position_exit_transition` モデルの import 追加 |
| `tests/conftest.py` | 全モデルの明示的 import 追加（`create_all` 漏れ防止） |

---

## DB変更内容

### migration 005 (`alembic/versions/005_market_state_tables.py`)

#### 新規テーブル: `state_definitions`

| カラム | 型 | 説明 |
|---|---|---|
| id | UUID PK | |
| layer | VARCHAR(32) NOT NULL | `time_window` / `market_condition` |
| state_code | VARCHAR(64) NOT NULL | `pre_open` / `normal` 等 |
| display_name | VARCHAR(128) | 人間可読名称 |
| severity | VARCHAR(16) NOT NULL | `info/caution/warning/critical` |
| description | TEXT | 詳細説明 |
| created_at | TIMESTAMPTZ NOT NULL | |

UNIQUE: `(layer, state_code)` / INDEX: `ix_state_definitions_layer`

#### 新規テーブル: `state_evaluations`

| カラム | 型 | 説明 |
|---|---|---|
| id | UUID PK | |
| layer | VARCHAR(32) NOT NULL | |
| target_type | VARCHAR(32) NOT NULL | `market` / `index` 等 |
| target_code | VARCHAR(64) NOT NULL | `TSE` / `N225` 等 |
| state_code | VARCHAR(64) NOT NULL | |
| is_active | BOOLEAN DEFAULT true | 最新かどうか（軟式失効） |
| evaluated_at | TIMESTAMPTZ NOT NULL | |
| valid_from | TIMESTAMPTZ NULL | |
| valid_until | TIMESTAMPTZ NULL | |
| confidence | NUMERIC(3,2) NULL | 0.00–1.00 |
| details | JSON NULL | 評価補足情報 |
| created_at | TIMESTAMPTZ NOT NULL | |

INDEX: `ix_state_evaluations_layer_target`, `ix_state_evaluations_active`, `ix_state_evaluations_evaluated_at`

#### 新規テーブル: `current_state_snapshots`

| カラム | 型 | 説明 |
|---|---|---|
| id | UUID PK | |
| layer | VARCHAR(32) NOT NULL | |
| target_type | VARCHAR(32) NOT NULL | |
| target_code | VARCHAR(64) NOT NULL | |
| state_code | VARCHAR(64) NOT NULL | |
| evaluated_at | TIMESTAMPTZ NOT NULL | |
| valid_from | TIMESTAMPTZ NULL | |
| valid_until | TIMESTAMPTZ NULL | |
| confidence | NUMERIC(3,2) NULL | |
| details | JSON NULL | |
| created_at | TIMESTAMPTZ NOT NULL | |
| updated_at | TIMESTAMPTZ NOT NULL | |

UNIQUE: `(layer, target_type, target_code)` — 1レイヤー1ターゲット1行を保証

### migration 003 (`alembic/versions/003_phase3_tables.py`)

#### 新規テーブル: `trading_halts`

| カラム | 型 | 説明 |
|---|---|---|
| id | UUID PK | |
| halt_type | VARCHAR(32) | `daily_loss` / `consecutive_losses` / `manual` |
| reason | TEXT NOT NULL | 停止理由（人間可読） |
| is_active | BOOLEAN DEFAULT true | **現在停止中かどうか**（正本） |
| activated_at | TIMESTAMPTZ NOT NULL | 停止発動時刻 |
| deactivated_at | TIMESTAMPTZ NULL | 停止解除時刻 |
| activated_by | VARCHAR(32) DEFAULT 'system' | |
| deactivated_by | VARCHAR(32) NULL | |
| details | JSONB NULL | 損失額・連続損失数等の補足 |
| created_at | TIMESTAMPTZ NOT NULL | |

INDEX: `ix_trading_halts_is_active`, `ix_trading_halts_type_active`, `ix_trading_halts_activated_at`

#### 新規テーブル: `position_exit_transitions`

| カラム | 型 | 説明 |
|---|---|---|
| id | UUID PK | |
| position_id | UUID FK → positions.id | |
| from_status | VARCHAR(16) NULL | 遷移前ステータス |
| to_status | VARCHAR(16) NOT NULL | 遷移後ステータス |
| exit_reason | VARCHAR(32) NULL | tp_hit / sl_hit / timeout / manual / signal |
| triggered_by | VARCHAR(32) DEFAULT 'system' | watcher / poller / manual_api |
| exit_order_id | UUID NULL | 対応する exit 注文 ID |
| details | JSONB NULL | 価格・PnL 等の補足 |
| created_at | TIMESTAMPTZ NOT NULL | |

INDEX: `ix_position_exit_transitions_position_id`, `ix_position_exit_transitions_position_created`

#### 変更テーブル: `orders`

| 変更 | 内容 |
|---|---|
| `signal_id` | NOT NULL → NULL 許容（exit 注文はシグナルなし） |
| `position_id` | UUID FK → positions.id を追加（NULL 許容・exit 注文用） |
| `is_exit_order` | BOOLEAN DEFAULT false を追加 |

---

## API変更内容

### 市場状態 API（`/api/v1/market-state/*`）

認証: `Authorization: Bearer <API_TOKEN>`

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/v1/market-state/current` | 全ての最新状態スナップショット一覧 |
| GET | `/api/v1/market-state/history` | 評価ログ履歴（クエリパラメータ: `layer`, `target_type`, `target_code`, `limit`） |
| GET | `/api/v1/market-state/symbols/{ticker}` | 銘柄の現在状態（active_states / score / confidence / evidence_list / updated_at） |

レスポンス形式:
```json
// GET /current
[{"layer": "time_window", "target_type": "market", "target_code": "TSE",
  "state_code": "morning_session", "evaluated_at": "...", "confidence": 1.0}]

// GET /history
[{"id": "...", "layer": "...", "state_code": "...", "is_active": true, "evaluated_at": "..."}]
```

### 管理者 API（`/api/admin/*`）

認証: `Authorization: Bearer <API_TOKEN>`（既存と同一トークン）

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/admin/halts` | アクティブ halt 一覧 |
| POST | `/api/admin/halts` | 手動 halt 発動 body: `{"reason": "..."}` |
| DELETE | `/api/admin/halts/{halt_id}` | 指定 halt 解除 |
| DELETE | `/api/admin/halts` | 全 halt 解除 |
| GET | `/api/admin/status` | システム稼働状況（halt数 + ポジション数） |
| POST | `/api/admin/positions/{position_id}/close` | 手動ポジションクローズ body: `{"exit_price": null}` |

`exit_price` 省略時は `initiate_exit()` 経由（exit 注文をブローカーへ送信）。
`exit_price` 指定時は `close_position()` で即時クローズ（緊急・テスト用）。

---

## ドメイン/ロジック変更

### ポジションのライフサイクル（Phase 3 後）

```
OPEN
  ↓ ExitWatcher が TP/SL/Timeout/Manual を検出
  ↓ PositionManager.initiate_exit()
CLOSING  ← exit 注文（is_exit_order=True）をブローカーへ送信済み
  ↓ OrderPoller が exit 注文の FILLED を検出
  ↓ PositionManager.finalize_exit()
CLOSED   ← TradeResult 記録・halt チェック実行
```

exit 注文が CANCELLED / REJECTED の場合 → OrderPoller が CLOSING → OPEN に**巻き戻し**（再 exit 可能）

### Execution 重複防止

```python
# OrderPoller._handle_entry_filled() / _handle_exit_filled()
if broker_exec_id:
    existing = await db.execute(SELECT ... WHERE broker_execution_id = ?)
    if existing:
        # 重複 → Order 状態のみ更新して Execution は作成しない
        return
```

→ ポーラー再起動・重複実行でも同一 fill の二重計上を防ぐ
→ DB の `UNIQUE(broker_execution_id)` も第2の安全網として残す

### Execution 駆動ポジション更新

ポジション数量の更新は **Order 状態ではなく Execution レコード** を基準とする。

**理由**:
- 部分約定（PARTIAL）で Order が FILLED になる前に数量が変動する
- キャンセル後に残量がある場合（partial cancel）もポジション数量に反映が必要
- ポーラー再起動・ブローカー応答遅延があっても Execution が正本

**処理フロー**:
```
Execution レコード作成
  ↓
remaining_qty = position.qty - execution.qty
  ↓
remaining_qty > 0 → CLOSING 維持（次の約定待ち）
remaining_qty == 0 → finalize_exit() → CLOSED
```

→ OrderPoller の `_handle_exit_filled()` で実装予定（現状は全量約定時のみ finalize_exit 呼び出し）

**Position 更新責務の集約（実装済み 2026-03-16）**:

Position 更新は必ず PositionManager 経由で行う。

OrderPoller / ExitWatcher / Admin API / RecoveryManager など複数コンポーネントが同一 Position に影響しうるため、Position の直接更新を各所で行ってはならない。

Position の状態変更・数量変更・平均価格変更・損益更新は PositionManager に集約する。

必要に応じて position_id 単位の排他制御または楽観ロックを導入できる構造を維持すること。

### halt チェックの優先順位

`RiskManager.check()` の実行順:
1. **取引停止チェック（halt）** ← 最優先。DBを直接参照
2. 市場時間チェック
3. 残高取得
4. ポジションサイズ
5. 同時保有上限
6. 日次損失
7. 銘柄集中
8. 未解決注文

### halt 自動発動タイミング

- `PositionManager.finalize_exit()` の末尾
- `PositionManager.close_position()` の末尾

どちらも `HaltManager.check_and_halt_if_needed()` を呼び出し:
- **日次損失**: `SUM(trade_results.pnl < 0)` ≥ `DAILY_LOSS_LIMIT_JPY`
- **連続損失**: 直近 `CONSECUTIVE_LOSSES_STOP` 件が全てマイナス

---

## 注文・ポジション ステートマシン

### 注文ステート遷移

```
created
  ↓ OrderRouter.place_order() → ブローカー送信
pending_submit
  ↓ ブローカー受付
submitted
  ↓ 部分約定
partial ──────────────────────────────→ filled（残量ゼロ）
  ↓ キャンセル受付                        ↓
cancel_pending                         （後述の CLOSED フロー）
  ↓
cancelled（残量あり partial cancel も含む）

submitted / partial
  ↓ ブローカー拒否
rejected

submitted / partial
  ↓ ステータス不明
unknown
```

### ポジションステート遷移

```
OPEN
  ↓ ExitWatcher が TP/SL/Timeout/Manual を検出
  ↓ PositionManager.initiate_exit()
CLOSING  ← exit 注文（is_exit_order=True）送信済み
  ↓ OrderPoller が exit 注文 FILLED を検出
  ↓ PositionManager.finalize_exit()
CLOSED   ← TradeResult 記録・halt チェック

CLOSING → OPEN（exit 注文が CANCELLED/REJECTED の場合、OrderPoller が巻き戻し）
```

### コンポーネント責務表

| コンポーネント | 担当 |
|---|---|
| ExitWatcher | OPEN ポジションの exit 条件評価 → `initiate_exit()` 呼び出し |
| PositionManager | `initiate_exit()` (OPEN→CLOSING + exit注文発行) / `finalize_exit()` (CLOSING→CLOSED) |
| OrderPoller | exit 注文の約定確認 → `finalize_exit()` 呼び出し / CANCELLED→ `_revert_closing_position()` |
| RiskManager | `check()` の最初で halt チェック（DB直接参照） |

### Internal OCO の注意事項

TP/SL はブローカー側のネイティブ OCO を前提とせず、アプリケーション内部ロジックで管理する。

TP 約定時は SL 側を cancel し、SL 約定時は TP 側を cancel する。

ただし cancel 遅延・約定通知遅延・部分約定により、両方が約定する可能性を排除できない。

そのため **OCO は保証機能ではなく最善努力制御**とする。

最終的な正本は Execution であり、Position 数量は Execution ベースで補正する。

---

## 重要な設計判断

### 1. halt 状態は DB 正本

- Redis キャッシュを使わず毎回 DB を読む（RiskManager.check() ごと）
- 再起動・フェイルオーバー後も halt 状態が正確に復元される
- パフォーマンスよりも**安全性を優先**

### 2. ExitWatcher は BrokerAdapter 経由で価格取得

- `BrokerAdapter.get_market_price(ticker) -> Optional[float]` を抽象メソッドとして追加
- Phase 6 で WebSocket フィード等に差し替える場合は `ExitWatcher._get_broker()` を修正するか、`PriceSource` Protocol に分離する
- 現時点では価格取得が None の場合は TP/SL をスキップ（TimeStop は価格不要のため常に発動する）

### 3. signal_id の nullable 化

- exit 注文はシグナルと無関係に ExitWatcher が生成するため NULL が必要
- 既存の entry 注文は常に signal_id を持つので後方互換性に問題なし
- `Order.is_exit_order` フラグで entry/exit を区別

### 4. CLOSING → OPEN 巻き戻し

- exit 注文が CANCELLED/REJECTED になった場合、ポジションを OPEN に戻す
- これにより ExitWatcher が次のサイクルで再 exit を試みられる
- CLOSING 中のポジションは ExitWatcher が再評価しない（二重発注防止）

---

## 未解決事項

### 要実装（Phase 6）

1. **TachibanaBrokerAdapter.get_market_price()**: 立花証券 e_api の時価照会 API を使用。仕様書受領後に実装
2. **TachibanaBrokerAdapter 本体**: `place_order`, `cancel_order`, `get_order_status`, `get_positions`, `get_balance`
3. **ExitWatcher の価格ソース分離**: 高頻度更新が必要な場合は WebSocket フィードに差し替え。`PriceSource` Protocol として抽出

### migration 確認手順

#### ✅ 実施済み (2026-03-16)

**実行方法**: `trade_app` コンテナが未起動のため、`python:3.11-slim` コンテナを `trade-system_default` ネットワークに接続して alembic を実行。

```bash
# postgres / redis コンテナを起動
docker compose up -d postgres redis

# python:3.11-slim コンテナで alembic を実行
docker run --rm \
  --network trade-system_default \
  -v /home/opc/trade-system:/app \
  -w /app \
  -e DATABASE_URL_SYNC="postgresql+psycopg2://trade:trade_secret@postgres:5432/trade_db" \
  python:3.11-slim \
  bash -c "pip install alembic psycopg2-binary sqlalchemy asyncpg pydantic pydantic-settings --quiet && alembic upgrade head"
```

**実行結果**:
```
INFO  Running upgrade  -> 001, 初回スキーマ作成（全テーブル）
INFO  Running upgrade 001 -> 002, Phase 2 テーブル追加
INFO  Running upgrade 002 -> 003, Phase 3 テーブル追加・変更
INFO  Running upgrade 003 -> 004, add remaining_qty to positions
```

#### ✅ 確認結果

| 確認項目 | 結果 |
|---|---|
| `alembic_version` = `004` | ✅ 確認済み |
| `positions.remaining_qty` (integer, nullable) | ✅ 存在確認 |
| `trading_halts` テーブル | ✅ 存在確認 |
| `position_exit_transitions` テーブル | ✅ 存在確認 |
| `orders.is_exit_order`, `orders.position_id` | ✅ 存在確認 |
| `positions` 全カラム（19列） | ✅ 確認済み |

#### 問題発生時の切り分けポイント

| 症状 | 確認コマンド | 対処 |
|---|---|---|
| `alembic upgrade` が失敗 | `alembic history` で revision 系列を確認 | `down_revision` チェーンの不整合を修正 |
| `remaining_qty` カラムなし | `\d positions` | `alembic upgrade 004` を単独実行 |
| テーブルが存在しない | `\dt` で一覧確認 | `alembic upgrade 003` から順に適用 |
| asyncpg not found | pip install asyncpg | alembic 実行環境に asyncpg が必要（database.py が import するため） |

#### 未実施の理由

本番 Docker 環境へのアクセス権がないため CI/CD または手動での確認が必要。
ローカル開発では SQLite インメモリ DB を使用するため migration は不要（テストは通る）。

---

### 技術的負債・要確認

4. **部分約定（PARTIAL）のポジション管理**: 現在は全量約定後にポジション開設。PARTIAL 状態のまま長時間放置するケースが未対処
5. **exit 注文の PARTIAL 約定処理（実装済み 2026-03-16）**:
   - `Position.remaining_qty` カラム追加（nullable, migration 004）
   - `initiate_exit()` で `remaining_qty = position.quantity` に初期化
   - `PositionManager.apply_exit_execution(executed_qty, executed_price)` で減算
   - `remaining_qty > 0` → CLOSING 維持 / `remaining_qty == 0` → `finalize_exit()` 呼び出し
   - 過剰約定（remaining_qty < 0）→ 0 にクランプ + audit log に記録
6. **unrealized_pnl の精度**: 現在価格取得が None の場合は更新されない。価格が取得できない銘柄は unrealized_pnl が stale になる
7. **halt 解除後の日付またぎ**: `DAILY_LOSS` halt を翌日に解除すべきか否かのルールが未定義
8. **.env.example 更新済み**: `CONSECUTIVE_LOSSES_STOP=5`, `EXIT_WATCHER_INTERVAL_SEC=10`（デイトレ向け）
9. **TachibanaBrokerAdapter**: 全メソッドが `NotImplementedError`。Phase 6 で e_api 仕様書受領後に実装
10. **`alembic upgrade head` 確認済み (2026-03-16)**: migration 001〜004 を Docker postgres コンテナで適用確認完了

---

## 今回の作業サマリー (2026-03-19 — I-3 実装 + 補正)

### 補正事項

#### 1. state 検証責務の最終整理

| 主体 | 責務 |
|---|---|
| フロント | `code_verifier` / `state` を sessionStorage に保存 |
| フロント | Google から `?code=xxx&state=yyy` でコールバックを受け取り、sessionStorage の state と照合（CSRF 検証）|
| フロント | 照合成功後に `{code, code_verifier, state}` をバックエンドへ POST |
| バックエンド | `state` を受け取るが DB 保存・CSRF 照合は行わない。CSRF 検証はフロント済みのため不要 |

→ `schemas/auth.py` の `GoogleOAuthCallbackRequest` コメントと `routes/auth.py` docstring に明記済み。

#### 2. Cookie Secure 判定の見直し

- `COOKIE_SECURE: bool = True`（デフォルト True）を `config.py` に追加
- `_set_session_cookie` / `_clear_session_cookie` の引数を `debug` → `cookie_secure` に変更
- 本番で Secure=true を強制できる形。DEBUG 変数に依存しない
- `.env.example` に `COOKIE_SECURE=true` のドキュメントを追加

#### 3. Google callback 失敗時ログ方針

| 失敗ケース | ログ先 | 理由 |
|---|---|---|
| ネットワーク接続失敗 | アプリログ `logger.error` | インフラ/Google 側の問題 |
| token exchange 失敗 (400) | アプリログ `logger.warning` | クライアント側の問題 |
| userinfo 取得失敗 (non-200) | アプリログ `logger.warning` | **追加（不足していた）** |
| 未登録メール / 非アクティブ (403) | 監査ログ `LOGIN_FAILURE` | セキュリティ記録必要 |

### I-3 実装内容

| 実装内容 | 詳細 |
|---|---|
| `requirements.txt` | `cryptography>=42.0.0` / `pyotp>=2.9.0` 追加 |
| `config.py` | `TOTP_ENCRYPTION_KEY: str = ""` / `COOKIE_SECURE: bool = True` 追加 |
| `.env.example` | `TOTP_ENCRYPTION_KEY=` / `COOKIE_SECURE=true` セクション追加 |
| `trade_app/admin/services/encryption.py` | `TotpEncryptor` 実装（AES-256-GCM / `gv1:` フォーマット）|
| `trade_app/admin/services/auth_guard.py` | `get_pre2fa_user` / `RequirePreAuth` 追加（Pre-2FA セッション許可）|
| `trade_app/admin/schemas/auth.py` | `TotpVerifyResponse` から `session_token` を削除（Cookie ベース設計に統一）|
| `trade_app/admin/routes/auth.py` | `POST /auth/totp/setup` / `POST /auth/totp/verify` 実装、`COOKIE_SECURE` 対応 |
| `tests/admin/test_encryption.py` | 新規作成 19 件 |
| `tests/admin/test_auth_routes.py` | `TestTotpStubs` (2件) → `TestPreAuth` (2件) + `TestTotpSetup` (5件) + `TestTotpVerify` (8件) に置き換え |

**TOTP フロー（実装確定）:**
```
OAuth callback → Pre-2FA session (is_2fa_completed=False, TTL=10分)
  → POST /auth/totp/setup (RequirePreAuth)
      → pyotp.random_base32() → TotpEncryptor.encrypt() → DB 保存 → QR URI 返却
        → POST /auth/totp/verify {session_id, totp_code} + Cookie
            → session 照合（body.session_id + Cookie 両方確認）
            → TotpEncryptor.decrypt() → pyotp.TOTP.verify()
            → session.is_2fa_completed=True, expires_at延長（SESSION_TTL_SEC=8h）
            → user.totp_enabled=True
            → Cookie max_age=SESSION_TTL_SEC に延長（同一トークン再セット）
```

**テスト**: 848 件全通過（816 → 848、+32件）

### 変更ファイル一覧（2026-03-19）

| ファイル | 変更内容 |
|---|---|
| `requirements.txt` | cryptography / pyotp 追加 |
| `.env.example` | TOTP_ENCRYPTION_KEY / COOKIE_SECURE セクション追加 |
| `trade_app/config.py` | COOKIE_SECURE / TOTP_ENCRYPTION_KEY 追加 |
| `trade_app/admin/services/encryption.py` | **新規作成** TotpEncryptor |
| `trade_app/admin/services/auth_guard.py` | get_pre2fa_user / RequirePreAuth 追加 |
| `trade_app/admin/schemas/auth.py` | TotpVerifyResponse.session_token 削除 |
| `trade_app/admin/routes/auth.py` | setup_totp / verify_totp 実装・COOKIE_SECURE 対応・userinfo ログ追加 |
| `tests/admin/test_encryption.py` | **新規作成** 19 件 |
| `tests/admin/test_auth_routes.py` | TestTotpStubs → TestPreAuth + TestTotpSetup + TestTotpVerify |

---

## 今回の作業サマリー (2026-03-18 — admin_db 分離設計整理)

### 修正した表現箇所

1. **「Phase 2 で物理分離」→「物理配置は未確定」** 全箇所で修正:
   - `trade_app/config.py`: ADMIN_DATABASE_URL コメント
   - `trade_app/admin/__init__.py`: Phase 1 単一コンテナ前提コメント
   - `trade_app/admin/database.py`: Phase 2 ボックス説明
   - `docs/admin/component_design.md` §0.6: Phase 2 ボックス
   - `docs/admin/component_design.md` §7 TODO テーブル: admin_db 物理分離行

2. **`created_by` / `updated_by` FK 再評価**（詳細は下記）

3. **`alembic_admin` version テーブル分離バグ修正** — `alembic_version_admin` テーブルを新設

### created_by / updated_by FK 再評価結果

| 観点 | 結論 |
|---|---|
| DB 制約上の実現可能性 | **Phase 1 で追加可能**。FK 先 (`ui_users`) も admin_db 内にあり、クロス DB 問題は発生しない |
| 追加のタイミング | **I-4（OAuth）完了後**。I-4 完了前は実ユーザーが `ui_users` に存在しないため FK を追加すると新規作成が失敗する |
| 結論 | 「Phase 2 固定」ではなく「I-4 完了後に migration で追加」が正しい |
| 修正した TODO | `TODO(I-1)` → `TODO(I-4)` に変更（symbol_config / notification_config / 001_admin_initial / component_design.md）|

### alembic_admin バージョンテーブル分離（バグ修正）

- **原因**: `alembic_admin/env.py` の `context.configure()` に `version_table` 未指定 → trade_db の `alembic_version` テーブルと共用 → `004` revision が admin チェーンに存在せず `FAILED: Can't locate revision` エラー
- **修正**: `version_table="alembic_version_admin"` を offline / online 両モードの `context.configure()` に追加
- **確認**: `alembic -c alembic_admin.ini upgrade head` → `Running upgrade -> a1b2c3d4e5f6` 成功
- **テーブル確認**: `ui_users`, `ui_sessions`, `ui_audit_logs`, `symbol_configs`, `notification_configs` 全5テーブル作成確認
- **バージョンテーブル**: `alembic_version_admin = a1b2c3d4e5f6` (trade_db の `alembic_version = 004` と独立)

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/config.py` | ADMIN_DATABASE_URL コメント修正（物理配置未確定） |
| `trade_app/admin/__init__.py` | Phase 1 説明コメント修正 |
| `trade_app/admin/database.py` | Phase 2 ボックスコメント修正 |
| `trade_app/admin/models/symbol_config.py` | `TODO(I-1)` → `TODO(O-4)` / `TODO(I-4)` に分離修正 |
| `trade_app/admin/models/notification_config.py` | `TODO(I-1)` → `TODO(I-4)` に修正 |
| `alembic_admin/env.py` | `version_table="alembic_version_admin"` 追加（バグ修正）|
| `alembic_admin.ini` | version_table の説明コメント追加 |
| `alembic_admin/versions/001_admin_initial.py` | created_by/updated_by コメント修正 |
| `docs/admin/component_design.md` | §0.6 / §4.4 / §7 TODO テーブルを修正 |

### テスト状況

**794 件 全通過** (`docker compose run --rm trade_app pytest -q`)

---

## 次回の推奨作業

### 実装フェーズ（I-3 / I-4 完了 — 次は Google OAuth 接続確認 または 運用 UI 実装）

**方針**: I-3（TotpEncryptor）と I-4（OAuth フロー）が完了。TOTP フロー全体が動作可能な状態。

#### 優先度 HIGH（次の実装）

1. **Google OAuth 接続確認**: `docs/admin/oauth_preconnect_checklist.md` の全チェックを完了後、`docs/admin/oauth_connection_procedure.md` の手順に従って実接続テストを実施
   - Google Cloud Console で OAuth 2.0 クライアント ID を取得
   - `.env` に `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `OAUTH_REDIRECT_URI` / `TOTP_ENCRYPTION_KEY` を設定
   - `COOKIE_SECURE=false`（ローカル開発環境）
   - `alembic_admin upgrade head` 実施済みであること（admin_db migration）
   - `ui_users` にテスト用メールアドレスを事前登録
2. **Phase 2 管理 UI 実装**: 次画面実装へ

#### I-3 実装完了 + 補正完了（2026-03-19）

| 実装済み内容 | ファイル |
|---|---|
| `TotpEncryptor` (AES-256-GCM / gv1: フォーマット) | `trade_app/admin/services/encryption.py` |
| `RequirePreAuth` 依存関数（Pre-2FA セッション許可）| `trade_app/admin/services/auth_guard.py` |
| `POST /auth/totp/setup` — TOTP シークレット生成・QR URI 返却 | `trade_app/admin/routes/auth.py` |
| `POST /auth/totp/verify` — TOTP 検証・セッション昇格・Cookie 延長 | `trade_app/admin/routes/auth.py` |
| `COOKIE_SECURE` / `TOTP_ENCRYPTION_KEY` / `TOTP_ISSUER="TradeSystem Admin"` 設定 | `trade_app/config.py` |
| テスト 32 件追加（848 件全通過）| `tests/admin/test_encryption.py` / `test_auth_routes.py` |
| `design_i4_auth_gaps.md` 補記: state 検証前提 (§2-A-1-x) / setup 再実行ポリシー (§2-B-1-x) / valid_window=1 仕様 (§2-B-2-x) | `docs/admin/design_i4_auth_gaps.md` |

#### I-4 実装完了（2026-03-18）

| 実装済み内容 | ファイル |
|---|---|
| `GET /auth/login` — code_challenge + state 受取 → authorization_url 生成 | `routes/auth.py` |
| `POST /auth/callback` — code exchange（httpx）→ Pre-2FA セッション発行 → HttpOnly Cookie | `routes/auth.py` |
| `POST /auth/logout` — セッション無効化 + Cookie クリア | `routes/auth.py` |
| `auth_guard.py` — Authorization: Bearer → Cookie 読み取りに変更 | `services/auth_guard.py` |
| `auth_guard.py` — 期限切れ時 SESSION_EXPIRED_ACCESS 監査ログ | `services/auth_guard.py` |
| CORS middleware — allow_credentials=True / ADMIN_FRONTEND_ORIGIN | `main.py` |
| config 追加 — GOOGLE_CLIENT_ID / SECRET / OAUTH_REDIRECT_URI / ADMIN_FRONTEND_ORIGIN / SESSION_TTL_SEC / PRE_2FA_SESSION_TTL_SEC | `config.py` |
| テスト — 32 件追加（816 件全通過） | `tests/admin/test_auth_routes.py` |
| `tests/` を docker-compose.yml のボリュームマウントに追加 | `docker-compose.yml` |

**OAuthCallbackRequest 確定スキーマ**: `{code, code_verifier, state}`
**code exchange**: httpx で直接 Google token endpoint に POST（外部 OAuth ライブラリ不要）
**Google userinfo**: `https://www.googleapis.com/oauth2/v2/userinfo` で email 取得

#### alembic_admin 確認済み

- `alembic -c alembic_admin.ini upgrade head` 実施済み（2026-03-18）
- `alembic_version_admin = a1b2c3d4e5f6`、全5テーブル作成確認済み
- `alembic upgrade head`（trade_db）は migration 004 が最新。007〜010 は適用未確認

#### 設計ドキュメント（2026-03-18 全確定）

| ファイル | 内容 | ステータス |
|---|---|---|
| `docs/admin/design_i3_encryption.md` | AES-256-GCM 暗号化設計書 | **設計確定** |
| `docs/admin/design_i4_auth_gaps.md` | OAuth フロー + Cookie + 事前登録 + PKCE 設計書 | **設計確定** |
| `docs/admin/design_i5_session.md` | セッション設計書（Absolute Timeout / TTL）| **設計確定** |
| `docs/admin/oauth_preconnect_checklist.md` | Google OAuth 実接続前チェックリスト（2026-03-19 作成）| **接続前確認用** |
| `docs/admin/oauth_connection_procedure.md` | 実接続テスト手順書 STEP 1〜9（2026-03-19 作成）| **接続確認用** |
| `docs/admin/auth_known_constraints.md` | Phase 1 未実装・既知制約・監査ログ一覧（2026-03-19 作成）| **参照用** |
| `docs/admin/oauth_troubleshoot.md` | トラブル切り分け表 T-01〜T-12 / 観察ポイント / 完了条件（2026-03-19 作成）| **実接続確認用** |

### 優先度中

4. **StrategyRunner の実装**: POST /recalculate の代わりに MarketStateRunner 相当の定期実行タスクを追加
5. **Signal Router との連携**: Strategy の decision result を Signal 処理フローに組み込む
6. **TimeWindowStateEvaluator の精度向上**: 祝日カレンダー（`jpholiday` ライブラリ等）連携で祝日を `closed` 扱いにする
7. **WATCHED_SYMBOLS の DB 管理**: 環境変数から DB テーブル管理に移行

### 優先度低

8. **TachibanaBrokerAdapter**: e_api 仕様書受領後に実装。`get_market_price()` の実装が ExitWatcher に直結する

---

## Strategy Engine 設計仕様（Phase 6 決定事項）

### 発注禁止設計

**StrategyEngine / StrategyEvaluator は発注しない。** 将来にわたって以下を禁止:
- BrokerAdapter の呼び出し
- OrderRouter への依存
- PositionManager の直接更新
- RiskManager のバイパス
- Signal の直接消費による注文作成

返すのは `StrategyDecisionResult` のみ。後続の Signal Router が参照する前提。

### GET /current の symbol 条件挙動

`GET /api/v1/strategies/current`（= `engine.run(ticker=None)`）では:
- `active_states_by_layer` に `"symbol"` キーが存在しない
- `layer="symbol"` の `required_state` 条件は **常に** `missing_required_state` として記録される
- 結果: symbol 条件を持つ strategy は **entry_allowed=False** になる
- これは **設計仕様**（グローバル評価では symbol 状態を参照しない）
- ticker 別評価は `GET /api/v1/strategies/symbols/{ticker}` を使用すること

### size_ratio=0 の安全ルール

`size_ratio <= 0.0` かつ `entry_allowed=True` になる状況を禁止:
- `max_size_ratio=0.0` または `size_modifier=0.0` が成立すると `size_ratio=0.0` になりうる
- このとき **entry_allowed=False** に強制し `blocking_reasons` に `"size_ratio_zero"` を追加
- Signal Router との接続時に発注サイズ 0 で entry が通ることを防ぐ

### stale 判定基準時刻

stale 判定は **`engine.run()` の `evaluation_time` 引数** を基準に行う:
```
age_sec = (evaluation_time - snapshot.updated_at).total_seconds()
if age_sec > STRATEGY_MAX_STATE_AGE_SEC:
    → "state_snapshot_stale:{layer}"
```
- `evaluation_time=None` の場合は `datetime.now(UTC)` を使用
- API 呼び出し時刻ではなく **engine 実行時刻** が基準
- これにより過去データを使ったバックテストでも stale 判定が正確に動作する

---

## コード監査記録 (2026-03-16)

### 実装記録 (2026-03-16): exit PARTIAL 約定 + RecoveryManager exit FILLED

#### exit PARTIAL 約定対応

- `trade_app/models/position.py`: `remaining_qty: Mapped[int | None]` 追加
- `alembic/versions/004_add_remaining_qty.py`: migration 004 追加
- `PositionManager.apply_exit_execution()`: Execution 駆動の remaining_qty 減算・過剰約定クランプ
- `PositionManager.initiate_exit()`: `remaining_qty = position.quantity` で初期化
- `OrderPoller._handle_exit_filled()`: delta 計算 + `apply_exit_execution()` → フラット時のみ `finalize_exit()`
- `OrderPoller._handle_partial()`: exit 注文の PARTIAL に `apply_exit_execution()` 追加

#### RecoveryManager exit FILLED 対応

- `RecoveryManager._reconcile_order()` の FILLED ブランチに exit/entry 分岐追加
- exit 注文 FILLED 時: `apply_exit_execution()` + フラット時 `finalize_exit()`
- Execution 重複防止チェック（broker_execution_id）を recovery にも適用

#### テスト追加

- `tests/test_position_exit_flow.py` 新規作成:
  - PARTIAL 30株 → remaining_qty=70 / CLOSING 維持
  - 2段階(30+70)でフラット → CLOSED
  - 過剰約定(110株) → remaining_qty=0 にクランプ
  - OrderPoller PARTIAL/FILLED 連携テスト
  - RecoveryManager UNKNOWN→FILLED exit クローズ
  - PARTIAL済み→FILLED で Execution 二重なし
  - broker_execution_id 重複で Execution 増加なし

### 実装記録 (2026-03-16): Phase 3 品質完了テスト追加 + .env.example 更新

#### test_exit_policies.py (32件)

- `TestTakeProfitPolicy` (10件): BUY/SELL 両方向の境界値 / tp_price None / 価格 None / exit_reason / name
- `TestStopLossPolicy` (10件): BUY/SELL 両方向の境界値 / sl_price None / 価格 None / exit_reason / name
- `TestTimeStopPolicy` (8件): 過去 deadline / 過去1時間 / 未来 deadline / deadline None / 価格 None / naive datetime / exit_reason / name
- `TestDefaultExitPolicies` (4件): ポリシー数 / 評価順序 / TP/SL 同時成立 / 価格 None で TimeStop のみ

#### test_halt_manager.py (20件)

- `TestManualHalt` (7件): activate / is_halted True / is_halted False / deactivate / deactivate_nonexistent / deactivate_all / get_active_halts
- `TestHaltDuplicatePrevention` (2件): 同一種別重複なし / 異種別共存可
- `TestDailyLossHalt` (3件): 上限超過で halt / 上限以下で halt なし / 上限ちょうどで halt
- `TestConsecutiveLossesHalt` (4件): N連続で halt / 途中利益で break / STOP=0で無効 / 件数不足で halt なし
- `TestInactiveHalt` (2件): 解除済みは is_halted 対象外 / 2回解除は noop
- `TestHaltDBPersistence` (2件): DB に永続化確認 / 種別ごと独立クエリ

#### test_exit_watcher.py (14件)

- `TestExitWatcherTP` (2件): TP到達→CLOSING / 未到達→OPEN維持
- `TestExitWatcherSL` (2件): SL到達→CLOSING / 未到達→OPEN維持
- `TestExitWatcherTimeStop` (2件): deadline 超過→CLOSING / 価格 None でも発火
- `TestExitWatcherSkipClosing` (2件): _watch_once で CLOSING は SELECT されない / CLOSING に evaluate しても例外握りつぶし
- `TestExitWatcherPriceNone` (2件): 価格 None で TP/SL スキップ / TimeStop は発火
- `TestExitWatcherBrokerIntegration` (2件): get_market_price が呼ばれる / 価格取得例外でクラッシュしない
- `TestExitWatcherNoDuplicateExit` (2件): CLOSING は _watch_once で除外 / exit 注文は1件のみ

#### test_order_poller.py 追加テスト（TestOrderPollerExitOrders: 9件）

- exit PARTIAL → remaining_qty 減算 / Execution 作成
- exit FILLED → CLOSED 遷移 / Execution 作成
- exit CANCELLED → OPEN 巻き戻し
- exit REJECTED → OPEN 巻き戻し
- exit UNKNOWN → CLOSING 維持
- broker_execution_id 重複 → Execution 増加なし
- 二重ポーリング → Execution 増加なし

#### .env.example 更新（初版）

- `CONSECUTIVE_LOSSES_STOP=5` — N連続損失 halt 発動件数（0=無効）
- `EXIT_WATCHER_INTERVAL_SEC=30` — ExitWatcher ポーリング間隔（秒）（後に10秒に修正）

---

### 実装記録 (2026-03-16): ExitWatcher 間隔修正 + test_risk_manager.py halt 対応 + migration 手順整備

#### EXIT_WATCHER_INTERVAL_SEC の修正

- `config.py` のデフォルト値はすでに `10` 秒だった（変更不要）
- `.env.example` が `30` 秒になっていたため `10` 秒に修正し、コメントをデイトレ向けに改訂
- config.py コメントはデイトレ前提の説明が不足していたが既に 10 秒に設定済みのため変更なし

#### test_risk_manager.py の halt 対応修正

**修正方針**: RiskManager 単体テストとして `HaltManager.is_halted()` を `patch` で分離。
halt DB への依存を排除し、チェック順序・メッセージ内容を責務として検証する。

**変更内容**:
- `_patch_halt_not_halted` ヘルパーを定義し、全既存テストに適用（halt=False の前提を明示）
- `_make_settings` に `CONSECUTIVE_LOSSES_STOP`, `EXIT_WATCHER_INTERVAL_SEC` を追加
- `TestHaltCheck` クラスを新規追加（4件）:
  - halt=True → check() が即座に RiskRejectedError
  - halt=True → broker.get_balance() が呼ばれない（halt が最優先）
  - halt=False → 残高チェックへ進む（正常経路）
  - halt 理由がエラーメッセージに含まれる
- 既存テストをクラスに整理（TestPositionSizeCheck / TestMaxPositionsCheck / TestTickerConcentrationCheck）
- `test_ticker_concentration`: `signal_id=str(uuid.uuid4())` の不要フィールドを削除し、別銘柄は通過することを追加確認
- **合計**: 12件（旧 5件 → 新 12件）

#### migration 004 確認手順（Docker 環境）

```bash
# 1. migration 適用
docker compose exec trade_app alembic upgrade head

# 2. 適用 revision 確認
docker compose exec trade_app alembic current
# 期待出力: 004 (head)

# 3. カラム確認
docker compose exec postgres psql -U trade_user -d trade_db \
  -c "SELECT column_name, data_type, is_nullable FROM information_schema.columns \
      WHERE table_name='positions' AND column_name='remaining_qty';"
# 期待: remaining_qty | integer | YES

# 4. テーブル確認
docker compose exec postgres psql -U trade_user -d trade_db \
  -c "\dt trading_halts position_exit_transitions"
```

**未実施理由**: Docker 本番環境へのアクセス権なし。ローカルテストは SQLite を使用。

---

### 実装記録 (2026-03-16): exit CANCELLED/REJECTED 巻き戻し + entry delta Execution 統一 + weighted average

#### RecoveryManager exit CANCELLED/REJECTED → revert_to_open

- `RecoveryManager._reconcile_order()` の CANCELLED/REJECTED ブランチに exit 注文検出を追加
- exit 注文かつ Position が CLOSING 状態のとき `PositionManager.revert_to_open()` を呼び出す
- `triggered_by="recovery"` でトランジション記録に起動時リカバリである旨を残す

#### entry delta Execution 統一 (OrderPoller)

- `OrderPoller._handle_entry_filled()`: `delta_qty = total_filled - (order.filled_quantity or 0)` で差分のみ Execution 記録
- PARTIAL → FILLED の段階約定で cumulative な Execution が二重作成されるバグを修正
- exit 側 `_handle_exit_filled()` も同様 delta 方式に統一済み（Round 4 より）

#### filled_price / weighted average 設計方針

- `finalize_exit()` での exit_price 計算を `PositionManager._calc_weighted_exit_price(exit_order)` に委譲
- 算出ロジック: `Σ(price × qty) / Σqty` from Execution レコード
- Execution が存在しない場合は `order.filled_price` にフォールバック（ブローカー仕様依存）
- 加重平均により PARTIAL → FILLED の2段階約定でも正確な平均コスト取得が可能

#### テスト追加 (tests/test_recovery_manager.py — 11件)

- `TestRecoveryExitCancelledRejected` (5件):
  - CANCELLED → OPEN 巻き戻し（revert_to_open 呼び出し確認）
  - REJECTED → OPEN 巻き戻し
  - entry 注文は CANCELLED でも revert しない
  - CLOSING でないポジションは revert しない
  - PositionExitTransition (CLOSING→OPEN) が記録される
- `TestEntryDeltaExecution` (3件):
  - PARTIAL(30株) → FILLED(100株): Execution が 2 件(30+70)で合計 100
  - broker_execution_id 重複で Execution 増加なし
  - SUBMITTED → FILLED 直行: delta == full_qty で 1 件のみ
- `TestExitWeightedAverage` (3件):
  - Execution 1件のみ → exit_price = その価格
  - Execution 2件(30@2600 + 70@2620) → weighted average で算出
  - Execution なし → order.filled_price にフォールバック

---

### 監査対象ファイル

`position_manager.py`, `order_poller.py`, `exit_watcher.py`, `halt_manager.py`, `recovery_manager.py`, `routes/admin.py`, `models/position.py`, `models/order.py`

### Position 直接更新 — 発見・修正済み

**発見箇所**: `services/order_poller.py` の `_revert_closing_position()` メソッド（旧 553-556 行）

```python
# NG: Position を PositionManager 経由せず直接更新していた
position.status = PositionStatus.OPEN.value
position.exit_reason = None
position.updated_at = datetime.now(timezone.utc)
```

**修正内容**:
1. `PositionManager.revert_to_open(position, reason, triggered_by)` を新規追加
   - CLOSING 状態チェック（CLOSING 以外は ValueError）
   - PositionExitTransition の記録（CLOSING → OPEN）
   - `position.status / exit_reason / updated_at` の更新
2. `OrderPoller._revert_closing_position()` を `pos_manager.revert_to_open()` 呼び出しに変更

**残存する直接更新なし**: `admin.py` では `position.status` を READ のみ（条件チェック用途）。ExitWatcher / RecoveryManager / RiskManager は Position を直接更新しない。

### Internal OCO — 現状実装

ExitWatcher は CLOSING ポジションをスキップするため、同一ポジションに対し2つの exit 注文が同時に存在する経路がない。現時点では OCO リスクは構造的に回避されている。

Phase 4 でネイティブ OCO を使う場合は別途対応が必要。

### UNKNOWN 状態 — 現状実装

- OrderPoller: UNKNOWN 受信時に exit 注文でも `_revert_closing_position` を **呼ばない** → Position は CLOSING 維持（正しい）
- RecoveryManager: `_run_recovery()` のクエリに `OrderStatus.UNKNOWN.value` を含む → 再起動時に再照会される（正しい）
- 自動失敗化なし（正しい）

### 追加テスト

`tests/test_position_audit.py` を新規作成:
- `TestRevertToOpenViaPositionManager`: revert_to_open() の正常・遷移記録・エラーケース
- `TestUnknownExitOrderKeepsCLOSING`: UNKNOWN exit order 後 CLOSING 維持 / CANCELLED は OPEN 巻き戻し
- `TestRecoveryManagerIncludesUnknown`: UNKNOWN 注文がリカバリ照会対象かつ CLOSING 維持
- `TestOverfillPrevention`: finalize_exit / revert_to_open の前提チェック

---

## テスト状況

### 実装済みテスト (Phase 1/2)

| ファイル | 内容 | 状況 |
|---|---|---|
| `tests/test_signal_receiver.py` | 冪等性・重複検出 | ✅ |
| `tests/test_mock_broker.py` | FillBehavior シナリオ | ✅ |
| `tests/test_risk_manager.py` | 各リスクチェック + halt 優先チェック | ✅ halt=True/False mock で修正済み (12件) |
| `tests/test_order_poller.py` | FILLED/PARTIAL/STUCK 処理 | ⚠️ exit注文処理テスト未追加 |
| `tests/test_pipeline.py` | signal → order フロー | ✅ |

### Phase 3 追加テスト (コード監査・exit フロー・リカバリ・品質完了)

| ファイル | テスト数 | 内容 |
|---|---|---|
| `tests/test_position_audit.py` | 9 | revert_to_open / UNKNOWN維持 / RecoveryManager照会対象 / 前提チェック |
| `tests/test_position_exit_flow.py` | 10 | exit PARTIAL 約定 / 2段階→CLOSED / 過剰クランプ / OrderPoller連携 / Recovery exit FILLED |
| `tests/test_recovery_manager.py` | 11 | exit CANCELLED→OPEN巻き戻し / entry delta Execution / weighted average exit_price |
| `tests/test_exit_policies.py` | 32 | TP/SL/TimeStop 境界値（BUY/SELL両方向・価格None・deadline境界・DEFAULT順序） |
| `tests/test_halt_manager.py` | 20 | manual halt / 解除 / 二重防止 / daily_loss / consecutive_losses / DB永続性 |
| `tests/test_exit_watcher.py` | 14 | TP/SL/TimeStop発火 / CLOSING スキップ / 価格None / broker連携 / 二重exit防止 |
| `tests/test_order_poller.py` (追加) | +9 | exit PARTIAL/FILLED/CANCELLED/REJECTED/UNKNOWN / 重複防止 / 二重ポーリング |

**Phase 3 追加テスト合計: 106件（既存 Phase 1/2 テスト除く）**

### Phase 5 追加テスト

| ファイル | テスト数 | 内容 |
|---|---|---|
| `tests/test_symbol_state.py` | 52 | GapUp(5) / GapDown(4) / Volume(6) / Spread(3) / Trend(5) / Range(3) / Breakout(4) / Overextended(5) / Volatility(3) / Missing(3) / MultipleStates(3) / DBIntegration(4) / API(4) |
| `tests/test_phase5_regression.py` | 16 | save_evaluationsグループ化(5) / Engine失敗分離(2) / ticker単位失敗分離(3) / WATCHED_SYMBOLSパース(6) |

**Phase 5 追加テスト合計: 68件**

### Phase 6 追加テスト

| ファイル | テスト数 | 内容 |
|---|---|---|
| `tests/test_strategy_engine.py` | 44 | Evaluator(15) / SizeRatioZero(4) / CurrentSymbol(2) / StaleBasetime(2) / EngineSafety(5) / EngineEvaluation(6) / Seed(3) / API(7) |

**Phase 6 追加テスト合計: 44件**（補強 8件含む）

### Phase 4 追加テスト

| ファイル | テスト数 | 内容 |
|---|---|---|
| `tests/test_market_state.py` | 31 | TimeWindowStateEvaluator(11) / MarketStateEvaluator(8) / Engine DB操作(5) / API(7) |

**Phase 4 追加テスト合計: 31件**

### テスト総数 (2026-03-16 時点)

**285件 全通過** (`docker compose run --rm trade_app pytest -q → 285 passed in 15.02s` 確認済み)

### テスト実行環境

```bash
# Docker コンテナ内で実行
docker compose exec trade_app pytest tests/ -x -q

# ローカルでは依存パッケージ不足のため実行不可
# conftest.py は SQLite インメモリ DB を使用（PostgreSQL 不要）
```

---

## 注意事項

### 二重発注防止

- エントリー注文: OrderRouter の Redis 分散ロック (`SET NX`)
- exit 注文: ExitWatcher は `CLOSING` ポジションをスキップ（再発行しない）

### 約定重複防止

- OrderPoller: `broker_execution_id` を DB で事前検索
- `executions.broker_execution_id` に UNIQUE 制約（第2安全網）
- `broker_execution_id` が NULL の場合は重複チェックをスキップ（ブローカーがIDを返さない場合）

### halt は DB 正本

- Redis キャッシュなし
- `RiskManager.check()` の最初で毎回 `SELECT FROM trading_halts WHERE is_active=TRUE` を実行
- 手動解除は `DELETE /api/admin/halts/{halt_id}` または `DELETE /api/admin/halts`（全解除）

### broker 差し替え前提

- `BrokerAdapter` ABC の全抽象メソッドを実装すること
- Phase 3 追加: `get_market_price(ticker) -> Optional[float]`
- TachibanaBrokerAdapter は現在全メソッドが `NotImplementedError`

### recovery / reconcile との整合性

- RecoveryManager は起動時に SUBMITTED/PARTIAL/PENDING の entry 注文を再照会する
- exit 注文（`is_exit_order=True`）も SUBMITTED/PARTIAL なら同様に再照会される
- UNKNOWN になった exit 注文がある場合はポジションが CLOSING のまま残る（手動確認が必要）

### UNKNOWN 状態の扱い

unknown は異常状態ではなく、ブローカー応答欠落・通信断・照会失敗時に発生しうる**正規の注文状態**として扱う。

unknown の注文を自動的に failed 扱いしてはならない。

RecoveryManager / Reconcile によりブローカー照会を再実行し、実際の約定・取消・拒否状態を確認してから確定すること。

unknown 状態の exit 注文が存在する場合、ポジションは**安全側に倒して CLOSING 維持**または手動確認対象とする。

---

### 実装記録 (2026-03-16): Symbol State Engine Phase 5

#### 新規実装・修正

- **migration 006**: `ix_state_eval_layer_target_time` (layer, target_code, evaluation_time DESC) を追加。既存の `ix_state_evaluations_target_time` (target_type, target_code, evaluation_time DESC) は 005 で作成済み → 重複なし
- **SymbolStateEvaluator** (`symbol_evaluator.py`): 11状態を同時評価。ATR比率・VWAP差分・RSI 境界値を使ったスコアリング込み
- **save_evaluations バグ修正**: 旧実装は for-result ループで毎回 soft-expire → 2件目 INSERT が 1件目を上書き。グループ化 (1回 soft-expire + 全件 INSERT) に修正
- **MarketStateRunner**: `AsyncSessionLocal` から毎回セッションを生成。60秒周期。Evaluator エラーはログのみ（ループ継続）
- **GET /api/v1/market-state/symbols/{ticker}**: snapshot + active evaluations を組み合わせて返す。データなし=404

#### テスト (tests/test_symbol_state.py — 52件)

- `TestGapUp` (5件): 2% 超・ちょうど・未満 / evidence / score スケール
- `TestGapDown` (4件): -2% 超・ちょうど・未満 / evidence
- `TestRelativeVolume` (6件): 2x・ちょうど・未満 / low_liquidity / 正常 / evidence
- `TestSpread` (3件): wide / normal / evidence
- `TestTrend` (5件): trend_up / trend_down / 混在（両方向）/ evidence
- `TestRange` (3件): no_trend+low_atr / trending / high_atr
- `TestBreakout` (4件): breakout / no_breakout_with_gap / no_breakout_without_volume / evidence
- `TestOverextended` (5件): overbought / oversold / normal / 境界値 75 / 境界値 25
- `TestVolatility` (3件): high / low / evidence
- `TestMissingData` (3件): no_symbol_data / partial / None フィールド
- `TestMultipleStates` (3件): gap+volume+rsi 同時 / target フィールド確認 / 複数銘柄
- `TestSymbolStateEngineDB` (4件): 複数状態保存 / 前回失効 / snapshot 生成 / 上書きなし
- `TestSymbolStateAPI` (4件): 404 / with_data / unauthorized / multiple_active_states

---

### 実装記録 (2026-03-16): Market State Engine Phase 1

#### Market State Engine 新規実装

- `trade_app/services/market_state/` パッケージ全体を新規作成
- `StateEvaluator` 基底クラス + `EvaluationContext` / `StateEvaluationResult` スキーマ
- `TimeWindowStateEvaluator`: JST 時間帯判定 (8 zones: pre_open / opening_auction / morning_session / closing_auction / afternoon_session / after_hours / closed)
- `MarketStateEvaluator`: index_change_pct ±0.5% 閾値 (normal / volatile_up / volatile_down)
- `MarketStateRepository`: save_evaluations (soft-expire + INSERT) / upsert_snapshot (select-then-update-or-insert) / get_current_states / get_evaluation_history
- `MarketStateEngine`: Evaluator リストを走査し Repository 経由で DB 保存 + commit
- `GET /api/v1/market-state/current` / `GET /api/v1/market-state/history` ルート追加

#### インフラ修正 (テスト 142 件の隠れた失敗を解消)

- **JSONB → sa.JSON**: 全モデルで置換。`sqlalchemy.dialects.postgresql.JSONB` は SQLite テスト環境で `Compiler can't render element of type JSONB` エラーになる
- **重複 Index 削除**: `index=True` on `mapped_column` が `ix_{table}_{col}` を自動生成するため、`__table_args__` 内の同名 `Index()` を削除（signal / execution / broker_request / broker_response / order_state_transition）
- **Order↔Position foreign_keys 明示**: Phase 3 で `Order.position_id` FK が追加されたことで 2 つの FK パスが存在 → `AmbiguousForeignKeysError`。両 relationship に `foreign_keys="Position.order_id"` を文字列形式で設定
- **SQLAlchemy 2.x default タイミング**: `default=lambda: str(uuid.uuid4())` は FLUSH 時に適用される。テスト内で `order.id` を FK として使う前に `id=str(uuid.uuid4())` を明示
- **`Position.__new__(Position)` 禁止**: `_sa_instance_state` が未設定になり属性アクセスで `AttributeError`。`test_exit_policies.py` 等を正規コンストラクタ呼び出しに変更

#### テスト追加 (tests/test_market_state.py — 31件)

- `TestTimeWindowStateEvaluator` (11件): 各時間帯境界値 / after_hours / closed 判定
- `TestMarketStateEvaluator` (8件): normal / volatile_up / volatile_down / 境界値ちょうど / context_data なし
- `TestMarketStateEngineDB` (5件): save_evaluations 軟式失効 / upsert_snapshot 更新 / get_current_states / get_evaluation_history
- `TestMarketStateAPI` (7件): GET /current 200 / GET /history 200 / layer フィルタ / limit パラメータ / 認証エラー

---

*最終更新: 2026-03-19 / Google OAuth 実接続準備完了 — `docs/admin/oauth_troubleshoot.md`（T-01〜T-12 切り分け表・観察ポイント一覧・完了条件 C-1〜C-17）新規作成 / `auth.py` token exchange エラーログ改善（Google error コード・error_description を個別抽出）/ `main.py` 起動時に `管理画面設定: Google OAuth=設定済み/未設定 TOTP暗号化=設定済み/未設定` をログ出力。テスト 848 件全通過*

---

## 今回の作業サマリー (2026-03-20 — 止血・workers=1・ORM型修正・恒久対策完了)

### 実施内容

#### 1. Dockerfile --workers 2 → --workers 1

- 二重起動による重複 INSERT が本番で再現する実装上の問題として確定
- CMD を `--workers 2` → `--workers 1` に変更
- 実測: `docker compose top` でプロセス2本（sh ラッパー + uvicorn 本体）のみ確認

#### 2. scalar_one_or_none() 止血修正（4箇所）

workers >= 2 または再起動タイミングで重複行が生じた場合に `MultipleResultsFound` が発生する箇所を `.limit(2)` + warning ログ + `rows[0] if rows else None` パターンに変更。

| ファイル | メソッド |
|---|---|
| `trade_app/services/halt_manager.py` | `activate_halt()` |
| `trade_app/services/market_state/repository.py` | `upsert_snapshot()` |
| `trade_app/services/market_state/repository.py` | `get_symbol_snapshot()` |
| `trade_app/services/strategy/decision_repository.py` | `_find_existing()` |

コミット: `1d2cf61`

#### 3. migration 012: current_strategy_decisions partial unique index

DB レベルの重複防止制約を追加（適用前に 0行・重複なしを実測確認済み）。

| インデックス名 | 定義 |
|---|---|
| `uq_csd_null_ticker` | `(strategy_id) UNIQUE WHERE ticker IS NULL` |
| `uq_csd_symbol_ticker` | `(strategy_id, ticker) UNIQUE WHERE ticker IS NOT NULL` |

`alembic current = 012 (head)` 確認済み（2026-03-20）

#### 4. seed 実行: POST /api/admin/strategies/init

- HTTP 200 / `{"message":"strategy seed 投入完了: 2 件処理","seeded":2}`
- strategy_definitions 実測: 2件（`long_morning_trend` / `short_risk_off_rebound`）

#### 5. current_strategy_decisions 正常稼働確認

StrategyRunner 初回サイクル後の実測:

```
total: 2 / ticker IS NULL: 2 / ticker IS NOT NULL: 0
null_ticker duplicates: 0 / symbol_ticker duplicates: 0
  long_morning_trend     ticker=None entry_allowed=False size_ratio=0.0
  short_risk_off_rebound ticker=None entry_allowed=False size_ratio=0.0
```

#### 6. strategy_id ORM 型不整合修正（3ファイル）

`strategy_id` カラムが `String(36)` で定義されていたため asyncpg が `::VARCHAR` キャストし PostgreSQL の UUID 型列との間で `DatatypeMismatchError` が発生。seed 実行時および StrategyRunner サイクル時にエラーとして顕在化。

| ファイル | 変更内容 |
|---|---|
| `trade_app/models/strategy_condition.py` | `String(36)` → `UUID(as_uuid=False)` |
| `trade_app/models/strategy_evaluation.py` | `String(36)` → `UUID(as_uuid=False)` |
| `trade_app/models/current_strategy_decision.py` | `String(36)` → `UUID(as_uuid=False)` |

DB スキーマ変更なし（migration 不要）。コミット: `4feedef`

#### 7. current_state_snapshots 恒久対策 ✅ 完了（2026-03-20）

**問題**: workers=2 時代に生成された旧行2件が残存し、60秒ごとに upsert_snapshot WARNING が継続出力されていた。

**重複実測結果**:

| key | 旧行 id（先頭8桁） | 旧行 updated_at |
|---|---|---|
| (market, market, NULL) | 320bf495 | 2026-03-19 14:22:13 UTC |
| (time_window, time_window, NULL) | 2bfdd952 | 2026-03-19 14:22:13 UTC |

**対処手順**:
1. 旧行2件のフル UUID を実測確認
2. 全カラム内容を記録（バックアップ代替）
3. 承認後に DELETE 実行 → 削除件数 2件確認
4. 重複ゼロ（`HAVING COUNT(*) >= 2` = 0行）・総件数2を確認
5. migration 013 を適用

**migration 013**: `alembic/versions/013_css_partial_unique_index.py`

| インデックス名 | 定義 |
|---|---|
| `uq_css_null_target` | `(layer, target_type) UNIQUE WHERE target_code IS NULL` |
| `uq_css_symbol_target` | `(layer, target_type, target_code) UNIQUE WHERE target_code IS NOT NULL` |

- `alembic current = 013 (head)` 確認済み
- pg_indexes で2本存在確認済み
- 再起動後 upsert_snapshot WARNING = **0件**（完全解消）

コミット: `3c62e46`

### strategy_evaluations 状態

- 総件数: 18件（修正後サイクルのみ。DatatypeMismatchError 発生時のロールバック残骸なし）
- `entry_allowed=False` は `after_hours` 時間帯 + `WATCHED_SYMBOLS` 未設定のため expected

### コミット一覧（2026-03-20）

| コミット | 内容 |
|---|---|
| `1d2cf61` | 止血4箇所 / workers=1 / migration 012 |
| `4feedef` | strategy_id 型修正（String → UUID）3ファイル |
| `3c62e46` | migration 013: current_state_snapshots partial unique index |

### alembic 履歴（2026-03-20 時点）

| revision | 内容 |
|---|---|
| 001〜011 | 既存（orders.cancel_requested_at まで） |
| 012 | current_strategy_decisions partial unique index |
| 013 (head) | current_state_snapshots partial unique index |

---

## 今回の作業サマリー (2026-03-24 — Phase E: overextended rule 化)

### 実施内容

#### 依存型ルールの rule 化（第1弾: overextended）

`_evaluate_symbol()` にインラインで書かれていた RSI 過熱判定を `_rule_overextended()` として module レベルに切り出し、独立 rule 構造に統一した。

**変更内容:**

| 項目 | 内容 |
|---|---|
| `_rule_overextended()` 追加 | `ticker, data, *, rsi_overbought, rsi_oversold, make` の signature |
| `_evaluate_symbol()` 修正 | インライン RSI 判定（15行）を削除し、独立 rule リストに1行追加 |
| `rsi = data.get("rsi")` 削除 | トップのデータ抽出ブロックから RSI 抽出を削除（rule 内で取得） |

**ガード:** `rsi is None` → `None`（key なしも同様）

**発火条件:**
- `rsi >= RSI_OVERBOUGHT (75.0)` → `direction="overbought"` / `score = max(0.3, min(1.0, (rsi - 75) / 15))`
- `rsi <= RSI_OVERSOLD (25.0)` → `direction="oversold"` / `score = max(0.3, min(1.0, (25 - rsi) / 15))`

**Why:** `_evaluate_symbol()` に state 固有判定が直書きされると新 state 追加のたびにメソッドが肥大化する。rule 分離により追加コストが「関数1つ + リスト1行」に圧縮された。overextended は RSI のみに依存する完全独立 rule のため今回の対象として最適。

**How to apply:** `overextended` は `wide_spread` / `price_stale` と同様に独立 rule リストから呼ばれる。遷移保存フロー（初回 INSERT / 継続 skip / 解除 soft-expire / 再発火 INSERT）は既存のまま動作。

#### テスト: `tests/test_phase_e.py` 追加（33件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleOverextendedDirect` | 20件 | `_rule_overextended()` 直接呼び出し（ガード・非発火・発火・score・evidence） |
| `TestOrchestratorOverextended` | 6件 | `evaluate()` 経由の結合確認 |
| `TestStructureOverextended` | 3件 | module レベル存在確認・インライン判定消去確認 |
| `TestOverextendedTransitions` | 4件 | `engine.run()` 経由の遷移テスト（初回/継続/解消/再発火） |

**テスト**: 983 件全通過（950 → 983、+33件）

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_overextended()` 追加・インライン RSI 判定削除・rule リストに追加 |
| `tests/test_phase_e.py` | **新規作成** 33件 |

### 現在の独立 rule 一覧

| rule 関数 | state_code | 系統 |
|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 |
| `_rule_price_stale` | `price_stale` | 鮮度系 |
| `_rule_overextended` | `overextended` | RSI 過熱系 |

依存型ルール（`_evaluate_symbol()` インライン）: `gap_up_open`, `gap_down_open`, 出来高系, トレンド系, レンジ, ボラティリティ, `breakout_candidate`

### 次フェーズ候補

1. **observability 強化** — skip 系理由（`invalid_current_price` / `no_bid` 等）を snapshot の `state_summary_json` に記録
2. **breakout_candidate の rule 化** — `is_high_volume` / `is_gap_up` / `is_gap_down` を引数で受け取る形に整理

---

## 今回の作業サマリー (2026-03-24 — Phase F: symbol_volatility_high rule 化)

### 実施内容

#### 依存型ルールの rule 化（第2弾: symbol_volatility_high）

`_evaluate_symbol()` のインライン ATR 高水準判定を `_rule_symbol_volatility_high()` として module レベルに切り出した。

**変更内容:**

| 項目 | 内容 |
|---|---|
| `_rule_symbol_volatility_high()` 追加 | `ticker, data, *, atr_ratio_high, make` の signature |
| `_evaluate_symbol()` 修正 | インライン ATR 高水準判定（8行）を削除し、独立 rule リストに1行追加 |
| `current_price` / `atr` のトップ抽出は維持 | `symbol_range`（インライン）が引き続き使用するため |

**ガード:** `current_price is None or <= 0` / `atr is None` → `None`

**発火条件:** `atr / current_price >= atr_ratio_high (0.02)` → `symbol_volatility_high`

**score:** `min(1.0, atr_ratio / 0.05)`（ATR 5% → score 1.0）

**Why:** `symbol_volatility_high` は `current_price` と `atr` のみに依存する完全独立 rule。Phase E（overextended）と同じパターンで切り出しを実施。

**How to apply:** `symbol_range`（ATR < threshold）と条件が排他的ではあるが独立した state code。両者が同時 active になることはないが rule は独立して処理される。

#### テスト: `tests/test_phase_f.py` 追加（30件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleVolatilityHighDirect` | 17件 | 直接呼び出し（ガード・非発火・発火・score・evidence） |
| `TestOrchestratorVolatilityHigh` | 6件 | `evaluate()` 経由の結合確認 |
| `TestStructureVolatilityHigh` | 3件 | module レベル存在確認・インライン判定消去確認 |
| `TestVolatilityHighTransitions` | 4件 | `engine.run()` 経由の遷移テスト（初回/継続/解消/再発火） |

**テスト**: 1013 件全通過（983 → 1013、+30件）

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_symbol_volatility_high()` 追加・インライン ATR 高水準判定削除・rule リストに追加 |
| `tests/test_phase_f.py` | **新規作成** 30件 |

### 現在の独立 rule 一覧

| rule 関数 | state_code | 系統 |
|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 |
| `_rule_price_stale` | `price_stale` | 鮮度系 |
| `_rule_overextended` | `overextended` | RSI 過熱系 |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 |

依存型ルール（`_evaluate_symbol()` インライン）: `gap_up_open`, `gap_down_open`, 出来高系, トレンド系, `symbol_range`, `breakout_candidate`

### 次フェーズ候補

1. **breakout_candidate の rule 化** — `is_high_volume` / `is_gap_up` / `is_gap_down` を引数で受け取る形に整理

---

## 今回の作業サマリー (2026-03-24 — Phase G: observability 強化)

### 実施内容

#### 各 rule の診断情報を state_summary_json に記録

4つの独立 rule に `status`（active/inactive/skipped）と主要メトリクスを返す診断サマリを追加した。

**変更内容:**

| 項目 | 内容 |
|---|---|
| `_rule_*()` 戻り値変更 | `StateEvaluationResult \| None` → `tuple[StateEvaluationResult \| None, dict[str, Any]]` |
| `EvaluationContext` 拡張 | `rule_diagnostics_by_ticker: dict[str, dict[str, dict[str, Any]]]` フィールド追加 |
| `_evaluate_symbol()` 戻り値変更 | `list[...]` → `tuple[list[...], dict[str, dict[str, Any]]]` |
| `evaluate()` 更新 | `ctx.rule_diagnostics_by_ticker[ticker] = rule_diagnostics` に書き込み |
| `engine.py` 更新 | `_update_symbol_snapshots()` で `state_summary_json["rule_diagnostics"]` に注入 |
| 既存テスト修正 | `test_phase_d〜f` の `_call_rule()` ラッパーをアンパック対応に変更 |

**診断 status:**
- `"active"` — rule が発火した
- `"inactive"` — rule を評価したが閾値未満だった
- `"skipped"` — 必要なデータが不足して評価しなかった（gate）

**Why:** rule が active でない理由（データ欠損・閾値未満）を `state_summary_json` で追えるようにすることで、strategy engine の blocked 判定やアラート設計を補完する。スキーマ大改修なし・既存挙動変更なし。

**How to apply:** `CurrentStateSnapshot.state_summary_json["rule_diagnostics"]` に 4 キーが常に存在する。ticker に対する symbol データが存在しない場合は `rule_diagnostics` はスナップショットに含まれない。

#### テスト: `tests/test_phase_g.py` 追加（27件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestWidespreadDiagnostic` | 6件 | active/inactive/skipped(×4) 診断内容 |
| `TestPriceStaleDiagnostic` | 5件 | active(×3)/inactive/skipped |
| `TestOverextendedDiagnostic` | 4件 | active overbought/oversold / inactive / skipped |
| `TestVolatilityHighDiagnostic` | 4件 | active/inactive/skipped(×2) |
| `TestAllRuleKeysPresent` | 3件 | 4 キーが常に存在 |
| `TestSnapshotDiagnostics` | 5件 | engine snapshot 統合テスト |

**テスト**: 1040 件全通過（1013 → 1040、+27件）

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/schemas.py` | `rule_diagnostics_by_ticker` フィールド追加 |
| `trade_app/services/market_state/symbol_evaluator.py` | 4 rule の戻り値を tuple に変更、`_evaluate_symbol()` / `evaluate()` 更新 |
| `trade_app/services/market_state/engine.py` | `_update_symbol_snapshots()` に `rule_diagnostics` 注入 |
| `tests/test_phase_d.py` | `_call_rule()` ラッパー追加 + 全呼び出し箇所を修正 |
| `tests/test_phase_d_plus_1.py` | `_call_rule()` をアンパック対応に変更 |
| `tests/test_phase_e.py` | `_call_rule()` をアンパック対応に変更 |
| `tests/test_phase_f.py` | `_call_rule()` をアンパック対応に変更 |
| `tests/test_phase_g.py` | **新規作成** 27件 |

---

## 今回の作業サマリー (2026-03-24 — Phase H: breakout_candidate rule 化)

### 実施内容

#### 依存型ルールの rule 化（第3弾: breakout_candidate）

`_evaluate_symbol()` のインライン breakout_candidate 判定を `_rule_breakout_candidate()` として module レベルに切り出した。
依存引数（`is_high_volume` / `is_gap_up` / `is_gap_down`）はキーワード引数として明示的に渡す形を採用し、独立 rule との一貫した構造に統合した。

**変更内容:**

| 項目 | 内容 |
|---|---|
| `_rule_breakout_candidate()` 追加 | `ticker, data, *, is_high_volume, is_gap_up, is_gap_down, make` の signature |
| `_evaluate_symbol()` 修正 | インライン breakout 判定（20行）を削除し、独立 rule ループに1行追加 |
| `test_phase_g.py` 更新 | `_RULE_KEYS` に `"breakout_candidate"` を追加（equality check のため必須）|

**ガード:** `current_price is None or <= 0` → skipped / `no_current_price` / `ma20 is None or <= 0` → skipped / `no_ma20`

**発火条件:**
- `current_price > ma20`
- AND `is_high_volume` (vol_ratio >= 2.0)
- AND NOT `is_gap_up`, NOT `is_gap_down`

**score:** `max(0.3, min(1.0, pct_above_ma20 / 0.03))`（MA20 比 3% 上 → score 1.0）

**Why:** breakout_candidate はギャップ・出来高判定に依存するが、それらの依存値を引数で受け取ることで rule 関数として独立させられる。これで `_evaluate_symbol()` のインラインロジックをすべて rule ベース構造で扱えることを確認した。

**How to apply:** 依存型 rule でも依存引数をキーワード引数として明示渡しすることで、既存の rule ループに追加するだけで同じ診断サマリ（status/metrics）が state_summary_json["rule_diagnostics"] に記録される。

#### テスト: `tests/test_phase_h.py` 追加（36件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleBreakoutCandidateDirect` | 15件 | 直接呼び出し（ガード・非発火・発火・score・evidence） |
| `TestOrchestratorBreakout` | 5件 | `evaluate()` 経由の結合確認 |
| `TestStructureBreakout` | 3件 | module レベル存在確認・インライン判定消去確認 |
| `TestBreakoutCandidateTransitions` | 4件 | `engine.run()` 経由の遷移テスト（初回/継続/解消/再発火） |
| `TestBreakoutCandidateDiagnostic` | 9件 | active/inactive/skipped 診断・rule_diagnostics キー存在確認 |

**テスト**: 1076 件全通過（1040 → 1076、+36件）

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_breakout_candidate()` 追加・インライン判定削除・rule ループに追加 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に `"breakout_candidate"` 追加・`test_all_rules_active` データ更新 |
| `tests/test_phase_h.py` | **新規作成** 36件 |

### 現在の独立 rule 一覧（Phase H 完了時点）

| rule 関数 | state_code | 系統 | 依存引数 |
|---|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 | なし |
| `_rule_price_stale` | `price_stale` | 鮮度系 | なし |
| `_rule_overextended` | `overextended` | RSI 過熱系 | なし |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 | なし |
| `_rule_breakout_candidate` | `breakout_candidate` | ブレイクアウト系 | `is_high_volume`, `is_gap_up`, `is_gap_down` |

依存型ルール（`_evaluate_symbol()` インライン）: `gap_up_open`, `gap_down_open`, 出来高系, トレンド系, `symbol_range`

### 次フェーズ候補

1. **gap / volume / trend / range の rule 化** — 残るインライン判定を順次切り出し

---

## 今回の作業サマリー (2026-03-24 — Phase I: gap_up_open / gap_down_open rule 化)

### 目的

`_evaluate_symbol()` のインライン gap 判定（上下対称）を module レベル rule に切り出し、Phase H で確立した依存引数注入パターンを再利用する。

### 変更内容

#### `_rule_gap_up_open()` / `_rule_gap_down_open()` 追加

```
signature: (ticker, data, *, gap_threshold: float, make: _MakeFn) -> tuple[StateEvaluationResult | None, dict]
```

- `current_open`, `prev_close` は関数内で `data.get()` — top extraction から削除
- guard: `no_current_open` / `no_prev_close` / `zero_prev_close` → `None, {status: "skipped"}`
- 非発火: `gap_pct < gap_threshold` (up) / `gap_pct > -gap_threshold` (down) → `None, {status: "inactive", gap_pct}`
- score: `min(1.0, gap_pct / 0.04)` (up) / `min(1.0, abs(gap_pct) / 0.04)` (down)

#### `_evaluate_symbol()` 修正

| 変更 | 内容 |
|---|---|
| `rule_diagnostics = {}` の初期化位置 | rule ループ直前 → メソッド冒頭（gap 診断をループより先に記録するため） |
| インライン gap ブロック（20行）削除 | `_rule_gap_up_open()` / `_rule_gap_down_open()` 呼び出しに置き換え |
| `current_open`, `prev_close` のトップ抽出 | 削除（rule 内部で `data.get()` する） |
| `is_gap_up` / `is_gap_down` 算出 | `_gap_up_result is not None` / `_gap_down_result is not None` |

#### `test_phase_g.py` 更新

- `_RULE_KEYS` に `"gap_up_open"`, `"gap_down_open"` を追加（7キー）
- `test_all_rules_active` を2データセット構成に変更（gap と no-gap は同時発火しないため）

#### テスト: `tests/test_phase_i.py` 追加（45件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleGapUpOpenDirect` | 12 | guard × 3 / 非発火 × 4 / 発火 × 2 / score × 2 / evidence × 1 |
| `TestRuleGapDownOpenDirect` | 11 | guard × 3 / 非発火 × 3 / 発火 × 2 / score × 2 / evidence × 2 |
| `TestOrchestratorGap` | 5 | up / down / no-gap / coexist wide_spread / mutual exclusion |
| `TestStructureGap` | 4 | module level × 2 / calls both rules / no inline current_open |
| `TestGapUpOpenTransitions` | 4 | initial insert / continuation skip / deactivation / reactivation |
| `TestGapDiagnostic` | 9 | active × 2 / inactive × 2 / skipped × 2 / both keys × 1 / active via evaluate × 2 |

### テスト結果

1121 passed（全体）/ 45 passed（test_phase_i.py）/ 既存回帰なし

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_gap_up_open()` / `_rule_gap_down_open()` 追加・インライン判定削除・`rule_diagnostics` 初期化位置変更 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に gap 2キー追加・`test_all_rules_active` 2データセット化 |
| `tests/test_phase_i.py` | **新規作成** 45件 |

### 現在の独立 rule 一覧（Phase I 完了時点）

| rule 関数 | state_code | 系統 | 依存引数 |
|---|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 | なし |
| `_rule_price_stale` | `price_stale` | 鮮度系 | なし |
| `_rule_overextended` | `overextended` | RSI 過熱系 | なし |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 | なし |
| `_rule_gap_up_open` | `gap_up_open` | ギャップ系 | `gap_threshold` |
| `_rule_gap_down_open` | `gap_down_open` | ギャップ系 | `gap_threshold` |
| `_rule_breakout_candidate` | `breakout_candidate` | ブレイクアウト系 | `is_high_volume`, `is_gap_up`, `is_gap_down` |

依存型ルール（`_evaluate_symbol()` インライン）: 出来高系, トレンド系, `symbol_range`

### 次フェーズ候補

1. **volume / trend / range の rule 化** — 残るインライン判定を順次切り出し

---

## 今回の作業サマリー (2026-03-24 — Phase J: high_relative_volume / low_liquidity rule 化)

### 目的

`_evaluate_symbol()` のインライン出来高判定を `_rule_high_relative_volume()` / `_rule_low_liquidity()` として module レベルに切り出し、Phase I で確立した依存引数注入パターンを再利用する。

### 変更内容

#### `_rule_high_relative_volume()` / `_rule_low_liquidity()` 追加

```
signature: (ticker, data, *, volume_ratio_high/low: float, make: _MakeFn) -> tuple[StateEvaluationResult | None, dict]
```

- `current_volume`, `avg_volume_same_time` は関数内で `data.get()` — top extraction から削除
- guard: `no_current_volume` / `no_avg_volume` / `zero_avg_volume` → `None, {status: "skipped"}`
- high_relative_volume 非発火: `vol_ratio < volume_ratio_high` → `None, {status: "inactive", vol_ratio}`
- low_liquidity 非発火: `vol_ratio >= volume_ratio_low` → `None, {status: "inactive", vol_ratio}`
- score: `min(1.0, vol_ratio / 4.0)` (high) / `max(0.1, 1.0 - vol_ratio / threshold)` (low)
- `high_relative_volume` と `low_liquidity` は排他的（vol_ratio が両方の閾値を同時に満たすことはない）

#### `_evaluate_symbol()` 修正

| 変更 | 内容 |
|---|---|
| インライン出来高ブロック（22行）削除 | `_rule_high_relative_volume()` / `_rule_low_liquidity()` 呼び出しに置き換え |
| `current_volume`, `avg_volume_same_time` のトップ抽出 | 削除（rule 内部で `data.get()` する） |
| `is_high_volume` 算出 | `_high_vol_result is not None` |

#### `test_phase_g.py` 更新

- `_RULE_KEYS` に `"high_relative_volume"`, `"low_liquidity"` を追加（9キー）
- `test_all_rules_active` に `high_relative_volume` / `low_liquidity` のアサーション追加（low_liquidity は別データセット）

#### テスト: `tests/test_phase_j.py` 追加（43件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleHighRelativeVolumeDirect` | 10 | guard × 3 / 非発火 × 2 / 発火 × 2 / score × 3 / evidence × 1 |
| `TestRuleLowLiquidityDirect` | 10 | guard × 3 / 非発火 × 3 / 発火 × 1 / score × 2 / evidence × 1 |
| `TestOrchestratorVolume` | 5 | high / low / no-vol / 排他 / coexist breakout |
| `TestStructureVolume` | 4 | module level × 2 / calls both rules / no inline vol_ratio |
| `TestHighRelativeVolumeTransitions` | 4 | initial insert / continuation skip / deactivation / reactivation |
| `TestVolumeDiagnostic` | 9 | active × 2 / inactive × 2 / skipped × 2 / both keys × 1 / via evaluate × 2 |

### テスト結果

1164 passed（全体）/ 43 passed（test_phase_j.py）/ 既存回帰なし

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_high_relative_volume()` / `_rule_low_liquidity()` 追加・インライン判定削除 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に volume 2キー追加・`test_all_rules_active` アサーション追加 |
| `tests/test_phase_j.py` | **新規作成** 43件 |

### 現在の独立 rule 一覧（Phase J 完了時点）

| rule 関数 | state_code | 系統 | 依存引数 |
|---|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 | なし |
| `_rule_price_stale` | `price_stale` | 鮮度系 | なし |
| `_rule_overextended` | `overextended` | RSI 過熱系 | なし |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 | なし |
| `_rule_gap_up_open` | `gap_up_open` | ギャップ系 | `gap_threshold` |
| `_rule_gap_down_open` | `gap_down_open` | ギャップ系 | `gap_threshold` |
| `_rule_high_relative_volume` | `high_relative_volume` | 出来高系 | `volume_ratio_high` |
| `_rule_low_liquidity` | `low_liquidity` | 出来高系 | `volume_ratio_low` |
| `_rule_breakout_candidate` | `breakout_candidate` | ブレイクアウト系 | `is_high_volume`, `is_gap_up`, `is_gap_down` |

依存型ルール（`_evaluate_symbol()` インライン）: トレンド系（`symbol_trend_up`, `symbol_trend_down`）, `symbol_range`

### 次フェーズ候補

1. **trend / range の rule 化** — 残るインライン判定を順次切り出し

---

## 今回の作業サマリー (2026-03-24 — Phase K: symbol_trend_up / symbol_trend_down rule 化)

### 目的

`_evaluate_symbol()` のインライントレンド判定（上下対称）を `_rule_symbol_trend_up()` / `_rule_symbol_trend_down()` として module レベルに切り出し、依存引数注入パターンを再利用する。

### 変更内容

#### `_rule_symbol_trend_up()` / `_rule_symbol_trend_down()` 追加

```
signature: (ticker, data, *, make: _MakeFn) -> tuple[StateEvaluationResult | None, dict]
```

- `current_price`, `vwap`, `ma5`, `ma20` は関数内で `data.get()` — top extraction から削除
- guard: `no_current_price` / `no_vwap` / `no_ma5` / `no_ma20` / `zero_vwap` / `zero_ma20` → skipped
- trend_up 発火: `price > vwap AND ma5 > ma20`
- trend_down 発火: `price < vwap AND ma5 < ma20`（等号は両方とも inactive）
- 混合状態（片方のみ成立）→ どちらも inactive
- score: `max(0.3, min(1.0, (vwap_diff + ma_diff) * 20))`

#### `_evaluate_symbol()` 修正

| 変更 | 内容 |
|---|---|
| インライントレンドブロック（30行）削除 | `_rule_symbol_trend_up()` / `_rule_symbol_trend_down()` 呼び出しに置き換え |
| `vwap`, `ma5`, `ma20` のトップ抽出 | 削除（rule 内部で `data.get()` する） |
| `is_trend_up` / `is_trend_down` 算出 | `_trend_up_result is not None` / `_trend_down_result is not None` |
| `current_price`, `atr` | 引き続きトップ抽出（range rule が使用） |

#### `test_phase_g.py` 更新

- `_RULE_KEYS` に `"symbol_trend_up"`, `"symbol_trend_down"` を追加（11キー）
- `test_all_rules_active` の "no gap" データに `vwap=900.0, ma5=1050.0` 追加（trend_up 発火）
- trend_down 用の別データセット追加

#### テスト: `tests/test_phase_k.py` 追加（45件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleSymbolTrendUpDirect` | 14 | guard × 6 / 非発火 × 3 / 発火 × 1 / score × 3 / evidence × 1 |
| `TestRuleSymbolTrendDownDirect` | 9 | guard × 2 / 非発火 × 4 / 発火 × 1 / score × 2 / evidence × 1 |
| `TestOrchestratorTrend` | 5 | up / down / mixed / 排他 / coexist high_vol |
| `TestStructureTrend` | 4 | module level × 2 / calls both rules / no inline vwap |
| `TestSymbolTrendUpTransitions` | 4 | initial insert / continuation skip / deactivation / reactivation |
| `TestTrendDiagnostic` | 9 | active × 2 / inactive × 2 / skipped × 2 / both keys × 1 / via evaluate × 2 |

### テスト結果

1210 passed（全体）/ 45 passed（test_phase_k.py）/ 既存回帰なし

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_symbol_trend_up()` / `_rule_symbol_trend_down()` 追加・インライン判定削除・`vwap/ma5/ma20` トップ抽出削除 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に trend 2キー追加・`test_all_rules_active` データ更新 |
| `tests/test_phase_k.py` | **新規作成** 45件 |

### 現在の独立 rule 一覧（Phase K 完了時点）

| rule 関数 | state_code | 系統 | 依存引数 |
|---|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 | なし |
| `_rule_price_stale` | `price_stale` | 鮮度系 | なし |
| `_rule_overextended` | `overextended` | RSI 過熱系 | なし |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 | なし |
| `_rule_gap_up_open` | `gap_up_open` | ギャップ系 | `gap_threshold` |
| `_rule_gap_down_open` | `gap_down_open` | ギャップ系 | `gap_threshold` |
| `_rule_high_relative_volume` | `high_relative_volume` | 出来高系 | `volume_ratio_high` |
| `_rule_low_liquidity` | `low_liquidity` | 出来高系 | `volume_ratio_low` |
| `_rule_symbol_trend_up` | `symbol_trend_up` | トレンド系 | なし（data から直接取得）|
| `_rule_symbol_trend_down` | `symbol_trend_down` | トレンド系 | なし（data から直接取得）|
| `_rule_breakout_candidate` | `breakout_candidate` | ブレイクアウト系 | `is_high_volume`, `is_gap_up`, `is_gap_down` |

依存型ルール（`_evaluate_symbol()` インライン）: `symbol_range` のみ

### 次フェーズ候補

1. **symbol_range の rule 化** — 最後のインライン判定を切り出し（`is_trend_up`, `is_trend_down` を依存引数で渡す）

---

## 今回の作業サマリー (2026-03-24 — Phase L: symbol_range rule 化・全 state 判定の rule 化完了)

### 目的

`_evaluate_symbol()` の最後のインライン state 判定 `symbol_range` を `_rule_symbol_range()` として module レベルに切り出し、evaluator の全 state 判定を rule ベースに揃える。

### 変更内容

#### `_rule_symbol_range()` 追加

```
signature: (ticker, data, *, is_trend_up, is_trend_down, atr_ratio_high, make) -> tuple[StateEvaluationResult | None, dict]
```

- `current_price`, `atr` は関数内で `data.get()` — `_evaluate_symbol()` のトップ抽出を全削除
- guard: `no_current_price`（None または <= 0）/ `no_atr` → skipped
- inactive: `is_trend_up or is_trend_down` → `{reason: "trending", is_trend_up, is_trend_down}`
- inactive: `atr_ratio >= atr_ratio_high` → `{atr_ratio}`
- score: `max(0.1, 1.0 - atr_ratio / atr_ratio_high)`（ATR が低いほど高スコア）

#### `_evaluate_symbol()` 修正

| 変更 | 内容 |
|---|---|
| インライン range ブロック（15行）削除 | `_rule_symbol_range()` を rule ループに追加 |
| `current_price`, `atr` のトップ抽出削除 | 全 rule 関数が内部で `data.get()` — `_evaluate_symbol()` に `data.get()` ゼロ |
| コメント修正 | `data.get()` という文字列がコメントに残らないよう変更（構造テストが pass するように）|

#### `test_phase_g.py` 更新

- `_RULE_KEYS` に `"symbol_range"` を追加（12キー）
- `test_all_rules_active` に `symbol_range` 用データセット追加（トレンドなし・低 ATR）

#### テスト: `tests/test_phase_l.py` 追加（32件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleSymbolRangeDirect` | 11 | guard × 3 / 非発火（依存）× 2 / 非発火（ATR）× 2 / 発火 × 1 / score × 2 / evidence × 1 |
| `TestOrchestratorRange` | 5 | range / no-range × 3 / coexist wide_spread |
| `TestStructureRange` | 4 | module level / calls rule / no inline / **no data.get()** |
| `TestSymbolRangeTransitions` | 4 | initial insert / continuation skip / deactivation / reactivation |
| `TestRangeDiagnostic` | 8 | active / inactive × 2 / skipped × 2 / key present / via evaluate × 2 |

### マイルストーン

`_evaluate_symbol()` 内の `data.get()` がゼロになった。全 12 state が `_rule_*()` 関数として独立し、`_evaluate_symbol()` は純粋な orchestrator として機能する。

### テスト結果

1242 passed（全体）/ 32 passed（test_phase_l.py）/ 既存回帰なし

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_symbol_range()` 追加・インライン判定削除・`current_price/atr` トップ抽出削除・コメント修正 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に `symbol_range` 追加・`test_all_rules_active` データ追加 |
| `tests/test_phase_l.py` | **新規作成** 32件 |

### 現在の独立 rule 一覧（Phase L 完了時点 — 全 state rule 化済み）

| rule 関数 | state_code | 系統 | 依存引数 |
|---|---|---|---|
| `_rule_wide_spread` | `wide_spread` | 価格差系 | なし |
| `_rule_price_stale` | `price_stale` | 鮮度系 | なし |
| `_rule_overextended` | `overextended` | RSI 過熱系 | なし |
| `_rule_symbol_volatility_high` | `symbol_volatility_high` | ATR ボラティリティ系 | なし |
| `_rule_gap_up_open` | `gap_up_open` | ギャップ系 | `gap_threshold` |
| `_rule_gap_down_open` | `gap_down_open` | ギャップ系 | `gap_threshold` |
| `_rule_high_relative_volume` | `high_relative_volume` | 出来高系 | `volume_ratio_high` |
| `_rule_low_liquidity` | `low_liquidity` | 出来高系 | `volume_ratio_low` |
| `_rule_symbol_trend_up` | `symbol_trend_up` | トレンド系 | なし |
| `_rule_symbol_trend_down` | `symbol_trend_down` | トレンド系 | なし |
| `_rule_symbol_range` | `symbol_range` | レンジ系 | `is_trend_up`, `is_trend_down`, `atr_ratio_high` |
| `_rule_breakout_candidate` | `breakout_candidate` | ブレイクアウト系 | `is_high_volume`, `is_gap_up`, `is_gap_down` |

`_evaluate_symbol()` インライン state 判定: **なし**（全 rule 化完了）

### 次フェーズ候補

1. **新規 rule の追加** — 新たな state コードを rule として追加する場合は `_rule_*()` 関数1つ + rule ループ1行で完結
2. **rule の閾値設定外部化** — YAML/DB 設定で閾値をサイクルごとに変更できる仕組み
3. **rule 評価の並列化** — 独立 rule を asyncio.gather で並列実行（latency 改善）

---

## 今回の作業サマリー (2026-03-24 — Phase M: rule registry 明文化・single loop 統一)

### 目的

- rule registry の明文化: 全 state code を module レベルで一覧できるようにする
- `_evaluate_symbol()` を single loop にする: rule 追加手順が「関数追加 + registry 1行 + _rules リスト 1行」で完結
- diagnostics 統一: `symbol_range` high ATR inactive に `reason: "high_atr"` を追加（trending と区別可能に）

### 変更内容

#### `_RULE_REGISTRY` / `_RULE_DEP_FLAGS` 追加（module レベル定数）

```python
_RULE_REGISTRY: tuple[str, ...] = (
    "gap_up_open", "gap_down_open",
    "high_relative_volume", "low_liquidity",
    "symbol_trend_up", "symbol_trend_down",
    "wide_spread", "price_stale", "overextended", "symbol_volatility_high",
    "symbol_range", "breakout_candidate",
)

_RULE_DEP_FLAGS: dict[str, str] = {
    "gap_up_open":          "is_gap_up",
    "gap_down_open":        "is_gap_down",
    "high_relative_volume": "is_high_volume",
    "symbol_trend_up":      "is_trend_up",
    "symbol_trend_down":    "is_trend_down",
}
```

#### `_evaluate_symbol()` を single loop + deps dict に書き換え

- 旧実装: gap/volume/trend の3ブロック（各 rule を個別に呼ぶ）+ for ループ（6 rule）
- 新実装: `deps: dict[str, bool]` + `_rules = [(state_code, lambda), ...]` + 1つの for ループ

```python
deps: dict[str, bool] = {}
_rules = [
    ("gap_up_open", lambda: _rule_gap_up_open(...)),
    ...
    ("symbol_range", lambda: _rule_symbol_range(..., is_trend_up=deps.get("is_trend_up", False), ...)),
    ("breakout_candidate", lambda: _rule_breakout_candidate(..., is_gap_up=deps.get("is_gap_up", False), ...)),
]
for _state_code, _rule_fn in _rules:
    _result, _diag = _rule_fn()
    if _result is not None: results.append(_result)
    rule_diagnostics[_state_code] = _diag
    if _state_code in _RULE_DEP_FLAGS:
        deps[_RULE_DEP_FLAGS[_state_code]] = _result is not None
```

deps dict を Python の late-binding closure で参照 → `symbol_range` / `breakout_candidate` が呼ばれる時点で deps には前 rule の結果が入っている。

#### `symbol_range` high ATR inactive diagnostic に `reason: "high_atr"` 追加

- 旧: `{"status": "inactive", "atr_ratio": ...}`
- 新: `{"status": "inactive", "reason": "high_atr", "atr_ratio": ...}`
- `reason: "trending"` との区別が可能になった

### テスト: `tests/test_phase_m.py` 追加（18件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleRegistry` | 6 | exists / is_tuple / 12 entries / unique / all codes / dep order |
| `TestRuleDepFlags` | 4 | exists / 5 entries / flag values / keys in registry |
| `TestEvaluateSymbolSingleLoop` | 5 | 12 codes in diagnostics / no data.get / deps propagation × 3 |
| `TestSymbolRangeHighAtrReason` | 3 | high_atr reason / trending not high_atr / via evaluate_symbol |

**テスト**: 1260 件全通過（1242 → 1260、+18件）

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_RULE_REGISTRY` / `_RULE_DEP_FLAGS` 追加・`symbol_range` high_atr reason 追加・`_evaluate_symbol()` single loop 書き換え |
| `tests/test_phase_m.py` | **新規作成** 18件 |

### rule 追加手順（Phase N 以降）

新しい state rule を追加する場合:
1. `_rule_新state名(ticker, data, *, params, make) -> tuple[StateEvaluationResult | None, dict]` を module レベルに追加
2. `_RULES` リストに `("state_code", lambda t, d, ev, dp, et: _rule_新state名(t, d, ...))` を1行追加（依存順を考慮）
3. 他 rule の結果に依存する場合は `_RULE_DEP_FLAGS` にフラグ名を追加し、lambda で `dp.get(flag, False)` を渡す

`_RULE_REGISTRY` は `_RULES` から自動導出されるため手動追加不要。

---

## 今回の作業サマリー (2026-03-24 — Phase N: _RULES single source 化・二重管理解消)

### 目的

Phase M で `_RULE_REGISTRY`（文字列リスト）と `_evaluate_symbol()` 内 `_rules`（lambda リスト）が二重管理になっていた。Phase N でこれを `_RULES`（module レベルの実行定義リスト）1箇所に統合した。

### 変更内容

#### `symbol_evaluator.py`

| 変更 | 内容 |
|---|---|
| `_RULE_REGISTRY` 手動定義を削除 | `_RULES` から自動導出するため不要 |
| `_RULES: list[tuple[str, Any]]` 追加 | 全 `_rule_*()` 関数定義の直後（クラス定義前）に配置。各エントリ `(state_code, caller)` |
| `_RULE_REGISTRY` 自動導出 | `tuple(code for code, _ in _RULES)` — `_RULES` と常に同期 |
| `_evaluate_symbol()` 書き換え | ローカル `_rules` リスト廃止 → `for _state_code, _caller in _RULES:` の1行に統合 |
| `_caller` シグネチャ | `(t, d, ev, dp, et)` — ticker, data, evaluator, deps, eval_time |
| クラス docstring 更新 | `_RULES` が実行定義の唯一の場所であることを明記 |

#### `_RULES` caller シグネチャ

```python
# caller: (t, d, ev, dp, et) → (StateEvaluationResult | None, diag)
_RULES: list[tuple[str, Any]] = [
    ("gap_up_open",         lambda t, d, ev, dp, et: _rule_gap_up_open(t, d, gap_threshold=ev.GAP_THRESHOLD, make=ev._make)),
    ...
    ("symbol_range",        lambda t, d, ev, dp, et: _rule_symbol_range(t, d, is_trend_up=dp.get("is_trend_up", False), ...)),
    ("breakout_candidate",  lambda t, d, ev, dp, et: _rule_breakout_candidate(t, d, is_high_volume=dp.get("is_high_volume", False), ...)),
]
_RULE_REGISTRY: tuple[str, ...] = tuple(code for code, _ in _RULES)
```

**deps は `dp` として lambda パラメータで明示渡し**。Phase M の `deps.get()` closure 依存をなくし、モジュールレベルでも安全。

#### 既存テスト修正（8件）

`_evaluate_symbol()` ソースを見ていた構造テストを `inspect.getsource(_mod)` へ切り替え:
- `test_phase_d.py::TestStructure::test_evaluate_symbol_has_no_spread_inline`
- `test_phase_e.py::TestStructureOverextended::test_evaluate_symbol_has_no_rsi_inline`
- `test_phase_f.py::TestStructureVolatilityHigh::test_evaluate_symbol_calls_rule`
- `test_phase_h.py::TestStructureBreakout::test_evaluate_symbol_calls_rule_breakout_candidate`
- `test_phase_i.py::TestStructureGap::test_evaluate_symbol_calls_gap_rules`
- `test_phase_j.py::TestStructureVolume::test_evaluate_symbol_calls_volume_rules`
- `test_phase_k.py::TestStructureTrend::test_evaluate_symbol_calls_trend_rules`
- `test_phase_l.py::TestStructureRange::test_evaluate_symbol_calls_range_rule`

### テスト: `tests/test_phase_n.py` 追加（20件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRulesList` | 5 | exists / is_list / 12 entries / (str, callable) pairs / codes match registry |
| `TestRuleRegistryDerived` | 3 | equals derived / order matches / dep providers before consumers |
| `TestEvaluateSymbolStructure` | 4 | no local _rules var / references _RULES / no data.get / 12 codes in diagnostics |
| `TestBehaviorUnchanged` | 4 | dep propagation × 3 / price_stale eval_time |
| `TestObservabilityUnchanged` | 4 | wide_spread active / high_atr reason / trending reason / all status keys |

**テスト**: 1280 件全通過（1260 → 1280、+20件）

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_RULES` 追加・`_RULE_REGISTRY` 自動導出・`_evaluate_symbol()` 単純化 |
| `tests/test_phase_n.py` | **新規作成** 20件 |
| `tests/test_phase_d.py` | 構造テストのソース検索対象を `_mod` に変更 |
| `tests/test_phase_e.py` | 同上 |
| `tests/test_phase_f.py` | 同上 |
| `tests/test_phase_h.py` | 同上 |
| `tests/test_phase_i.py` | 同上 |
| `tests/test_phase_j.py` | 同上 |
| `tests/test_phase_k.py` | 同上 |
| `tests/test_phase_l.py` | 同上 |

---

## 今回の作業サマリー (2026-03-24 — Phase O: activated state 通知連携)

### 目的

評価結果から activated かつ whitelist 内の state のみを抽出し、通知ディスパッチする経路を追加する。
既存の評価・遷移保存・observability は変更しない。

### 変更内容

#### `engine.py` に追加した定数・関数

| 定数 / 関数 | 内容 |
|---|---|
| `NOTIFIABLE_STATE_CODES: frozenset[str]` | 通知対象 state の whitelist（`wide_spread` / `price_stale` / `breakout_candidate`）|
| `extract_notification_candidates(symbol_results, evaluation_time)` | `is_new_activation=True` かつ `NOTIFIABLE_STATE_CODES` に含まれる result を payload list として返す |
| `dispatch_notifications(candidates)` | 各 payload を `logger.info("[NOTIFY] ...")` に流す。失敗は握りつぶす |

#### `engine.run()` への組み込み

`symbol_results` 確定後・`_save_symbol_transitions()` の前に以下を実行:
```python
try:
    candidates = extract_notification_candidates(symbol_results, evaluation_time)
    dispatch_notifications(candidates)
except Exception:
    pass  # 通知失敗は run 全体を失敗させない
```

#### 抽出条件

1. `is_new_activation == True`
2. `state_code ∈ NOTIFIABLE_STATE_CODES`

#### payload 構造

| キー | 全 state 共通 |
|---|---|
| `ticker` | `r.target_code` |
| `state_code` | `r.state_code` |
| `evaluation_time` | 引数の `evaluation_time` |
| `reason` | `r.evidence.get("reason")` |
| `score` | `r.score` |

state 別追加:
- `wide_spread`: `spread`, `spread_rate`, `current_price`
- `price_stale`: `last_updated`, `age_sec`, `threshold_sec`
- `breakout_candidate`: 追加なし（score のみ）

### テスト: `tests/test_phase_o.py` 追加（29件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestExtractionFilter` | 6 | activated 抽出 / continued 除外 / whitelist 外除外 / 3件同時 / 混在 / 空リスト |
| `TestPayloadRequiredKeys` | 6 | 必須キー存在 / ticker / eval_time / score / reason / reason=None |
| `TestStateSpecificPayload` | 7 | wide_spread extras / price_stale extras / breakout score / フィールド混入なし |
| `TestDispatchNotifications` | 3 | 例外握りつぶし / 継続処理 / 空リスト |
| `TestNotifiableStateCodes` | 7 | frozenset / 3件 / 各 state 存在 / 非 whitelist |

**テスト**: 1309 件全通過（1280 → 1309、+29件）

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/engine.py` | `NOTIFIABLE_STATE_CODES` / `extract_notification_candidates` / `dispatch_notifications` 追加・`run()` に通知処理組み込み |
| `tests/test_phase_o.py` | **新規作成** 29件 |

---

## 今回の作業サマリー (2026-03-24 — Phase P: quote_only rule 追加)

### 目的

「気配はあるが約定価格がない」状態を検出する `quote_only` rule を追加する。
既存 state の判定ロジック・通知対象・repository は変更しない。

### 変更内容

#### `_rule_quote_only()` 追加（`symbol_evaluator.py`）

```
signature: (ticker, data, *, make: _MakeFn) -> tuple[StateEvaluationResult | None, dict]
```

| 条件 | 結果 |
|---|---|
| `current_price is not None` | inactive / `reason: "has_last_price"` |
| `current_price is None` AND `best_bid=None` AND `best_ask=None` | inactive / `reason: "no_quotes"` |
| `current_price is None` AND (`best_bid` or `best_ask` が存在) | **active** / `score=1.0` |

evidence (active 時): `reason / current_price / best_bid / best_ask / has_bid / has_ask`

#### `_RULES` に1行追加

```python
("quote_only", lambda t, d, ev, dp, et: _rule_quote_only(t, d, make=ev._make)),
```

`_RULE_REGISTRY` は自動導出のため手動追加不要。依存フラグなし（独立 rule）。

#### 通知対象: 追加しない

`NOTIFIABLE_STATE_CODES` に `quote_only` は含めない（仕様）。

#### 既存テスト修正

| ファイル | 変更内容 |
|---|---|
| `tests/test_phase_g.py` | `_RULE_KEYS` に `"quote_only"` 追加（12 → 13 キー）|
| `tests/test_phase_m.py` | `test_has_12_entries` → `test_has_13_entries` |
| `tests/test_phase_n.py` | `test_has_12_entries` → `test_has_13_entries`、docstring 更新 |

### テスト: `tests/test_phase_p.py` 追加（29件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestRuleQuoteOnlyDirect` | 13 | active × 3 / inactive × 4 / score / evidence × 3 / diag |
| `TestOrchestratorQuoteOnly` | 4 | active / no quote_only × 2 / 共存 |
| `TestStructureQuoteOnly` | 4 | module level / registry / _RULES / count=13 |
| `TestQuoteOnlyTransitions` | 4 | initial / continued / deactivation / reactivation |
| `TestQuoteOnlyDiagnostic` | 4 | active / has_last_price / no_quotes / key 存在 |
| `TestQuoteOnlyNotification` | 1 | NOTIFIABLE_STATE_CODES に含まれない |

**テスト**: 1338 件全通過（1309 → 1338、+29件）

### 変更ファイル

| ファイル | 変更内容 |
|---|---|
| `trade_app/services/market_state/symbol_evaluator.py` | `_rule_quote_only()` 追加・`_RULES` に1行追加 |
| `tests/test_phase_p.py` | **新規作成** 29件 |
| `tests/test_phase_g.py` | `_RULE_KEYS` に `"quote_only"` 追加 |
| `tests/test_phase_m.py` | 件数チェック 12 → 13 |
| `tests/test_phase_n.py` | 件数チェック 12 → 13・docstring 更新 |

---

## stale_bid_ask shadow hard guard 観測系 — Phase W〜AE 完了記録

### 観測系の目的

stale_bid_ask を将来 hard guard に昇格させるかどうかを判断するための観測 infrastructure。
Phase W〜AD で実装し、Phase AE で凍結した。

**本番挙動への影響: なし**（hard guard は price_stale のみ。stale_bid_ask は reject しない）

### 実装フェーズ一覧

| フェーズ | 追加 stage / 機能 | テスト |
|---|---|---|
| Phase W | `shadow_hard_guard_decision` イベントを trace に記録（reject しない） | 19件 |
| Phase X | `shadow_hard_guard_assessment` 派生 entry（shadow event 集約） | 34件 |
| Phase Y | `shadow_hard_guard_review_summary` 派生 entry（promotion_readiness） | 36件 |
| Phase Z | `upsert_trace_stage` 正規化 helper / 重複防止 / 読み出し helper 統一 | 33件 |
| Phase AA | `shadow_hard_guard_promotion_metrics` 派生 entry（overlap / advisory / weight） | 37件 |
| Phase AB | `shadow_hard_guard_promotion_decision` 派生 entry（4値 provisional decision） | 29件 |
| Phase AC | `shadow_hard_guard_aggregate_review_key` 派生 entry（集計用分類キー） | 45件 |
| Phase AD | `shadow_hard_guard_aggregate_review_verdict` 派生 entry（verdict ラベル） | 34件 |
| Phase AE | 役割固定・昇格判定基準明文化・次フェーズ方針凍結（コードなし） | — |

**テスト累計**: Phase W〜AD で 267件追加（全体 1766件）

### 派生 stage 一覧（Phase AE 確定）

source events / source inputs:
- `shadow_hard_guard_decision`: shadow event 本体（Phase W）
- `execution_guard_hints`: blocking/warning reason 入力（PlannerContext）
- `advisory_guard_assessment`: advisory guard 評価（Phase U）

derived stages（再計算可能・hard guard 判定には使わない）:

| stage | 役割 | 主要フィールド |
|---|---|---|
| `shadow_hard_guard_assessment` | shadow event 集約 | has_shadow_candidate / would_reject_candidates / event_count |
| `shadow_hard_guard_review_summary` | 簡易レビュー要約 | promotion_readiness: "no_signal" / "observe" / "needs_review" |
| `shadow_hard_guard_promotion_metrics` | 昇格判断用基礎観測値 | overlaps_with_price_stale / has_advisory_guard / promotion_signal_weight |
| `shadow_hard_guard_promotion_decision` | provisional decision | decision: "no_signal" / "observe" / "hold" / "review_priority" |
| `shadow_hard_guard_aggregate_review_key` | 集計用分類キー | shadow/overlap/advisory/decision bucket / countable |
| `shadow_hard_guard_aggregate_review_verdict` | 集計結果 verdict ラベル | verdict: "insufficient_signal" / "observe_only" / "overlap_hold" / "priority_review" |

### stale_bid_ask 昇格判定基準（Phase AE 確定）

以下の観点をすべて確認してから昇格判断を行うこと。

| # | 確認観点 |
|---|---|
| 1 | `countable=True` の母数が十分あること（verdict="priority_review" / "overlap_hold" 累積件数） |
| 2 | `overlap_bucket="distinct_from_price_stale"` が一定割合あること（price_stale の代替指標でないこと） |
| 3 | `decision_bucket="review_priority"` が複数セッションにわたり継続して観測されること |
| 4 | `advisory_bucket` の分布が偏っていないこと（すべて blocking / すべて none は要解釈） |
| 5 | 誤検知懸念が強い場合は昇格しないこと |
| 6 | 本番 reject 影響の事前評価が完了するまでは observe 継続可能 |

レビュー結論ラベル（運用概念。現時点では trace には追加しない）:
- `promote_candidate`: 昇格条件をすべて満たした
- `hold_observation`: 観測継続（evidence 不足 / 誤検知懸念あり）
- `insufficient_evidence`: 母数不足でレビュー判断不能

### 今後の方針（Phase AE 凍結）

**Phase AE をもって shadow hard guard 観測系の実装を完了とする。**

次にやること:
- 観測データを集計し昇格判定基準を確認する（**review フェーズ**）
- 昇格判断が出た場合のみ stale_bid_ask を hard guard 化する

次にやらないこと:
- 新たな derived stage の追加（原則禁止）
- reject ロジックの変更（昇格判断確定まで禁止）
- planning_trace_json の構造変更

---

*最終更新: 2026-03-26 / Phase AE — stale_bid_ask 観測系完了・昇格判定基準凍結 / テスト 1766 件全通過*

---

## stale_bid_ask shadow hard guard — review report template（Phase AF 確定）

**このセクションは以後の review 報告で必ず使うテンプレートである。**
実装報告ではなく「review 報告」として扱う。
数値がまだない項目は "未集計" と明示する。推測で埋めない。
根拠のない `promote_candidate` 結論は禁止。

---

### shadow hard guard review report

```
対象 candidate : stale_bid_ask
観測期間       : YYYY-MM-DD 〜 YYYY-MM-DD

母数
  total signals     :
  countable=true    :
  countable=false   :

shadow_bucket 分布
  no_signal         :
  triggered_only    :
  would_reject      :

overlap_bucket 分布
  no_overlap                :
  overlaps_price_stale      :
  distinct_from_price_stale :

advisory_bucket 分布
  none     :
  warning  :
  blocking :

decision_bucket 分布
  no_signal       :
  observe         :
  hold            :
  review_priority :

aggregate_review_verdict 分布
  insufficient_signal :
  observe_only        :
  overlap_hold        :
  priority_review     :

重点確認
  1. distinct_from_price_stale の割合は十分か
  2. review_priority は複数セッションで継続しているか
  3. overlap_hold 偏重ではないか
  4. advisory_bucket に極端な偏りはないか
  5. 誤検知が疑われる事例はあるか
  6. hard reject 化した場合の本番影響は許容可能か

レビュー結論
  [ ] promote_candidate   — 昇格条件をすべて満たした
  [ ] hold_observation    — 観測継続（evidence 不足 / 誤検知懸念あり）
  [ ] insufficient_evidence — 母数不足でレビュー判断不能

結論理由
  -

次アクション
  [ ] 継続観測
  [ ] 追加集計
  [ ] 昇格検討開始
```

---

### review 報告ルール（Phase AF 確定）

| ルール | 内容 |
|---|---|
| フォーマット | 上記テンプレートを必ず使う |
| 数値未集計時 | 各項目を "未集計" と明示する。推測で埋めない |
| 結論 | 観測根拠が十分な場合のみ `promote_candidate` を使う |
| 禁止 | 根拠のない昇格提案、テンプレートを省略した報告 |
| 適用範囲 | stale_bid_ask に関する全ての review 報告 |

---

## 今回の作業サマリー (2026-03-27 — Phase AM/AN: p_errno=2 セッション切断の自動再ログイン修正)

### Phase AM — 原因調査

**観察事実:**
- `MarketStateRunner` ログに `SymbolDataFetcher: ticker=7203 市場データ取得失敗 — この ticker はスキップ: p_errno=2 url=...` が継続記録
- コンテナ内から直接 `sUrlPrice` エンドポイントに HTTP リクエストを送信し、生レスポンスを確認:
  ```json
  {"287":"2","286":"セッションが切断しました。","334":"CLMMfdsGetMarketPrice"}
  ```
- `p_errno=2` は demo API 仕様制約ではなく**セッション切断**（サーバー側タイムアウト）
- セッション有効期限実測: 41分（2026-03-24 11:22→12:03）〜約10時間（2026-03-23 21:49→2026-03-24 08:30）
- ログ確認: 2026-03-25 15:04:47 に `current_price=3346.0` を正常取得している（再起動後は動く）

**根本原因:**
`_P_ERRNO_AUTH_CODES = frozenset({10001})` に `2` が含まれていなかったため:
- `p_errno=2` → `BrokerAPIError` を送出（`BrokerAuthError` にならない）
- `adapter.get_market_data()` が `BrokerAuthError` を catch して `session.invalidate()` を呼ぶ経路が通らない
- `ensure_session()` は `is_usable=True` のまま → 再ログインしない
- 結果: 永続的にセッション切断状態でデータ取得失敗が継続

### Phase AN — 修正内容

#### `trade_app/brokers/tachibana/client.py` 変更（1箇所）

```python
# Before
_P_ERRNO_AUTH_CODES: frozenset[int] = frozenset({
    10001,   # 暫定: セッション認証エラー（再ログイン必要）
})

# After
_P_ERRNO_AUTH_CODES: frozenset[int] = frozenset({
    2,       # セッション切断（実測: p_err_msg="セッションが切断しました。"）→ 再ログイン必要
    10001,   # 暫定: セッション認証エラー（再ログイン必要）
})
```

**修正後の動作フロー:**
1. `p_errno=2` → `_check_p_errno()` → `BrokerAuthError` 送出
2. `adapter.get_market_data()` が `BrokerAuthError` を catch → `_handle_auth_error()` → `session.invalidate()`
3. `is_usable=False` になる
4. 次サイクルの `ensure_session()` → `_do_login()` → 新しいセッション + 新しい `sUrlPrice` を取得
5. `get_market_data()` が新 URL で成功

#### テスト: `tests/test_tachibana_client_p_errno.py` 追加（9件）

| クラス | 件数 | 内容 |
|---|---|---|
| `TestCheckPErrno` | 7 | p_errno=2→BrokerAuthError / p_errno=10001→BrokerAuthError / p_errno=0→正常 / 欠損→正常 / p_errno=99→BrokerAPIError / 整数値2→BrokerAuthError / URL がメッセージに含まれる |
| `TestAdapterInvalidatesOnPErrno2` | 2 | BrokerAuthError → invalidate() 呼び出し / BrokerAPIError → invalidate() 呼ばれない |

**テスト**: 1775 件全通過（1766 → 1775、+9件）

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `trade_app/brokers/tachibana/client.py` | `_P_ERRNO_AUTH_CODES` に `2` を追加・`_check_p_errno` コメント更新 |
| `tests/test_tachibana_client_p_errno.py` | **新規作成** 9件 |

---

*最終更新: 2026-03-26 / Phase AK — market-hours entry smoke test runbook 確定*

---

## Phase AK — market-hours entry smoke test runbook（2026-03-26 確定）

> **このセクションは次の市場時間（JST 09:15–11:30）にそのまま実施できる runbook である。**
> コード変更・新 stage 追加・DB schema 変更は一切行わない。
> hard guard は price_stale のみ。stale_bid_ask は reject しない。実時刻判定の前提を崩さない。

---

### 背景・現状ブロッカー（Phase AJ 確定）

| ブロッカー | 詳細 | 解消条件 |
|---|---|---|
| `after_hours` | JST 23:00 現在、`time_window=after_hours` → `long_morning_trend` 通過不可 | JST 09:15–11:30 内での稼働 |
| `market=range` | 日経平均変動率 < ±0.5% → `trend_down` 条件を満たせず `short_risk_off_rebound` 通過不可 | 日経平均 ±0.5% 超の継続変動 |
| symbol データ空 | Tachibana demo API が `p_errno=2` を返す → `symbol_trend_up` / `symbol_volatility_high` が active にならない（`active_states=[]`） | Tachibana API 本番接続 or demo API symbol 取得方法の確認 |

**Gate 通過が現在不可能であっても Planning Layer 自体は正常（Phase AI exit path で 9 derived stages 含む trace 保存を実証済み）。**

---

### A. 実行前チェック

#### A-1. 時刻確認

```bash
TZ='Asia/Tokyo' date
# → JST 09:15–11:30 内であること
# 範囲外なら long_morning_trend は通過しない（after_hours/opening_auction_risk/midday_low_liquidity）
```

#### A-2. コンテナ稼働確認

```bash
cd /home/alma/trade-system
docker compose ps
# → trade_app / postgres / redis が全て Up (healthy) であること
```

#### A-3. API 疎通確認

```bash
curl -s http://localhost:8000/health
# → {"status":"ok"} であること
```

#### A-4. 現在の signal_plans / trade_signals 件数（実行前ベースライン）

```bash
docker exec trade-system-postgres-1 psql -U trade trade_db -c "
SELECT
  (SELECT COUNT(*) FROM signal_plans) as signal_plans_before,
  (SELECT COUNT(*) FROM trade_signals WHERE signal_type='entry') as entry_signals_before;"
```

#### A-5. market regime 確認（Gate 通過可否）

```bash
curl -s -H "Authorization: Bearer changeme_before_production" \
  http://localhost:8000/api/v1/market-state/current \
  | python3 -c "import sys,json; d=json.load(sys.stdin); [print(x['layer'],x['active_states']) for x in d]"

# long_morning_trend が通るために必要:
#   time_window: ["morning_trend_zone"]
#   market: ["range"] または ["normal"] (risk_off でなければ OK)
#
# short_risk_off_rebound が通るために必要:
#   market: ["trend_down"]
#   time_window: midday_low_liquidity でなければ OK
```

#### A-6. symbol データ取得可否確認

```bash
curl -s -H "Authorization: Bearer changeme_before_production" \
  http://localhost:8000/api/v1/market-state/symbols/7203 \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print('active_states:', d['active_states'])"

# active_states が空 [] なら symbol データ未取得
# active_states に "symbol_trend_up" が含まれれば long_morning_trend の symbol 条件が通る
```

#### A-7. Gate 通過可否の最終確認

```bash
curl -s -H "Authorization: Bearer changeme_before_production" \
  http://localhost:8000/api/v1/strategies/latest \
  | python3 -c "
import sys, json
decisions = json.load(sys.stdin)
for d in decisions:
    if d['ticker'] is None:
        print(d['strategy_code'], 'entry_allowed:', d['entry_allowed'], 'blocking:', d.get('blocking_reasons',[]))"

# entry_allowed: true のものが1つ以上あれば Gate 通過可能
```

---

### B. 実行コマンド（manual entry smoke test）

> **実施タイミング: JST 09:15–11:30 内、A-1〜A-7 が全て OK になってから実施すること。**
> analysis system からの自動送信がなくても、手動 POST でエンドポイントを直接叩く。

#### B-1. long_morning_trend 向け（BUY entry）

```bash
IDEM_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")
curl -s -X POST http://localhost:8000/api/signals \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer changeme_before_production" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -H "X-Source-System: manual-smoke-test" \
  -d '{
    "ticker": "7203",
    "signal_type": "entry",
    "order_type": "market",
    "side": "buy",
    "quantity": 100,
    "generated_at": "'"$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())")"'"
  }' | python3 -m json.tool
```

※ `generated_at` は Gate 判定に**影響しない**（Gate はサーバー現在時刻で評価）。正しい現在時刻を渡しておくことで audit trail を正確に保つ。

#### B-2. short_risk_off_rebound 向け（SELL entry）

```bash
IDEM_KEY=$(python3 -c "import uuid; print(uuid.uuid4())")
curl -s -X POST http://localhost:8000/api/signals \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer changeme_before_production" \
  -H "Idempotency-Key: $IDEM_KEY" \
  -H "X-Source-System: manual-smoke-test" \
  -d '{
    "ticker": "7203",
    "signal_type": "entry",
    "order_type": "market",
    "side": "sell",
    "quantity": 100,
    "generated_at": "'"$(python3 -c "from datetime import datetime,timezone; print(datetime.now(timezone.utc).isoformat())")"'"
  }' | python3 -m json.tool
```

---

### C. 成功条件

| 確認項目 | 確認コマンド | 成功条件 |
|---|---|---|
| HTTP レスポンス | curl 結果 | `202 Accepted` / `{"signal_id": "...", "status": "accepted"}` |
| trade_signals 増加 | 下記 SQL | `status='accepted'` のレコードが 1 件増加 |
| signal_plans 増加 | 下記 SQL | `planning_status='accepted'` または `'reduced'` のレコードが 1 件増加 |
| planning_trace_json 保存 | 下記 SQL | 10 stages 以上（planning steps 8 + derived stages 8） |
| execution_guard_hints 保存 | 下記 SQL | trace 内 `stage=execution_guard_hints` が存在 |
| derived stages 保存 | 下記 SQL | `stage=shadow_hard_guard_aggregate_review_verdict` が存在 |

#### C-1. 確認 SQL（実行後に走らせる）

```bash
# trade_signals の最新 entry レコード確認
docker exec trade-system-postgres-1 psql -U trade trade_db -c "
SELECT id, ticker, side, status, rejection_reason,
       created_at AT TIME ZONE 'Asia/Tokyo' as created_jst
FROM trade_signals
WHERE signal_type='entry'
ORDER BY created_at DESC
LIMIT 3;"

# signal_plans の最新レコード確認
docker exec trade-system-postgres-1 psql -U trade trade_db -c "
SELECT id, planning_status, planned_order_qty, rejection_reason_code,
       jsonb_array_length(planning_trace_json::jsonb) as trace_stages,
       created_at AT TIME ZONE 'Asia/Tokyo' as created_jst
FROM signal_plans
ORDER BY created_at DESC
LIMIT 3;"

# 最新 signal_plan の trace stages 一覧
docker exec trade-system-postgres-1 psql -U trade trade_db -c "
SELECT elem->>'stage' as stage,
       elem->>'decision' as decision,
       elem->>'verdict' as verdict,
       elem->>'guard_level' as guard_level
FROM signal_plans,
     jsonb_array_elements(planning_trace_json::jsonb) AS elem
WHERE id = (SELECT id FROM signal_plans ORDER BY created_at DESC LIMIT 1);"
```

#### C-2. execution_guard_hints の内容確認

```bash
docker exec trade-system-postgres-1 psql -U trade trade_db -c "
SELECT elem->'hints' as execution_guard_hints
FROM signal_plans,
     jsonb_array_elements(planning_trace_json::jsonb) AS elem
WHERE id = (SELECT id FROM signal_plans ORDER BY created_at DESC LIMIT 1)
  AND elem->>'stage' = 'execution_guard_hints';"
```

---

### D. 失敗時の切り分け

#### D-1. after_hours で Gate reject

```
症状: trade_signals.status='rejected', rejection_reason LIKE '%decision_blocked:global:long_morning_trend%'
      OR '%morning_trend_zone%'
確認: TZ='Asia/Tokyo' date で 09:15–11:30 外
対応: 時間外。市場時間まで待機。
```

#### D-2. market=range で Gate reject

```
症状: trade_signals.status='rejected', rejection_reason LIKE '%decision_blocked:global:short_risk_off_rebound%'
      OR '%trend_down%'
確認: GET /api/v1/market-state/current で market: ["range"]
対応: 日経平均変動率が ±0.5% 以上になるのを待機。observation のみ。
```

#### D-3. symbol データ欠損で Gate reject

```
症状: trade_signals.status='rejected', rejection_reason LIKE '%symbol_trend_up%' OR '%symbol_volatility_high%'
確認: GET /api/v1/market-state/symbols/7203 で active_states=[]
対応: Tachibana API p_errno=2 の解消を待機（下記 D 章「Tachibana demo API 制約」参照）
```

#### D-4. Authorization / Idempotency-Key / API 契約不一致

```
症状: 401 Unauthorized / 422 Unprocessable Entity / 400 Bad Request
確認: curl の -H ヘッダーを再確認。Idempotency-Key は UUID v4 形式。
      ticker は数字のみ（'7203' OK / '7203.T' NG）
      order_type=market の場合 limit_price 不要
対応: ヘッダー・ボディを修正して再実行（Idempotency-Key は毎回新しい UUID を生成）
```

#### D-5. Gate reject（signal_plans に記録なし）

```
症状: trade_signals.status='rejected', signal_plans 増加なし
確認: signal.status が 'rejected' で signal.rejection_reason が "strategy gate rejected: ..."
対応: Gate 段階で弾かれた（Planning Layer 未到達）。A-7 の Gate 通過可否を再確認。
```

#### D-6. Planning が rejected で signal_plans は増加している

```
症状: signal_plans.planning_status='rejected'
確認: signal_plans.rejection_reason_code を確認
      EXECUTION_GUARD_PRICE_STALE → price_stale hard guard が発動
      PLANNED_SIZE_ZERO → サイズ計算の結果 0 株になった
対応: execution_guard_hints の blocking_reasons を確認。price_stale の場合はデータ鮮度問題。
```

#### D-7. 保存失敗（アプリログ確認）

```bash
docker compose logs trade_app --tail=50 | grep -E "ERROR|WARNING|exception|Traceback"
```

---

### Tachibana demo API 制約の解釈ルール（Phase AK 確定）

#### 現状

| 観点 | 内容 |
|---|---|
| 現象 | Tachibana demo API が symbol data fetch 時に `p_errno=2` を返す |
| 結果 | `symbol_data_fetcher` が有効データを取得できない → symbol snapshot の `active_states=[]` |
| 影響 | `symbol_trend_up` / `symbol_volatility_high` が active にならない → Gate が entry を通過させない |

#### 未確定事項

| 事項 | 状態 |
|---|---|
| `p_errno=2` が demo API の仕様制約か、実装不備か | **未確定** |
| demo API で symbol データを正しく取得する方法があるか | **未調査** |
| 本番 API で解消するか | **未確認** |

#### 解釈ルール（運用上の分類）

entry smoke test で `signal_plans` に `no_signal` または Gate reject になった場合:

| ケース | 解釈 | stale_bid_ask 観測可否 |
|---|---|---|
| symbol data 取得失敗（`active_states=[]`）かつ Gate reject | **観測不能** — symbol 条件が充足されないため Gate を通過しない | 不可 |
| symbol data 取得成功・Gate pass・`execution_guard_hints.blocking_reasons=[]` | **正常 no_signal** — stale_bid_ask が発火しなかった（正常） | 観測可能（no_signal） |
| symbol data 取得成功・Gate pass・`blocking_reasons=["stale_bid_ask"]` | **shadow 観測対象** — stale_bid_ask が shadow event として記録される | 観測可能（would_reject 候補） |

**重要:** `p_errno=2` が継続する限り、stale_bid_ask の観測は「観測不能」状態に留まる。
解消方法が判明した時点で別途 runbook を更新すること。

---

### 次回市場時間チェックリスト（Phase AK 確定）

実施日時: ___________（JST 09:15–11:30 内に記入）

```
事前確認（実行前）
[ ] JST 09:15–11:30 内 — TZ='Asia/Tokyo' date で確認
[ ] trade_app 稼働 — docker compose ps で Up 確認
[ ] postgres / redis healthy — (healthy) 表示確認
[ ] API token 確認 — curl http://localhost:8000/health → {"status":"ok"}
[ ] signal_plans ベースライン件数記録 — 実行前カウント: ___ 件
[ ] trade_signals(entry) ベースライン件数記録 — 実行前カウント: ___ 件
[ ] market regime 確認 — GET /api/v1/market-state/current
    time_window: _____________  （morning_trend_zone なら long_morning_trend が通る可能性あり）
    market: _____________       （trend_down なら short_risk_off_rebound が通る可能性あり）
[ ] symbol データ取得可否確認 — GET /api/v1/market-state/symbols/7203
    active_states: _____________（[] なら Tachibana p_errno=2 継続）
[ ] Gate 通過可否確認 — GET /api/v1/strategies/latest
    entry_allowed=true の strategy: _____________（なければ Gate reject 確定）

実行
[ ] manual entry POST 実行 — B-1（BUY）または B-2（SELL）を IDEM_KEY を新規生成して実行
    HTTP レスポンス: _____________（202 なら受付）

実行後確認
[ ] trade_signals 保存確認 — C-1 SQL で status 確認
    status: _____________（accepted / rejected）
    rejection_reason（rejected 時）: _____________
[ ] signal_plans 保存確認 — C-1 SQL で planning_status 確認
    planning_status: _____________（accepted / reduced / rejected）
    trace_stages 件数: _____________（expected: 16 以上 for entry）
[ ] planning_trace_json 保存確認 — C-1 SQL でステージ一覧確認
    stage=execution_guard_hints: [ ] あり  [ ] なし
    stage=shadow_hard_guard_aggregate_review_verdict: [ ] あり  [ ] なし
[ ] execution_guard_hints 内容確認 — C-2 SQL で blocking_reasons 確認
    has_quote_risk: _____________
    blocking_reasons: _____________（[] なら stale_bid_ask なし）
[ ] shadow 系 stage 値確認
    shadow_bucket: _____________（no_signal / triggered_only / would_reject）
    overlap_bucket: _____________
    decision_bucket: _____________
    verdict: _____________（insufficient_signal / observe_only / overlap_hold / priority_review）
[ ] stale_bid_ask 観測可否判定
    [ ] symbol データ取得成功かつ Gate pass → 観測可能 → verdict を記録
    [ ] symbol データ空（p_errno=2）→ 観測不能 → Tachibana API 制約として記録
      ※ Phase AN 修正後は p_errno=2 で自動再ログインするため、次回実行時は観測可能になる見込み
```

---

## 今回の作業サマリー (2026-03-27 — Phase AO/AL/AP/AQ/AR-2: demo limitation freeze)

### Phase AO 完了（symbol data recovery verification）

**実測確認（JST 10:48 / Phase AN 修正後）:**
- `get_market_data("7203")`: current_price=3414.0, best_bid=3413.0, best_ask=3414.0 ✅
- `get_market_data("6758")`: current_price=3222.0, best_bid=3222.0, best_ask=3223.0 ✅
- `SymbolDataFetcher.fetch(["7203","6758"])`: keys=['7203','6758']（非空）✅
- p_errno=2 → BrokerAuthError → session.invalidate() → 再ログイン → 新 sUrlPrice 取得 ✅

### Phase AL 結果（market-hours manual entry smoke test）

- 実施: JST 11:07 頃に manual entry POST（ticker=7203, side=buy）
- 結果: Gate reject（`signal.status='rejected'`）
- 原因: `symbol_trend_up` が `missing_required_state` — symbol active_states=[] のため
- 根因: demo API が MA / ATR を返さないため symbol rule が全て skipped → active_states 永続的空

### Phase AP 結果（demo API sTargetColumn coverage 実測）

**demo API で取得可能（実測確認）:**

| sTargetColumn | numeric key | 内容 |
|---|---|---|
| `pDPP` | 115 | 現在値 |
| `pQBP` | 184 | 最良買気配値 |
| `pQAP` | 182 | 最良売気配値 |
| `pVWAP` | 213 | 当日 VWAP（実測: 3375.4〜3375.9 for 7203）|
| `pAV` | 99 | 出来高系（実測: 5300〜12300 / 種別未確定）|

**demo API で取得不能（key 認識されるが値が空）:**
- MA 系: `pMA5`, `pMA25`, `pMA75`
- テクニカル指標: `pATR`, `pRSI`, `pRS`
- 始値・高値・安値: `pOP`, `pHIP`, `pLOP`
- 前日終値・前週終値: `pPCP`, `pYCP`

**分類:**
- `pVWAP` / `pAV`: demo API は返すが adapter/mapper/MarketData が未対応 → **実装不足**
- MA / ATR / RSI / open / prev_close 系: demo API 自体が空を返す → **demo API 制約**

### Phase AQ 結論（demo-passable strategy path verification）

既存 strategy（`long_morning_trend` / `short_risk_off_rebound`）の条件を棚卸し、demo coverage との突合を実施。

**`long_morning_trend`（direction=long）の required_state:**
1. `time_window: morning_trend_zone` — JST 09:15–11:30 内で充足可能
2. `symbol: symbol_trend_up` — `price > vwap AND ma5 > ma20` 必須 → `ma5` / `ma20` が demo 制約で取得不能 → **永続的 missing**

**`short_risk_off_rebound`（direction=short）の required_state:**
1. `market: trend_down` — index_change_pct < −0.5% で充足可能
2. `symbol: symbol_volatility_high` — `atr / current_price >= 0.02` 必須 → `atr` が demo 制約で取得不能 → **永続的 missing**

**結論: demo 環境では既存 strategy の Gate 通過経路なし（2策略とも symbol-layer required_state でブロック）**

`pVWAP` を adapter に追加しても `long_morning_trend` の通過には不十分（`ma5` / `ma20` が残るため `symbol_trend_up` は skipped のまま）。

### Phase AR-2: demo limitation freeze（確定記録）

#### 再開条件

以下のいずれかが満たされるまで、Phase AL 以上（manual entry smoke test / Gate 通過確認）は保留:

| 条件 | 内容 |
|---|---|
| A | 本番 API 接続で `pMA5` / `pMA25` / `pATR` 等が有効な値を返す |
| B | 外部計算（日次バッチ / 時系列 DB）で `ma5` / `ma20` / `atr` を symbol state 入力として供給できる |

条件 A または B が充足された時点で Phase AL を再開し、market-hours manual entry smoke test を実施する。

#### backlog タスク（優先度低・保留）

| タスク | 内容 | 着手条件 |
|---|---|---|
| Phase AR-1 | `pVWAP` / `pAV` ingestion 実装（adapter / mapper / MarketData 拡張）| 再開条件 A または B とは独立。Gate 通過保証には直結しないため今は保留 |
| `pAV` 種別確定 | key=99 の値が株数か株数/100か累積か確認 | AR-1 着手前に確認要 |

#### 禁止事項（確定）

- strategy seed を demo 環境通過目的で変更しない
- demo 制約回避のために rule の guard 条件を緩めない
- 再開条件未充足のまま AL 以降の smoke test を反復しない

---

*最終更新: 2026-03-27 / Phase AR-2 — demo limitation freeze 確定 / 再開条件明文化 / テスト 1775 件全通過*

---

## 今回の作業サマリー (2026-03-27 — Phase AS-2/AT: 日次メトリクス設計・実装)

### Phase AS-2 設計承認事項（条件付き承認）

| 決定項目 | 採用案 |
|---|---|
| 日次データ供給元 | **J-Quants**（TSE公式・無料プラン・過去データ即時取得可）|
| 保存方式 | **新テーブル `daily_price_history`**（方式A）|
| DB schema 変更 | **必要**（migration 014 を新設）|
| 計算方式 | アプリ側でウィンドウ計算（MA/ATR/RSI）。DBには raw OHLCV のみ保存 |
| stale 判定 | `rows[0].trading_date < today_jst - 4日` → 全 None（部分利用禁止）|
| vwap 対応 | 本フェーズ対象外（mapper 追加は別フェーズ）|
| Tachibana API | 日次 OHLCV エンドポイントなし（リアルタイム配信のみ）→ 主供給元として除外 |
| yfinance | 非公式 → 本番不適として除外 |

### Phase AT 実装内容

| 実装内容 | 詳細 |
|---|---|
| DB (migration 014) | `daily_price_history` テーブル追加。UNIQUE(ticker, trading_date) / INDEX(ticker, trading_date DESC) |
| `DailyPriceHistory` モデル | SQLAlchemy モデル。OHLCV + source + created_at |
| `DailyPriceRow` dataclass | ORM から計算層を切り離すための内部型 |
| `DailyMetricsRepository` | DB から直近 N 行を trading_date DESC で取得。DailyPriceRow リストに変換して返す |
| `DailyMetricsComputer` | MA5 / MA20 / ATR14 / RSI14 を計算。stale / 行数不足は None |
| `runner.py` 修正 | `_run_once()` 内で symbol_data 取得後・engine 実行前に `DailyMetricsComputer.enrich` を注入 |
| `scripts/seed_daily_price.py` | J-Quants API から過去30取引日分を取得・upsert。`JQUANTS_EMAIL` / `JQUANTS_PASSWORD` 環境変数必要 |
| テスト 30 件追加 | MA/ATR/RSI 計算・stale・Repository・Runner integration |

### 計算定義（確定）

| メトリクス | 計算式 | 必要行数 |
|---|---|---|
| ma5 | 直近5取引日 close 単純平均 | 5 |
| ma20 | 直近20取引日 close 単純平均 | 20 |
| atr | 14日 Wilder ATR（先頭TR = H-L、以降は prev_close 使用）| 14 |
| rsi | 14期間 RSI（Wilder 初期値。avg_gain/avg_loss の単純平均）| 15（変化14本）|

### Stale / 欠損ポリシー（確定）

| ケース | 動作 |
|---|---|
| rows が空 | ma5=ma20=atr=rsi=None |
| `rows[0].trading_date < today_jst - 4日` | ma5=ma20=atr=rsi=None（stale）|
| 行数 < 必要数 | 該当メトリクスのみ None（他メトリクスは計算可能なら返す）|
| high/low=None（ATR計算時）| atr=None |

### 初期充填手順（Phase AT 完了後に実施）

```bash
# 1. migration 014 適用
docker compose exec trade_app alembic upgrade head

# 2. 初期充填（J-Quants API キー取得後）
export JQUANTS_EMAIL="your@email.com"
export JQUANTS_PASSWORD="yourpassword"
export DATABASE_URL="postgresql+asyncpg://trade:trade_secret@localhost:5432/trade_db"
export WATCHED_SYMBOLS="7203,6758"
python scripts/seed_daily_price.py --days 30

# 3. MarketStateRunner 起動（次サイクルから ma5/ma20/atr/rsi が注入される）
docker compose up -d trade_app
```

### 変更ファイル一覧

| ファイル | 変更内容 |
|---|---|
| `alembic/versions/014_daily_price_history.py` | **新規** daily_price_history テーブル |
| `trade_app/models/daily_price_history.py` | **新規** SQLAlchemy モデル |
| `trade_app/services/market_state/daily_metrics.py` | **新規** DailyMetricsRepository / DailyMetricsComputer / DailyPriceRow |
| `trade_app/services/market_state/runner.py` | `_run_once()` に daily metrics enrich 注入・`_JST` タイムゾーン追加 |
| `alembic/env.py` | daily_price_history モデル import 追加 |
| `tests/conftest.py` | daily_price_history モデル import 追加 |
| `scripts/seed_daily_price.py` | **新規** J-Quants 初期充填スクリプト |
| `tests/test_phase_at.py` | **新規** 30件テスト |

### 変更しなかったもの

- `symbol_evaluator.py`（`data.get("ma5")` は実装済み）
- `EvaluationContext` / `symbol_data` スキーマ
- strategy seed / planning / guard / stage
- broker adapter / Tachibana mapper

### 次フェーズ

**Phase AL 再実施条件**（demo limitation freeze の再開条件 B が部分充足）:
- migration 014 適用済みであること ← Phase AT で実装済み
- J-Quants API キー取得済みであること
- `seed_daily_price.py` で 20 取引日分以上を投入済みであること
- その後 Phase AL（market-hours manual entry smoke test）を再実施

**次フェーズ候補**:
1. J-Quants API キー取得 → seed_daily_price.py 実行 → migration 014 適用（インフラ作業）
2. Phase AR-1: `pVWAP` / `pAV` mapper 追加（本フェーズとは独立）

*最終更新: 2026-03-27 / Phase AT — daily_price_history テーブル + DailyMetricsComputer 実装 / テスト 1805 件全通過*
