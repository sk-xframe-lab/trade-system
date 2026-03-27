"""
Planning trace 読み取り補助

planning_trace_json から派生情報を安全に導出するユーティリティ。
source of truth は planning_trace_json であり、
ここで計算する派生 entry はすべて再計算可能なデータである。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
stale_bid_ask shadow hard guard 観測系 — 派生 stage 一覧（Phase AE 確定）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

以下の derived stage は Phase W〜AD で追加した観測専用 entry である。
いずれも planning_trace_json から再計算可能であり、hard guard 判定には使わない。
stale_bid_ask の reject ロジックとは無関係。

source events / source inputs（再計算の入力）:
  - shadow_hard_guard_decision  : shadow event 本体（Phase W）
  - execution_guard_hints       : blocking/warning reason の入力（PlannerContext）
  - advisory_guard_assessment   : advisory guard 評価（Phase U / _save_plan 内）

derived stages（上記から計算する派生データ）:

  1. shadow_hard_guard_assessment        [Phase X]
     shadow event 群の集約。
     has_shadow_candidate / candidates / would_reject_candidates / event_count を保持。

  2. shadow_hard_guard_review_summary    [Phase Y]
     candidate 単位の簡易レビュー要約。
     shadow_triggered / would_reject / promotion_readiness を保持。
     promotion_readiness: "no_signal" | "observe" | "needs_review"

  3. shadow_hard_guard_promotion_metrics [Phase AA]
     昇格判断用の基礎観測値。
     overlaps_with_price_stale / has_advisory_guard / promotion_signal_weight を含む。

  4. shadow_hard_guard_promotion_decision [Phase AB]
     1 signal 単位の provisional decision。
     decision: "no_signal" | "observe" | "hold" | "review_priority"

  5. shadow_hard_guard_aggregate_review_key [Phase AC]
     aggregate しやすい分類キー。aggregate_key_version=1 で固定。
     shadow_bucket / overlap_bucket / advisory_bucket / decision_bucket / countable

  6. shadow_hard_guard_aggregate_review_verdict [Phase AD]
     aggregate 結果を読むための verdict ラベル。verdict_version=1 で固定。
     verdict: "insufficient_signal" | "observe_only" | "overlap_hold" | "priority_review"

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
stale_bid_ask 昇格判定基準（Phase AE 確定）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

以下の観点をすべて確認してから昇格を判断すること。

  1. countable=True の母数が十分あること
     （verdict="priority_review" または "overlap_hold" の累積件数が閾値を超えること）

  2. overlap_bucket="distinct_from_price_stale" が一定割合あること
     price_stale との完全重複に過ぎる場合は昇格価値が低い。
     price_stale の代替指標として機能している証拠が必要。

  3. decision_bucket="review_priority" が継続的に観測されること
     単発ではなく複数セッションを通じて安定して出現していること。

  4. advisory_bucket の分布を確認すること
     すべて "blocking" または "none" に偏る場合は実態に即した解釈が必要。

  5. 誤検知懸念が強い場合は昇格しないこと
     本番 reject 影響の事前評価が完了するまで observe を継続してよい。

  6. 昇格レビューの結論ラベル（運用方針）:
     - promote_candidate   : 昇格条件をすべて満たした
     - hold_observation    : 観測継続（evidence 不足 / 誤検知懸念あり）
     - insufficient_evidence : 母数不足でレビュー判断不能

     ※ 上記ラベルは今後のレビュー運用で使う概念であり、
        現時点では planning_trace_json に追加しない。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
今後の方針（Phase AE で凍結）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Phase AE をもって stale_bid_ask shadow hard guard 観測系の実装を完了とする。

次フェーズでやること:
  - 観測データを集計して上記判定基準を確認する（review フェーズ）
  - 昇格判断が出た場合のみ stale_bid_ask を hard guard 化する

次フェーズでやらないこと:
  - 新たな derived stage の追加（原則禁止）
  - reject ロジックの変更（昇格判断が確定するまで禁止）
  - planning_trace_json の構造変更

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""
from __future__ import annotations

from typing import Any


# ─── Phase Z: 派生 entry upsert / 読み出し helper ────────────────────────────

def upsert_trace_stage(trace: Any, entry: Any) -> list[dict[str, Any]]:
    """
    trace から同一 stage の既存 entry を全て除去してから entry を末尾に追加する。

    - 元の trace は変更しない（新しい list を返す）
    - trace が list でない場合は空 list として扱う
    - entry が dict でない、または stage キーが文字列でない場合は何もしない
    - decision event（例: shadow_hard_guard_decision）は除去対象外
      （upsert は entry["stage"] と完全一致する entry だけを対象にする）
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("stage"), str):
        return list(trace) if isinstance(trace, list) else []

    stage = entry["stage"]
    base: list[dict[str, Any]] = trace if isinstance(trace, list) else []
    filtered = [e for e in base if not (isinstance(e, dict) and e.get("stage") == stage)]
    return filtered + [entry]


def get_latest_stage_entry(trace: Any, stage: str) -> dict[str, Any] | None:
    """
    trace の末尾から走査して、最初に見つかった stage 一致 entry を返す。
    見つからない場合 / trace が不正な場合は None。
    """
    if not isinstance(trace, list):
        return None
    for entry in reversed(trace):
        if isinstance(entry, dict) and entry.get("stage") == stage:
            return entry
    return None


def get_shadow_hard_guard_assessment(trace: Any) -> dict[str, Any] | None:
    """shadow_hard_guard_assessment entry を取得する。なければ None。"""
    return get_latest_stage_entry(trace, "shadow_hard_guard_assessment")


def get_shadow_hard_guard_review_summary(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any] | None:
    """
    shadow_hard_guard_review_summary entry を取得する。

    candidate 引数と entry["candidate"] が一致しない場合は None を返す。
    """
    entry = get_latest_stage_entry(trace, "shadow_hard_guard_review_summary")
    if entry is None:
        return None
    if entry.get("candidate") != candidate:
        return None
    return entry


def extract_shadow_hard_guard_assessment(trace: Any) -> dict[str, Any]:
    """
    planning_trace_json から shadow_hard_guard_assessment を導出する。

    走査対象: stage == "shadow_hard_guard_decision" のイベント

    返す情報:
        has_shadow_candidate     : event_count > 0
        candidates               : 出現順・重複排除した candidate list
        would_reject_candidates  : decision == "would_reject" の candidate（出現順・重複排除）
        event_count              : shadow_hard_guard_decision イベント総数

    不正入力（None / 非 list / 欠損キー等）は安全側で空評価として扱う。
    stale_bid_ask 固有ロジックを持たず、将来の candidate 追加でも壊れない。
    """
    if not isinstance(trace, list):
        return _empty_assessment()

    candidates: list[str] = []
    would_reject: list[str] = []
    event_count = 0

    seen_candidates: set[str] = set()
    seen_would_reject: set[str] = set()

    for entry in trace:
        if not isinstance(entry, dict):
            continue
        if entry.get("stage") != "shadow_hard_guard_decision":
            continue

        candidate = entry.get("candidate")
        decision = entry.get("decision")
        if not isinstance(candidate, str) or not isinstance(decision, str):
            continue

        event_count += 1

        if candidate not in seen_candidates:
            candidates.append(candidate)
            seen_candidates.add(candidate)

        if decision == "would_reject" and candidate not in seen_would_reject:
            would_reject.append(candidate)
            seen_would_reject.add(candidate)

    return {
        "has_shadow_candidate":     event_count > 0,
        "candidates":               candidates,
        "would_reject_candidates":  would_reject,
        "event_count":              event_count,
    }


def _empty_assessment() -> dict[str, Any]:
    return {
        "has_shadow_candidate":     False,
        "candidates":               [],
        "would_reject_candidates":  [],
        "event_count":              0,
    }


# ─── Phase Y: shadow hard guard review summary ───────────────────────────────

def extract_shadow_hard_guard_review_summary(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any]:
    """
    planning_trace_json から指定 candidate の昇格レビュー用 summary を導出する。

    判定元:
        stage == "shadow_hard_guard_decision" のイベントのうち、
        candidate 引数に一致するものだけを走査する。

    返す情報:
        stage                : "shadow_hard_guard_review_summary"（固定）
        candidate            : 引数で渡した candidate 名
        shadow_triggered     : 当該 candidate の shadow event が1件以上ある
        would_reject         : decision == "would_reject" の event が1件以上ある
        promotion_readiness  : "no_signal" | "observe" | "needs_review"
        promotion_blockers   : list[str]（今回は []）
        notes                : list[str]（今回は []）

    promotion_readiness ルール:
        shadow_triggered = False  → "no_signal"
        shadow_triggered = True AND would_reject = True  → "needs_review"
        shadow_triggered = True AND would_reject = False → "observe"

    安全性:
        trace が None / 非 list / 要素不正でも落ちない。
        candidate 引数を変えるだけで将来の candidate に流用できる。
    """
    if not isinstance(trace, list):
        return _empty_review_summary(candidate)

    shadow_triggered = False
    would_reject = False

    for entry in trace:
        if not isinstance(entry, dict):
            continue
        if entry.get("stage") != "shadow_hard_guard_decision":
            continue

        entry_candidate = entry.get("candidate")
        entry_decision = entry.get("decision")
        if not isinstance(entry_candidate, str) or not isinstance(entry_decision, str):
            continue
        if entry_candidate != candidate:
            continue

        shadow_triggered = True
        if entry_decision == "would_reject":
            would_reject = True

    readiness = _promotion_readiness(shadow_triggered, would_reject)

    return {
        "stage":               "shadow_hard_guard_review_summary",
        "candidate":           candidate,
        "shadow_triggered":    shadow_triggered,
        "would_reject":        would_reject,
        "promotion_readiness": readiness,
        "promotion_blockers":  [],
        "notes":               [],
    }


def _promotion_readiness(shadow_triggered: bool, would_reject: bool) -> str:
    if not shadow_triggered:
        return "no_signal"
    if would_reject:
        return "needs_review"
    return "observe"


def _empty_review_summary(candidate: str) -> dict[str, Any]:
    return {
        "stage":               "shadow_hard_guard_review_summary",
        "candidate":           candidate,
        "shadow_triggered":    False,
        "would_reject":        False,
        "promotion_readiness": "no_signal",
        "promotion_blockers":  [],
        "notes":               [],
    }


# ─── Phase AA: shadow hard guard promotion metrics ───────────────────────────

def extract_shadow_hard_guard_promotion_metrics(
    trace: Any,
    execution_guard_hints: Any = None,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any]:
    """
    planning_trace_json から stale_bid_ask 昇格レビュー用 metrics を導出する。

    判定元:
        - shadow_hard_guard_decision    : shadow_triggered / would_reject
        - advisory_guard_assessment     : has_advisory_guard / advisory_guard_level
        - execution_guard_hints         : overlaps_with_price_stale

    返す情報:
        stage                   : "shadow_hard_guard_promotion_metrics"（固定）
        candidate               : 引数で渡した candidate 名
        shadow_triggered        : 対象 candidate の shadow event が1件以上ある
        would_reject            : 対象 candidate で decision == "would_reject" が1件以上ある
        overlaps_with_price_stale : execution_guard_hints.blocking_reasons に "price_stale" がある
        has_advisory_guard      : advisory_guard_assessment の guard_level が "none" 以外
        advisory_guard_level    : advisory_guard_assessment.guard_level（なければ "none"）
        promotion_signal_weight : would_reject=True → 1、それ以外 → 0

    安全性:
        trace / execution_guard_hints が None / 不正形式でも落ちない。
        candidate 引数を変えることで将来の candidate に流用できる。
    """
    # ── shadow_triggered / would_reject ──────────────────────────────────────
    shadow_triggered = False
    would_reject = False

    if isinstance(trace, list):
        for entry in trace:
            if not isinstance(entry, dict):
                continue
            if entry.get("stage") != "shadow_hard_guard_decision":
                continue
            entry_candidate = entry.get("candidate")
            entry_decision = entry.get("decision")
            if not isinstance(entry_candidate, str) or not isinstance(entry_decision, str):
                continue
            if entry_candidate != candidate:
                continue
            shadow_triggered = True
            if entry_decision == "would_reject":
                would_reject = True

    # ── overlaps_with_price_stale ─────────────────────────────────────────────
    overlaps_with_price_stale = False
    if isinstance(execution_guard_hints, dict):
        blocking = execution_guard_hints.get("blocking_reasons")
        if isinstance(blocking, list) and "price_stale" in blocking:
            overlaps_with_price_stale = True

    # ── advisory_guard_level / has_advisory_guard ─────────────────────────────
    advisory_guard_level = "none"
    advisory_entry = get_latest_stage_entry(trace, "advisory_guard_assessment")
    if advisory_entry is not None:
        raw_level = advisory_entry.get("guard_level")
        if isinstance(raw_level, str):
            advisory_guard_level = raw_level
    has_advisory_guard = advisory_guard_level != "none"

    return {
        "stage":                    "shadow_hard_guard_promotion_metrics",
        "candidate":                candidate,
        "shadow_triggered":         shadow_triggered,
        "would_reject":             would_reject,
        "overlaps_with_price_stale": overlaps_with_price_stale,
        "has_advisory_guard":       has_advisory_guard,
        "advisory_guard_level":     advisory_guard_level,
        "promotion_signal_weight":  1 if would_reject else 0,
    }


def get_shadow_hard_guard_promotion_metrics(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any] | None:
    """
    shadow_hard_guard_promotion_metrics entry を取得する。

    candidate 引数と entry["candidate"] が一致しない場合は None を返す。
    """
    entry = get_latest_stage_entry(trace, "shadow_hard_guard_promotion_metrics")
    if entry is None:
        return None
    if entry.get("candidate") != candidate:
        return None
    return entry


# ─── Phase AB: shadow hard guard provisional promotion decision ───────────────

def extract_shadow_hard_guard_promotion_decision(
    trace: Any,
    execution_guard_hints: Any = None,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any]:
    """
    planning_trace_json から stale_bid_ask の provisional promotion decision を導出する。

    Phase AA の promotion metrics を利用して evidence を構築し、
    evidence から provisional decision を導出する。

    decision ルール（上から優先順）:
        shadow_triggered = False
            → "no_signal" / "shadow_not_triggered"
        shadow_triggered = True かつ would_reject = False
            → "observe" / "shadow_triggered_without_would_reject"
        would_reject = True かつ overlaps_with_price_stale = True
            → "hold" / "overlaps_with_price_stale"
        would_reject = True かつ overlaps_with_price_stale = False
            → "review_priority" / "shadow_would_reject"

    安全性:
        trace / execution_guard_hints が None / 不正形式でも落ちない。
        candidate 引数で将来の candidate に流用できる。
    """
    metrics = extract_shadow_hard_guard_promotion_metrics(
        trace,
        execution_guard_hints=execution_guard_hints,
        candidate=candidate,
    )

    evidence = {
        "shadow_triggered":         metrics["shadow_triggered"],
        "would_reject":             metrics["would_reject"],
        "overlaps_with_price_stale": metrics["overlaps_with_price_stale"],
        "has_advisory_guard":       metrics["has_advisory_guard"],
        "advisory_guard_level":     metrics["advisory_guard_level"],
        "promotion_signal_weight":  metrics["promotion_signal_weight"],
    }

    decision, reasons = _derive_promotion_decision(
        shadow_triggered=metrics["shadow_triggered"],
        would_reject=metrics["would_reject"],
        overlaps_with_price_stale=metrics["overlaps_with_price_stale"],
    )

    return {
        "stage":            "shadow_hard_guard_promotion_decision",
        "candidate":        candidate,
        "decision":         decision,
        "decision_reasons": reasons,
        "evidence":         evidence,
    }


def _derive_promotion_decision(
    shadow_triggered: bool,
    would_reject: bool,
    overlaps_with_price_stale: bool,
) -> tuple[str, list[str]]:
    """provisional promotion decision と理由を返す内部ヘルパー。"""
    if not shadow_triggered:
        return "no_signal", ["shadow_not_triggered"]
    if not would_reject:
        return "observe", ["shadow_triggered_without_would_reject"]
    if overlaps_with_price_stale:
        return "hold", ["overlaps_with_price_stale"]
    return "review_priority", ["shadow_would_reject"]


def get_shadow_hard_guard_promotion_decision(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any] | None:
    """
    shadow_hard_guard_promotion_decision entry を取得する。

    candidate 引数と entry["candidate"] が一致しない場合は None を返す。
    """
    entry = get_latest_stage_entry(trace, "shadow_hard_guard_promotion_decision")
    if entry is None:
        return None
    if entry.get("candidate") != candidate:
        return None
    return entry


# ─── Phase AC: shadow hard guard aggregate review key ────────────────────────

def extract_shadow_hard_guard_aggregate_review_key(
    trace: Any,
    execution_guard_hints: Any = None,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any]:
    """
    planning_trace_json から stale_bid_ask 用 aggregate_review_key を導出する。

    Phase AA の promotion metrics と Phase AB の promotion decision を材料に、
    1 signal 単位で安定集計できる固定キーを構築する。

    返す情報:
        stage                  : "shadow_hard_guard_aggregate_review_key"（固定）
        candidate              : 引数で渡した candidate 名
        aggregate_key_version  : 1（後方互換のためのバージョン固定）
        shadow_bucket          : "no_signal" | "triggered_only" | "would_reject"
        overlap_bucket         : "no_overlap" | "overlaps_price_stale" | "distinct_from_price_stale"
        advisory_bucket        : "none" | "warning" | "blocking"
        decision_bucket        : "no_signal" | "observe" | "hold" | "review_priority"
        countable              : bool（would_reject=True のとき True）

    安全性:
        trace / execution_guard_hints が None / 不正形式でも落ちない。
        candidate 引数で将来の candidate に流用できる。
    """
    metrics = extract_shadow_hard_guard_promotion_metrics(
        trace,
        execution_guard_hints=execution_guard_hints,
        candidate=candidate,
    )
    decision_entry = extract_shadow_hard_guard_promotion_decision(
        trace,
        execution_guard_hints=execution_guard_hints,
        candidate=candidate,
    )

    shadow_triggered = metrics["shadow_triggered"]
    would_reject = metrics["would_reject"]
    overlaps = metrics["overlaps_with_price_stale"]
    advisory_level = metrics["advisory_guard_level"]
    decision = decision_entry["decision"]

    shadow_bucket = _derive_shadow_bucket(shadow_triggered, would_reject)
    overlap_bucket = _derive_overlap_bucket(shadow_triggered, overlaps)
    advisory_bucket = _derive_advisory_bucket(advisory_level)

    return {
        "stage":                 "shadow_hard_guard_aggregate_review_key",
        "candidate":             candidate,
        "aggregate_key_version": 1,
        "shadow_bucket":         shadow_bucket,
        "overlap_bucket":        overlap_bucket,
        "advisory_bucket":       advisory_bucket,
        "decision_bucket":       decision,
        "countable":             would_reject,
    }


def _derive_shadow_bucket(shadow_triggered: bool, would_reject: bool) -> str:
    if not shadow_triggered:
        return "no_signal"
    if not would_reject:
        return "triggered_only"
    return "would_reject"


def _derive_overlap_bucket(shadow_triggered: bool, overlaps_with_price_stale: bool) -> str:
    if not shadow_triggered:
        return "no_overlap"
    if overlaps_with_price_stale:
        return "overlaps_price_stale"
    return "distinct_from_price_stale"


def _derive_advisory_bucket(advisory_guard_level: str) -> str:
    if advisory_guard_level == "blocking":
        return "blocking"
    if advisory_guard_level == "warning":
        return "warning"
    return "none"


def get_shadow_hard_guard_aggregate_review_key(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any] | None:
    """
    shadow_hard_guard_aggregate_review_key entry を取得する。

    candidate 引数と entry["candidate"] が一致しない場合は None を返す。
    """
    entry = get_latest_stage_entry(trace, "shadow_hard_guard_aggregate_review_key")
    if entry is None:
        return None
    if entry.get("candidate") != candidate:
        return None
    return entry


# ─── Phase AD: shadow hard guard aggregate review verdict ─────────────────────

def extract_shadow_hard_guard_aggregate_review_verdict(
    trace: Any,
    execution_guard_hints: Any = None,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any]:
    """
    planning_trace_json から stale_bid_ask 用 aggregate_review_verdict を導出する。

    Phase AC の aggregate review key を材料に、将来 aggregate 集計した結果を
    読むための verdict ラベルを 1 signal 単位で固定する。

    verdict ルール（上から優先順）:
        countable = False かつ shadow_bucket = "no_signal"
            → "insufficient_signal" / ["not_countable"]
        countable = False かつ shadow_bucket = "triggered_only"
            → "observe_only" / ["triggered_without_would_reject"]
        countable = True かつ overlap_bucket = "overlaps_price_stale"
            → "overlap_hold" / ["overlaps_price_stale"]
        countable = True かつ overlap_bucket = "distinct_from_price_stale"
            → "priority_review" / ["distinct_would_reject"]

    返す情報:
        stage             : "shadow_hard_guard_aggregate_review_verdict"（固定）
        candidate         : 引数で渡した candidate 名
        verdict_version   : 1（後方互換のためのバージョン固定）
        verdict           : "insufficient_signal" | "observe_only" | "overlap_hold" | "priority_review"
        verdict_reasons   : list[str]
        supporting_buckets: Phase AC の aggregate review key の各 bucket

    安全性:
        trace / execution_guard_hints が None / 不正形式でも落ちない。
        candidate 引数で将来の candidate に流用できる。
    """
    agg_key = extract_shadow_hard_guard_aggregate_review_key(
        trace,
        execution_guard_hints=execution_guard_hints,
        candidate=candidate,
    )

    supporting_buckets = {
        "shadow_bucket":   agg_key["shadow_bucket"],
        "overlap_bucket":  agg_key["overlap_bucket"],
        "advisory_bucket": agg_key["advisory_bucket"],
        "decision_bucket": agg_key["decision_bucket"],
        "countable":       agg_key["countable"],
    }

    verdict, reasons = _derive_aggregate_verdict(
        shadow_bucket=agg_key["shadow_bucket"],
        overlap_bucket=agg_key["overlap_bucket"],
        countable=agg_key["countable"],
    )

    return {
        "stage":             "shadow_hard_guard_aggregate_review_verdict",
        "candidate":         candidate,
        "verdict_version":   1,
        "verdict":           verdict,
        "verdict_reasons":   reasons,
        "supporting_buckets": supporting_buckets,
    }


def _derive_aggregate_verdict(
    shadow_bucket: str,
    overlap_bucket: str,
    countable: bool,
) -> tuple[str, list[str]]:
    """aggregate review verdict と理由を返す内部ヘルパー。"""
    if not countable and shadow_bucket == "no_signal":
        return "insufficient_signal", ["not_countable"]
    if not countable and shadow_bucket == "triggered_only":
        return "observe_only", ["triggered_without_would_reject"]
    if countable and overlap_bucket == "overlaps_price_stale":
        return "overlap_hold", ["overlaps_price_stale"]
    if countable and overlap_bucket == "distinct_from_price_stale":
        return "priority_review", ["distinct_would_reject"]
    # フォールバック（通常到達しない）
    return "insufficient_signal", ["not_countable"]


def get_shadow_hard_guard_aggregate_review_verdict(
    trace: Any,
    candidate: str = "stale_bid_ask",
) -> dict[str, Any] | None:
    """
    shadow_hard_guard_aggregate_review_verdict entry を取得する。

    candidate 引数と entry["candidate"] が一致しない場合は None を返す。
    """
    entry = get_latest_stage_entry(trace, "shadow_hard_guard_aggregate_review_verdict")
    if entry is None:
        return None
    if entry.get("candidate") != candidate:
        return None
    return entry
