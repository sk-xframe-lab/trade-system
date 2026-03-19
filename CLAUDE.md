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
