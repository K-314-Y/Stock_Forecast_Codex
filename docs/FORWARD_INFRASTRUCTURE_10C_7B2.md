# Forward Infrastructure Unblock — Task 10C-7B2

目的は CODE_PATH_READY。JPX availability や ready_for_live_execution とは独立する。
本Taskは公式履歴・既存snapshot・NOT_RUN台帳を変更せず、fixture/mockで経路を検証する。

## Historical admission

旧 `allowed_sessions` と `forecast_target` 由来の `session < previous(target)` cutoffを廃止。
`historical_direct.session_admission=VALIDATED_COMPLETED_HISTORICAL_SESSION` と
acquisition policyに固定したcalendar path / SHAを検証し、範囲内の検証済みJPX sessionで
`session < actual observation day (JST)` のときだけadmissionする。カレンダー更新も新しい
検証済みSHAとの対応が必要。Forecast target、永久NOT_RUN、old targetの日付に依存しない。
同日未完了・future・calendar外・hash不一致・naive clockはnetworkより前に拒否する。

call graph: acquire_one / discover_json_one → _policy → _historical_calendar →
STOP/retry boundary → official listing / observed JSON program / monthly list / monthly Stocks →
official PDF → unchanged parser → stage_existing_pdf。approved listing identity、official HTTPS host、
month / Stocks identity、redirect、CAPTCHA/security challenge、structure / policy gateを保持。
acquire_oneは明示transportが必要で、登録は自動実行しない。Retained acquisitionのpolicy SHAは
sealed startと一致し、実観測・取得timestampの順序と過去sessionを再検証する。

既存releaseのcomponent bindingを新コードに合わせて書き換えない。旧releaseが変更コードを
認可しない場合は既存の正式review/renewalが必要。timer/contextも旧identityのまま保持する。

## Offline manifest and registration APIs

`engine.forward_history.refresh_forward_history_manifest(history_end=None)` は公式履歴の
validated representationを作る。defaultは既存policyのhistorical_through。
HMAC認証済みTask 10A anchor、immutable source hashes、SHA-bound calendar、frozen candidates、
各official parsed/raw entry、direct acquisition discovery、署名済みHistorical Close bridgeを
再検証する。bridgeのない既存anchor行は承認済み表現とoriginal hashesを保持し、新provenanceを
捏造しない。anchor後の新しいcloseは既存Historical Close bridge以外から追加しない。

retention全体のSHAとentry hashesを含むcanonical manifestはtimestampを含まない。
同じvalidated history/evidenceならsame canonical content / SHA / artifact。正当にofficial entry
またはhistory horizonが増えたときだけ新manifestを作る。market valueの書換え、source downgrade、
unknown origin、未検証entry、既存entry変更は認可しない。JSON envelopeはrecordのcanonical SHAを
含み、manifest-<sha>.jsonへcreate-onlyで保存する。戻り値はpath / SHA / history_end。

`engine.forward_history.register_forward_snapshot(manifest_reference,
previous_snapshot=None, reason='OFFICIAL_HISTORY_ADVANCED')` はmanifestをもう一度再構築・照合し、
previous identityのCAS、append-only entry / close prefix、rollback禁止を検証する。
新registrationにはprevious snapshot identity、manifest SHA、history end、calendar / policy identity、
actual registered_at、code commit、reason、HMACを含める。snapshot-<sha>.jsonはcreate-only。
旧history.jsonを編集しない。同じregistrationは実時刻が進んでも同じpath/SHAを返し、競合は拒否。
同じclose horizonでも新official entryによりretentionが正当に進んだ場合は再registration可能。

認証済み `.registration-intent.json` が未完了transactionを表す。pointer書込み失敗後は一致する
intentだけ再開し、同じsnapshotを使用する。競合intentはfail closed。成功後はintentを削除し
directory fsync。成功済みcurrent pointerの喪失は自動再初期化せずfail closed。

既存forward_storageに追加した保存helperを使用し、全directoryをdir_fd / O_NOFOLLOWで固定、
regular single-link fileのみ読み、flockでwriterを直列化する。canonical/hash envelopeを一時fileへ
全量write → flush/file fsync → read-back → source hashes再確認 → exclusive hard-link publication
(manifest/registration) またはatomic replace (current projection) → directory fsync → read-back/hash
検証。fsync失敗をsuccessと扱わない。partial fileは正式artifactとして公開しない。
前提はLinuxのfsync-capable local filesystem、trusted local producer key/source code、cooperative locks。

## Guard and execution integration

旧snapshotはそのままINPUT_CHANGED_BEFORE_FREEZEを返す。refreshだけでも解除しない。
正式registration後、load_validated_historyは署名済みregistrationが指定したmanifest SHA、
policy/calendar identity、actual registration timeと全source hashesを検証する。
load_execution_historyは選択されたhistory endからだけ既存bridgeを追加する。
前営業日のFresh closeをhistorical snapshotで代用せず、history end >= previousは拒否する。
新入力token / actual pre-08:45 clock / one-use / final unchanged guardは従来どおり。

## Availability timing — Case C

結論: **AVAILABILITY_TIMING_UNRESOLVED (Case C)**。
次営業日掲載という既存説明はあるが、通常08:45前に利用可能というSLA/時刻の証拠はない。
確認した既存資料・保存済み観測:

- analysis/jpx_manual_outcome_scoring_validation_2026-09-16.md: 翌営業日掲載はユーザー説明で、
  公式仕様の独立確認なし。
- analysis/jpx_publication_aware_one_shot_r2_2026-09-24.md: 9/18 sessionを9/24 22:50:26 JSTに
  掲載確認。exact publication time UNKNOWN、08:00/18:00の安定性NOT_VALIDATED。
- 既存systemd journal (jpx-publication-20261013.service): 10/14 07:59:28.244659 JSTの
  HTTP 200観測で10/13 NOT_YET_AVAILABLE。
- state/jpx_publication_monitor/2026-10-13/latest.json: 08:45:00.286887のdeadline bookkeeping。
  http_status=nullであり、08:45時点の新しいHTTP観測ではない。
- state/forward_repair_10c7b/20261014T160300917376+0900/publication_observation.json:
  16:03:00.916903に10/13 PUBLICATION_DETECTED。

この1例から掲載時刻は(07:59:28, 16:03:00]の範囲にしか絞れず、08:45前後を断定できない。
通常の時刻分布も不明。毎回前営業日のJPX公式値を08:45前に要求する運用ではNOT_RUNが多発する
構造的リスクが残る。loader自体はpreviousより前のofficial Historical Closeと当日の承認済みFresh
を分けるが、monitorのusable-history completion条件も別に残る。別Taskで複数営業日の既存証拠と
入力availability契約を設計検討する。source / feature / model / threshold / baseline / cutoffは変更しない。

## Invariants and validation

2026-10-13 / 2026-10-14はPERMANENTLY_NOT_RUNのまま。その日の日次market dataが後日official
historical featureとして登録されることはForecast backfillとは別であり、admissionで拒否しない。
Primary equal-linear-pool-v1、weights 1/3 each、5pp rounding、FLAT > UP > DOWN tie、
HIGH >= 60 / MEDIUM >= 45 / LOW < 45、Threshold short-oc-p33-v1、Baseline minimal-baselines-v1、
Relative Strength missing_optional / RULE_NOT_FROZEN、parser semanticsは不変。

追加focused suite: tests.test_forward_history_refresh / tests.test_jpx_general_admission。
テストはsynthetic temporary filesとmock transportのみ。全suiteはaudit hookでsocket接続、
実repo data/state/config/analysisの書込み、systemd操作、remote Gitを拒否する。
public-treeはGit archiveからsynthetic subsetを1回実行する。結果は最終報告に記載する。
Taskからlive JPX HTTP / Fresh provider / real Forecast / Freeze / live PIT / scoringはすべて0。
