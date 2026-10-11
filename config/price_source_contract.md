# Price Source Contract v1.2 - Phase 2B-3

## Phase 2C-4G 追記（2026-09-30）

Bigdata/FMPは2026-09-28・09-29の別々の立会日について、08:20〜08:35 JSTのCore6鮮度観測が各6/6 PASS、固定価格と同日JPX公式終値が計12/12完全一致。Minimal Trialに限り `FRESH_CLOSE_BRIDGE` / `VALIDATED_AS_PREVIOUS_SESSION_CLOSE_FOR_MINIMAL_TRIAL` として技術検証PASS。ただしOutcome Scoringには使用せず、JPXを維持する。Bigdata Volume Featureは無効。歴史CacheとJPX Rawの調整基準はUNVERIFIEDで、Live Dry RunとForward Trialは未許可。JPX/Historical Source Termsと追加費用の確定も未解決。以下の旧節は当時の記録として残す。

## Phase 2C-4B 追記（2026-09-26）

過去節は当時の監査履歴として保持する。現在のMinimal Live候補では、Bigdata/FMPの役割を `FRESH_CLOSE_BRIDGE_CANDIDATE` に限定する。Bigdata `price` とJPX同日公式Closeの2営業日以上の照合は未達（0件）で、実入力適格性はfalse。BigdataのVolumeは保存のみ可、`volume_ratio_20d`等の予測Featureへ使わない。Outcome Scoringにも使わない。

JPX既存原本の2026-09-15・09-18は公式Gap Layerへ原本Hash付きで分離した。未取得09-14・16・17・24が残り、`OFFICIAL_GAP_SERIES_READY=false`。yfinance歴史Cacheへ追記しない。Cache候補の調整基準とJPX Rawの接続互換性も未検証で、物理的・論理的な自動接続を許可しない。

Bigdataは既存Connectorで利用可能で、現呼出し中に追加支払/upgrade要求は観測されなかった。ただし `INCREMENTAL_COST=UNKNOWN` であり、無料の証明ではない。この未知だけで追加料金を伴わない技術試験を停止しない一方、支払やPlan変更を要求されたら即停止する。`TECHNICAL_STATUS=GAP_REMAINS`、`TERMS_STATUS=UNRESOLVED`を分けて扱う。Trial・日次取得・Scheduleはいずれも無効。

Date: 2026-09-15. Status: PARTIAL. RUN_MODE: FREE_FORWARD_TRIAL_PREP.
This is a declarative research contract, not executable enforcement or provider admission.
MASTER_PROMPT.md and runtime gates take precedence. All execution gates remain disabled.

## Role separation
| Scope | Provider / candidate | Eligibility for this contract | Series / semantics | Permitted role |
|---|---|---|---|---|
| Japan Trial Historical | Unselected; existing yfinance cache candidate | RESEARCH_ONLY | SPLIT_NORMALIZED_OHLC candidate; native RAW_OHLC NOT_VALIDATED; TOTAL_RETURN_REFERENCE separate | Offline validation only; no live Yahoo retrieval without permission |
| Japan Trial Fresh State | BIGDATA / FMP | RESEARCH_ONLY | price_semantics UNKNOWN; latest quote-like price with unknown delay; volume_semantics UNKNOWN | FRESH_STATE_ONLY, research observations; future forecast-context candidate subject to separate approval |
| Japan Outcome Scoring | NOT_VALIDATED | NOT_VALIDATED | Required: verified official regular-session Open/Close, session identity, documented corporate-action basis | Disabled; Bigdata company_overview price prohibited |
| US Trial Historical | Existing routing candidate ALPACA; conditional MASSIVE fallback | NOT_VALIDATED for this role-specific contract | RAW_OHLC candidate; adjustment/action/coverage contract NOT_VALIDATED | No new validation or admission in this phase |
| US Trial Fresh State | Existing routing candidate ALPACA; conditional MASSIVE fallback | NOT_VALIDATED for this role-specific contract | Quote type/feed/session/delay NOT_VALIDATED | Disabled pending separate contract validation |
| US Outcome Scoring | NOT_VALIDATED | NOT_VALIDATED | Required: verified official Open/Close and explicit feed/action basis | Disabled |

US rows do not erase prior samples or change provider_registry.json eligibility/routing.
Japan Primary remains AUTO_FREE_UNRESOLVED. FRESH_STATE_ONLY is a role restriction, not an eligibility enum or Primary selection.

## Bigdata Japan restrictions
- Source tool: bigdata_company_tearsheet, company_type Public, sections [company_overview].
- Preserve source price as latest_price or source_reference.source_price; never silently assign it to normalized close/open.
- Price semantics: UNKNOWN among OFFICIAL_CLOSE/LAST_TRADE/REALTIME_QUOTE/DELAYED_QUOTE/ADJUSTED_CLOSE/OTHER. General quote documentation does not prove Tokyo real-time entitlement or exact trade definition.
- Volume semantics: UNKNOWN; preserve volume as unclassified source value. Do not use as verified EOD/session volume, liquidity threshold input, or a last-trade size. average_volume is separate.
- Raw/adjusted, split, dividend, corporate-action processing and historical revision behavior for this price field: UNKNOWN.
- Normalized OHLC and series_type stay null when absent/unproven; primary_price_direction_eligible=false. Unknown numeric volume semantics must remain flagged and excluded from volume-dependent decisions.
- Forecast Input: future contextual quote candidate only; currently disabled. Outcome Scoring: NO. Threshold/history calculations: NO.
- Receipt before freeze proves access to that snapshot at receipt, not official close, first publication, perpetual freshness, or SLA.
- Store provider timestamp, provider response as_of, local request/receipt independently; available_at/published_time remain null if unknown. A session date derived from timestamp is not a source date field.

## Historical / scoring separation
Keep RAW_OHLC, SPLIT_NORMALIZED_OHLC and TOTAL_RETURN_REFERENCE separate. No dividend-adjusted Total Return in primary stock-direction evaluation.
Required future scoring data must identify official session Open/Close and action basis consistently with existing horizon/return methodology. No new methodology is approved here.
Do not infer missing Open from quote price, or build five-year distributions from snapshots.
Preserve cache vintages and the MUFG 2022-05-17 DATA_CONFLICT. No interpolation, averaging, repair or backdating.

## Cost / admission gates
COST_STATUS = UNVERIFIED for the current Bigdata connection.
Help metadata exposes corporate-fundamentals access and PAYG-related information, but no verified unit tariff, currency, free entitlement, per-call debit, or contract terms for this use. Connection success is not zero-cost evidence.
No top-up, purchase, billing change, additional subscription, or automatic retrieval is authorized by this contract.
Before Trial: resolve cost and usage rights, validate each role's semantics/adapter/quality, and obtain explicit human approval. No provider may supply an unvalidated role merely because another role passed.

## Evidence
See ../analysis/bigdata_fmp_japan_price_semantics_2026-09-15.md and the unchanged Phase 2B-1/2B-2 JSON snapshots.
Registry remains unchanged: this file documents candidate restrictions without rerouting or changing eligibility.

## Phase 2C-1 outcome-source addendum (2026-09-15)
This dated addendum is current for Japan Short scoring. Earlier sections remain as audit history. No source is selected or admitted.

Japan Forecast Fresh State: BIGDATA, FRESH_STATE_ONLY, RESEARCH_ONLY eligibility; outcome_scoring=DISABLED. Quote is not Actual Close.
Japan Historical Features: existing yfinance cache is RESEARCH_ONLY; Yahoo permission unresolved, no new automatic retrieval. Full historical/action validation remains incomplete.

Japan Outcome Scoring:
- provider: NOT_VALIDATED (none selected)
- eligibility: NOT_VALIDATED
- open_semantics: UNKNOWN for selected source; minimum future admission PROVIDER_DAILY_OPEN, or verified OFFICIAL_SESSION_OPEN
- close_semantics: UNKNOWN for selected source; minimum future admission PROVIDER_DAILY_CLOSE, or verified OFFICIAL_SESSION_CLOSE
- series_type: RAW_OHLC (chosen contract target, not a claim of validated data)
- corporate_action_rule: use documented unadjusted Open and Close of the SAME Tokyo session. Do not bridge the overnight split boundary or apply a dividend reinvestment factor. Keep corporate-action metadata and immutable vintages; flag unsupported actions, inconsistent basis or missing values; never repair automatically.
- retrieval_timing: after that session closes or the following morning, independently of forecast Freeze. No 08:45 freshness requirement for scoring. Missing outcome remains pending, not a substitute quote.
- cost_status: UNVERIFIED for an admissible timely source
- terms_status: TERMS_UNCLEAR for an admissible timely source
- OUTCOME_SCORING_SOURCE_VALIDATED: false (machine-readable gate in minimal_forward_trial_config.json)

J-Quants Free: DELAYED_AUDIT_ONLY conditional role, not active admission. Documented daily O/C are before adjustment (PROVIDER_DAILY_OPEN / PROVIDER_DAILY_CLOSE), but Free excludes the last12 weeks. Public plan costs0; API/CLI automation is supported. Full current individual terms, account/key, per-ticker coverage and retention conditions remain unverified. No paid plan or registration requested.
J-Quants AdjO/AdjC may include rights-issue adjustments as well as splits, so do not blindly relabel them SPLIT_NORMALIZED_OHLC. RAW_OHLC avoids this ambiguity for same-day direction scoring; historical features still require a separately validated contract.

The existing trial_score_schema.json currently accepts only OFFICIAL_OPEN/OFFICIAL_CLOSE constants. This phase's broader documented-daily minimum does not bypass that schema: a versioned schema/adapter change with tests is required before a PROVIDER_DAILY_* source can be admitted. No enum alias may upgrade provider-daily provenance to official provenance.
Gate may be proposed true only after all listed evidence checks pass; it never overrides false execution gates or human start approval. No executable enforcement engine is claimed here.

## Phase 2C-1B 手動JPX候補の追加（2026-09-16）

日本Short採点の現在の候補は以下です。過去節は履歴として保持します。「候補を指定した」ことと「採点適格性を検証した」ことは別です。

|項目|値・確認状況|
|---|---|
|provider|JPX_DAILY_REPORT_MANUAL|
|資料|JPX 東京証券取引所日報・株式相場表|
|eligibility|NOT_VALIDATED|
|acquisition|MANUAL_DOWNLOAD_ONLY|
|automation_web_fetch / WEB_AUTO_DOWNLOAD|DISABLED|
|local_parse|ENABLED（ローカル処理の許可。Parser実装完了ではない）|
|publication_lag|NEXT_BUSINESS_DAY（ユーザー提示の説明。公式説明の独立確認は未実施）|
|cost|ZERO_ADDITIONAL_COST（採用条件。現行条件の確認状態はUNVERIFIED）|
|terms_status|TERMS_UNCLEAR。手動取得・ローカル保存・処理条件の公式根拠は未確認|
|series|RAW_OFFICIAL_DAILY_OHLC_CANDIDATE|
|open_semantics|OFFICIAL_SESSION_OPEN候補。確定値はUNKNOWN|
|close_semantics|OFFICIAL_SESSION_CLOSE候補。確定値はUNKNOWN|
|parser_status|PARSER_PENDING_REAL_SAMPLE|
|OUTCOME_SCORING_SOURCE_VALIDATED|false|

ユーザー提示の公式説明は「個別銘柄ごとの四本値・売買高等を掲載し、立会日の翌営業日に掲載」。本PhaseではJPX Webへのアクセスとファイル取得を行っていないため、独立確認済みと記載しません。四本値とは始値・高値・安値・終値です。取引区分、価格・出来高の単位、調整基準は実物と資料仕様で確認します。

同一立会日の掲載Open/Closeをそのまま使う方針で、Adjusted CloseやTOTAL_RETURN_REFERENCEには変換しません。Corporate Action日はflag、基準不明ならPENDING_DATA。詳細な事前ルールと取込設計は `../providers/jp/jpx_manual/README.md` を参照。

翌営業日の手動取得後にローカル取込・検証・将来の採点を行う設計で、08:45のFreshness要件は課しません。資料未入手中は保留し、Bigdata Quoteで代替しません。

Japan Forecast Fresh StateはBigdata FRESH_STATE_ONLY、採点禁止を維持。Historical Featuresは既存yfinance CacheのRESEARCH_ONLYを維持し、新規自動取得はしません。USの状態も変更しません。

Web取得権限とローカル処理権限を分離します。手動経路のゲートにWeb自動取得の許可は要求せず、手動取得・保存・ローカル利用の条件を確認します。実ファイルCore6抽出と全要件の検証前はゲートfalse。将来true候補となってもTrial開始承認ではありません。

## Phase 2C-1C 実物検証追記（2026-09-18）

ユーザー手動取得 `stq_20260915.pdf`（SHA-256: b998d1cd1d900f2185c37365b44fb270b93e642421931ca9a48cb10720852aae）の内部日付は2026-09-15。株式相場表・立会市場普通取引の見出し、午前/午後の始値・高値・安値・終値、売買高（千株）、売買代金（千円）を確認しました。Core6抽出・数値整合・原本ハッシュ不変・Normalized SchemaはPASS。

日次Open=午前始値、日次Close=午後終値、High/Low=両場の最大/最小。表に単独の日次四本値列があるわけではなく、確認した列からの導出です。open_semantics=OFFICIAL_SESSION_OPEN候補、close_semantics=OFFICIAL_SESSION_CLOSE候補。Volumeはこの立会市場普通取引表のSESSION_CUMULATIVE_VOLUME（株に換算）であり、立会外等を含む全取引合算とは主張しません。

series_type=RAW_OHLCは当日掲載値を調整せず使用するAdapter方針です。履歴の調整規約まで証明したものではありません。Corporate Action=UNKNOWN、price_basis_status=NOT_VALIDATED、quality_state=PARTIAL_DATA、primary_price_direction_eligible=falseを保持。

Parser=PASS_SINGLE_REAL_SAMPLE。利用条件=TERMS_UNCLEAR、現行Source費用条件=UNVERIFIEDを維持（今回のローカル処理で有料契約なし）。適格性NOT_VALIDATED、OUTCOME_SCORING_SOURCE_VALIDATED=false。出版遅延・ダウンロード日時は実測していません。Web取得なし、Bigdata/yfinanceの役割変更なし、Trial開始なし。

## Phase 2C-1E Computer Use候補とRetention（2026-09-19）

将来候補の取得OriginとしてCOMPUTER_USE_BROWSERを別扱いで設計。現行の人間手動取得サンプルと既存ParserのMANUAL_DOWNLOAD_ONLYは変更していません。`jpx_computer_use_policy.json` と `raw_data_retention_policy.json` を参照。Browser操作だけをComputer Useに担当させ、HTTP script取得は引き続き禁止。個人研究専用で配布・商用化・高頻度・アクセス制限回避は禁止です。

ONE_SHOT=NOT_RUN、技術的成立性=UNKNOWN、日次取得=false、Schedule=false、Trial=false。利用条件・費用の確認状態を昇格しません。技術的なBrowser取得成功は利用許諾の証明ではありません。

Retentionは実際のdownloaded_atのJST日付から30暦日。全保存/採点/対応/品質条件を満たすNORMAL/BACKFILLだけがゴミ箱候補です。既存2026-09-15 PDFはREGRESSION_FIXTUREとして期限なし保護。取得時刻不明のためtrash_afterはnull。CleanupはLocalのみ、完全削除Fallback禁止。現時点ではCleanup・OS実バックエンドも無効で、Manifest dry-run以外は実行しません。

## Phase 2C-1F-R2 公開予定日対応（2026-09-20、準備）

翌営業日掲載という仕様と既存JPX Calendarに基づき、2026-09-18のexpected_publication_business_dateは2026-09-24。9月21〜23日は休業日です。9月24日前の未掲載は正常なNOT_YET_AVAILABLEで、Missing/Failureにはしません。当日未掲載はPENDING_PUBLICATION、予定日後も未掲載ならMISSING_SOURCE_CANDIDATEに留め、人間確認前にBackfillへ登録しません。

9月24日18:00 JSTに1回観測する手順を準備しましたが、Scheduleは作成していません。正確な掲載時刻はUNKNOWN、通常取得時刻もNOT_VALIDATED。旧08:00候補は未採用であり、今後は確実な掲載後の1日1回を検討します。今回の準備ではWebアクセス・実測・取得はしていません。

## Phase 2C-2 現在の取得経路（2026-09-25）

上の各節は当時の履歴として残す。現在の技術的候補は provider=`JPX_DAILY_REPORT`、acquisition_method=`BROWSER_ASSISTED_SINGLE_FILE`、browser_actor=`CHATGPT_DESKTOP_BUILT_IN_BROWSER`。`web_auto_bulk_fetch=DISABLED`、`human_manual_download=SUPPORTED_FALLBACK`、`local_parser=ENABLED`。個人研究のみ。2026-09-24の9月18日分1件で Browser→Raw PDF→既存Parser→Core6 6/6→Normalized Schema が `PASS_ONE_SHOT`。過去の9月15日手動取得レコードとv0.1 Parser出力は変更しない。v0.2以降は取得方法を明示して出力する。

この技術的成功は自動ブラウザ取得の利用許諾、費用条件、継続的な掲載時刻、Corporate Action処理、採点Source適格性の確認ではない。`terms_status=UNRESOLVED`、`cost_status=UNVERIFIED`、`OUTCOME_SCORING_SOURCE_VALIDATED=false`。通常採点のローカルコードはLLM Calls 0とするが、実行ゲートは無効。Browser取得の日次時刻23:00 JSTは候補に過ぎず`CANDIDATE_NOT_VALIDATED`。
