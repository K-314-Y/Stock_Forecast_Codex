# INITIALIZE_WORK

このフォルダを株式市場予測システムとして初期化してください。

1. `MASTER_PROMPT.md` を最初から最後まで読み、最優先ルールとして扱う。
2. 既存ファイルは削除・上書きしない。
3. 必要なら以下の構造を作成する。
   - config/
   - logs/
   - state/fast/
   - state/medium/
   - state/slow/
   - historical_shocks/
   - analysis/
   - exports/
   - backups/
4. 最低限、以下の初期ファイルを作る。
   - config/analysis_config.md
   - config/core_universe.json
   - config/rotation_universe.json
   - config/version_history.md
   - logs/prediction_log.csv
   - logs/score_log.csv
   - logs/decision_trace_log.csv
   - logs/hypothesis_log.csv
   - logs/trial_ledger.csv
   - logs/rotation_history.csv
   - logs/regime_history.csv
   - logs/investor_cohort_history.csv
   - logs/expectation_gap_log.csv
   - historical_shocks/shock_library.json
5. まだ正式予測は開始しない。
6. Rotation Universe 100+100はMASTER_PROMPT条件に従って構築計画を作る。勝手に完成扱いしない。
7. Historical Shock Libraryは約20年の全日次データではなく、重要Eventの構造化Summaryとして設計する。
8. Investor Behavioral CohortsはShadow Layerとして初期化し、Productionへ混ぜない。
9. 最後に以下を報告する。
   - 作成したファイル一覧
   - 未作成・未確定事項
   - 接続可能なデータソース
   - Formal Run開始を妨げる問題
10. 最終状態を `READY / PARTIAL_READY / NOT_READY` のいずれかで示す。
