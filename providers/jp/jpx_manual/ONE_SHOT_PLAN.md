# Computer Use取得のONE_SHOT手順（未実行）

## Phase 2C-1F-R2更新（2026-09-20）

今回のR2にはこの節を優先し、以下の旧手順は履歴・一般設計として保持します。対象は2026-09-18に固定。既存JPX Calendarでは次の営業日は9月24日（21〜23日は休業日）です。翌営業日掲載という仕様に従い、9月24日18:00 JSTを初回観測候補とします。時刻は保証ではなく、ACQUISITION_TIME_STATUS=NOT_VALIDATED。旧08:00候補は採用しません。

現在は準備だけでScheduleなし。時刻到来時の自動起動はありません。ユーザーが当日に再開指示を出したときに、内蔵Browserで公式日報ページを1回確認します。Chromeは使いません。通常Navigation失敗のみ最大1回再試行、403/CAPTCHA/Access Denied/Rate Limit/Security Challengeは0回で停止。

EXPECTED_SESSION（対象立会日）、PUBLISHED_SESSION（一覧で掲載確認した日）、MISSING_SESSION（人間が別途確認した欠損）を分離。公開予定日前はNOT_YET_AVAILABLE、当日の未掲載はPENDING_PUBLICATION、翌日以降の未掲載はMISSING_SOURCE_CANDIDATE。いずれも自動Backfill登録は禁止。

今回の単発観測では未掲載なら外向き結果NOT_YET_AVAILABLEで終了し、内部のpublication_stateを併記。別Session選択・同日監視・URL推測は行いません。掲載ありの場合だけ対象PDF1件を取得し、既存Parser→Core6→Schema→Normalized→Manifestへ進みます。Parser失敗時は証拠を保存し、手入力で補正しません。

checked_at、expected_publication_business_date、available_at_check、published_sessionを記録。公開正確時刻が資料から分からなければpublished_exact_time=UNKNOWN。観測時刻を公開時刻と見なしません。全経路成功時だけBUILT_IN_BROWSER_TECHNICALLY_FEASIBLE=true候補。日次取得・Trial・Schedule・JPX_TERMS_VALIDATEDはfalseを維持。

## 旧手順（2C-1E時点）

状態: NOT_RUN。これは実行手順であり、定期取得の開始承認ではありません。利用条件の未解決事項は残っています。個人研究であることだけで自動ブラウザ操作が許可されたと見なしません。

## 取得前

1. 公式日報ページ・対象リンク・取得/保存/ローカル利用条件を人間が確認。リンク先やブラウザ保存先の確認を省略しない。
2. 検証済みJPX Calendarの範囲内で取得日と直前営業日を確定。土日だけを除いた自作日付で代用しない。08:00 JSTは目標時刻で、掲載完了の保証ではない。
3. 既存ManifestとInboxを照合。対象Sessionが登録済みなら新規Webアクセス0。原本Hashが異なる・不明PDFがある場合は人間確認で止める。欠損履歴は承認済みTrial対象期間だけから作る。
4. ONE_SHOTは1対象だけでBackfillを同時実行しない。通常運用計画は `acquisition.plan` で最大NORMAL 1件+最古BACKFILL 1件。
5. 取得Ledger（day、approved_plan、attempts、stopped）を作成。`begin_attempt` のIN_PROGRESS状態を `persist_ledger_new` で新しい版のファイルへ保存してからブラウザへ進む。単一実行者のみ。並列Browser workerは禁止。

## Computer Useの範囲

公式ページをブラウザで開く → 対象の株式相場表リンクを確認 → PDFを指定Inboxへ保存 → ブラウザ操作終了。HTTPクライアント・隠れたAPI・cookie抽出・制限迂回は使わない。PDFの解析・Forecast・ScoringはComputer Useで行わない。

CAPTCHA/403/Access Denied/明示的rate limit/security challengeはretry0、REVIEW_REQUIREDでその日は終了。リンク不明・ページ変更も同様。通常の一時失敗だけinitial+retry1。二度失敗したら当日終了し、次のBackfillへも進まない。

保存成功時点でsuccess件数を消費する。Parser失敗はダウンロード失敗と扱わず、追加取得を禁止して原本を保護。Ledgerの成功記録とParser結果は別々に保持する。途中終了でIN_PROGRESSが残った場合は当日自動再実行しない。

## ローカル処理

保存完了確認 → SHA-256 → Manifestへ原本登録（downloaded_atは実測、取得OriginはCOMPUTER_USE_BROWSER）→ 既存Parserへexpected sessionを渡す → Core6とSchema検証 → 出典・Hash付きJSON保存。

既存Parserは `MANUAL_DOWNLOAD_ONLY` を出力するため、将来Computer Use実測時はOriginをManifestで明示し、必要なAdapter版変更を先に行うこと。人間の手動取得と偽って登録しない。今回の人間取得済みサンプルのOriginは変更しない。

失敗（不正PDF、日付不一致、Core6欠損、重複、Schema不適合）はREVIEW_REQUIRED。原本補正・上書き・再取得の自動連鎖は禁止。

## ONE_SHOT判定

実際のブラウザ取得からローカルCore6抽出までの証拠が揃えばPASS/technically_feasible=true。実行して失敗した場合はFAIL/false。未実行はNOT_RUN/null（UNKNOWN）でありfalseと混同しない。

記録: 取得対象、公式URL、browser操作開始/終了、保存時刻、試行回数、制限検出、原本hash、Manifest参照、Parser版、Core6件数、Schema結果、エラー、実測可能なusage（取得不能ならUNKNOWN）。token/interaction数は推定を実測扱いしない。

結果がPASSでもDAILY_ACQUISITION_ENABLED=false、FREE_FORWARD_TRIAL_ENABLED=false、Schedule=falseを維持。本Phaseではブラウザテスト未実施。取得LedgerとManifest登録をつなぐ運用Runner、Calendar Adapter、Browser保存経路は今後の統合検証対象。

## Retention

確定したdownloaded_atのJST日付+30暦日。立会日やmtimeを起点にしない。時刻不明・各種保護条件・採点未完了は保管継続する。

`cleanup.py` は既定でdry-run。実行はcleanup_enabledとwindows_trash_backend_validatedの両方がtrueとなった後の明示指示のみ。現在は両方false。実行直前にRaw/JSON/Forecast/ScoreのHashと対応を再検証し、排他ロック、Manifest旧版、PREPARE journalを保存。Windowsごみ箱だけを呼び、失敗時はREVIEW_REQUIRED、削除Fallbackなし。途中停止時はjournalから人間が照合し、盲目的に再実行しない。

Windows実バックエンドはpywin32のIFileOperationを使用する未検証コード。非Recycle操作をcallbackで拒否する。テストは架空ファイル+模擬Trashであり、OSのゴミ箱そのものの証明ではない。別途、破棄可能な架空ファイルでのネイティブ検証と安全レビューが必要。

## Trial終了時のHuman Review

20〜30営業日後にAccuracy、Brier対3 Baseline、取得/Parserの失敗件数、Computer Use安定性、実測Token/interaction、人間介入回数、Raw/JSON保存容量・保護件数を比較。CONTINUE_JPX / MIGRATE_TO_API / STOP_TRIALを人間が選ぶ。自動移行・性能を理由にした自動Trial停止はしない。アクセス制限による当日停止とは区別する。
