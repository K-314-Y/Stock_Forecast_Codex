# 00_START_HERE — 通常Runの最小入口

通常Runはこのファイルから開始する。最初からMASTER_PROMPT・analysis・全configを読まない。

## 最初に読む
1. state/runtime/runtime_snapshot.json

通常Runは原則これだけで開始する。
不足・矛盾・異常がある場合のみ current_state.json / recent_sessions.json / run_checklist.json を読む。

## 条件付き参照
- Provider異常 → config/fresh_feature_bridge.json
- 履歴/Gap異常 → config/official_gap_policy.json と必要な該当レポート1件
- Threshold異常 → config/trial_thresholds_candidate.json
- Baseline異常 → config/trial_baselines_candidate.json
- Engine異常 → config/minimal_trial_engine.json
- 方法論変更/監査 → MASTER_PROMPT.md、analysis/、version_history

## 省トークン規則
- 5年Raw履歴をLLMへ渡さない。
- 過去レポートを通常Runで一括読込しない。
- recent_sessionsは直近31日だけ保持し、古い状態はarchiveへ。
- 欠損検出・hash・Feature・Baseline・ScoringはLocal Code優先。
- 異常がなければForecast LLMは最大1回。
- 状態変更後はruntimeの内部3ファイルを更新し、Localで `python -m engine.refresh_runtime_snapshot` を実行する。
- runtime_snapshot.jsonは生成物。通常Runではこれを最優先する。

## Core6 Freshness観測 / 取得フォールバック
- Core6のFreshness観測または「観測を実行」では、直前JPX営業日の必要入力を最初に確認する。
- 既存の有効Session・PDF・hash検証済みParsed/Manifestがある場合は再取得しない。
- Raw PDFが存在し `RAW_VALIDATED_METADATA_PENDING` の場合は再ダウンロードせず、既存PDFのprovenance/metadata検証と正規化を先に行う。
- 本当に未取得で、JPX上で公開確認できるSessionだけ `config/jpx_daily_acquisition_config.json` の MISSING_ONLY 経路で補完してよい。
- 通常取得経路は可視Google Chrome + Remote Desktop Commander。Historical Catch-upの2026-10-05〜07だけは `config/jpx_daily_acquisition_config.json#historical_direct` の限定例外を参照する。新adapterはfixture検証段階で実通信未実施。Fresh Close・Forecast・Outcome取得へ流用しない。CAPTCHA/403/429/security challengeは回避せず停止する。
- Freshness観測は予測生成と分離する。08:45以降の観測でもFreezeを偽装・遡及せず、実時刻を保存する。

現在の次作業詳細が必要な場合のみrun_checklist.jsonを読む。

## 2026-10-13 個人・非商用Forward Trial
- [Task 10B運用条件](docs/FRESH_EPHEMERAL_FORWARD_TRIAL.md) と `config/forward_trial_execution_policy.json` を適用する。
- 許可スコープは `LOCAL_PERSONAL_NONCOMMERCIAL_FORWARD_TRIAL` のみ。Formal / Schedule / scoringは無効。
- Task 10Cでは `engine.forward_trial_inputs.prepare_execution_inputs` にticker-boundの既存connector callableを渡し、Core6を当日新規取得する。provider responseをファイルへ中継・保存しない。
- 保存済み10/9 Fresh evidence・Task 10A feature-input・保存済みderived snapshotを実行入力に再利用しない。
- Task 10Bはゲート整備のみ。Forecast / Freeze / Forecast ID / probabilities / scoringは未実行。
