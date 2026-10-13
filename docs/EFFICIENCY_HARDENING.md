# Task 10C-OPT-1: Forward基盤の効率化とgate維持

作業対象は `dev/efficiency-hardening-v1`、baseは
`f60fbba965ee858ca6e7a3afefbdb4e09655a4f0`。
本番worktree、本番branch、稼働中monitor state、systemdは変更しない。
このbranchのpushはmergeや稼働コードの切替を意味しない。

## 変更と数値

| 項目 | before | after |
|---|---|---|
| Legacy: 無関係なcommitによるrenewal要求 | YES | NO |
| monitor: repository全体HEAD一致 | 必須 | audit情報のみ |
| historical extensionの重い共通検証/session | Core6で約6回 | 1回 |
| deadline expiryによるtracked config変更 | YES | NO |
| deadline finalization | potentially unbounded restart | initial + 最大2 retries、合計3 attempts |
| deadline retry HTTP | 0 | 0 |
| ProductionのProbe module依存 | あり | なし |

Primaryはequal-linear-pool-v1、Base Rate/Momentum/MA Trend各1/3、
5pp deterministic rounding、tie FLAT > UP > DOWN、HIGH >=60/MEDIUM >=45、
relative_strength missing_optional/RULE_NOT_FROZENを維持する。
Threshold short-oc-p33-v1、Baseline minimal-baselines-v1、parser sourceは変更しない。
実データでのFresh、JPX live、Forecast、Freeze、scoring、PIT write、trainingは実行しない。
回帰テストの生成物は隔離したsynthetic fixtureのみ。

## Acquisition fingerprintの根拠

historical_directのofficial URL/redirect/challenge/discovery/request/acceptance、
STOP lifecycle/manual review、parser/acquisition、取得policy/schemaを追跡した。
FIX_PATHSだけを実行bindingとして使わない。

全byte対象:

- providers/jp/jpx_manual: historical_direct.py、historical_policy_v2.py、
  legacy_stop_review.py、parser.py、acquisition.py
- config: jpx_daily_acquisition_config.json、normalized_market_schema.json、operation_policy_v2.md

到達するengine helperの対象:

- minimal_trial: CORE、ROOT、canonical_bytes、sha256、read_frozen、
  _resolve_jpx_raw_source、load_parsed_jpx
- official_gap: ROOT、MISSING_ALLOWLIST、EXISTING_SESSIONS、GAP_START/GAP_THROUGH、
  expected_gap_sessions、stage_existing_pdf
- shared artifact: lock、seal、directory fsyncと、それらのimport alias

選択関数はsourceをhashする。module imports、module-level実行文、関数header/default/decoratorも
hashし、annotated/conditional/global再代入を見落とさない。
取得に到達しない関数body、README、monitor、独立した無関係engine、Shadow ML、reportは除外する。
module-level実行やimport変更はhelperを変え得るため、保守的に再検証対象になる。

既存receiptのcommit SHAはGit audit lineageを検証するために残す。
最新旧renewalでは記録commitのcomponent snapshotを現在集合と比較する。
このmechanical refactorはexact before/after mappingのみで移行する。
mapper/mappingは指定baseの子孫である初回導入commitのbyteと照合する。
比較済みmapping bytesをbinder内strict JSONで解析し、再読しない。
既存generation4のcommitからの集合は指定baseのbefore集合と完全一致した。
before 30、after 35 logical component hashes。
新receiptはinfrastructureも含むnative fingerprintを保存する。
初回release authoringでは現在fix bytes、renewal authoringではclean/current commitを引き続き要求する。
既存generationのfix refs、validation reports、STOP、discovery、hash sidecarsは保持・検証する。
最適化だけを理由にgeneration5を要求・生成しない。

## Runtime NOT_RUN

tracked policyは10/13の既存決定とdeadline後backfill禁止を保持する。
新しいmissed sessionは `state/forecast_not_run_sessions/YYYY-MM-DD.json` に保存する。
execution_policy.not_run_sessionsを経由してstatic/runtime unionを読む。
Forecast target選択とprospective PIT validationを含むconsumerの直接config list参照を除去した。

各recordはtarget_session/reason/source/aware recorded_atとschemaを持ち、canonical hash envelopeで固定する。
no-follow directory-fd traversal、flock、fsync済みtemporary fileからexclusive hard-link publicationを使う。
先行するatomic hash-bound inventoryにcreate-once recordの正確な内容を保存する。
repository rootの独立witnessでstate/ledger directory全体の欠落も検知する。既存witnessがある場合、
消失・空directoryからlock/inventoryを再初期化しない。witnessはlock取得済みの完全なbytesを
atomic公開し、初回並行writeでもpartial fileや一時的なhard-link countを誤認しない。
欠落・破損・duplicate JSON key・異なるduplicate・clock rollbackはfail closed。
不完全publicationはreaderを閉じ、writerはinventoryの正確なrecordを復旧する。
完全なledgerとhashを自由に書き換えられるlocal accountへの認証機構ではない。

## Deadline retryと共通artifact library

deadline_retry.jsonのattempt予約はbookkeeping前にdurable保存する。
初回含め3回まで、restart/concurrent callを含め1時間未満では予算を消費しない。
独立deadline_reserved witnessをattempt予約前に保存する。STOP/latest公開前の中断でも、
retry fileが失われた場合はfail closed。
成功completionをretry envelopeに保存し、latest/STOP保存途中の中断を復旧する。
clock rollbackとcorrupt retry fileもzero HTTPでSTOPとなる。
serviceはRestart=no、explicit driverは2 sleepsまで。最終STOPでは両timerをdisarmする経路を持ち、
service failure時のOnFailure unitもSTOP保存と両timer disarmを試みる。
生成unit文字列だけを検証し、実systemd操作は行わない。

forward_artifactsは既存read-only validationとforward_storageのprivate互換aliasを提供する。
one_session_dry_runはこのlibraryを使い、productionからProbeをimportしない。
create-only bundle commitはstage durability後に最後のcutoff/source confirmを行う。
historical.verify_dayはday共通record/HMAC/policy/raw/parsed/manifest検証後にCore6 mapを返す。
各executionで再検証し、processをまたぐcacheはない。Freeze直前の独立source再検証は残す。

## 検証とreview

追加34 regressionsは、無関係commit許容と取得component変更STOP、monitor identity変更STOP、
tracked tree clean、runtime NOT_RUN、corruption/duplicate/conflict/concurrency/partial publication、
bounded zero-HTTP retry、時計rollback、symlink、missing ledger、completion復旧、重い検証count、
最終cutoff guard、不変semanticsを確認する。

| 検証 | 結果 |
|---|---|
| focused | 65 PASS、うち追加regression34 |
| related regression | 380 PASS |
| full offline（1回） | 707実行、704 PASS、3件はignored fixture準備不足で読み込みerror |
| fullで失敗した3件のfocused追試 | 既存fixtureをhash一致コピー後、3 PASS。コード変更・full再実行なし |
| public-tree（1回） | Git archive、ignored data/stateなし、481 PASS |
| diff check | PASS |

full offline全対象はfull実行と失敗3件の追試を合わせて確認した。一括実行の結果を全greenとは扱わない。
public-tree対象はPARTIAL_TICKER記載のsynthetic subsetに、legacy/release/monitor/NOT_RUN/Forward/PIT/
parallel model/training/admissionのsynthetic suitesを追加したもの。
検証用audit guardでlive socket接続、production write、実worktree data/state/config write、
systemd操作、テスト中remote Gitを拒否した。実市場データでのtraining/Forecast等は0。
original STOPとgeneration 1–4は新bindingで読み取り検証し、store 940 filesのhash不変と
new generation=0を確認した。
Code Review初回6指摘と追試2指摘を修正した。Security初回3指摘、その後のmapping再読、
retry ledger欠落、ledger再初期化、witness初期化raceを修正し、反例testと独立source reviewで確認した。
Security reviewのsource確認と修正追試は行ったが、pluginのcanonical report保存は親directory権限により
拒否された。既存scanとcandidate/validation/attack-path記録は保持し、保存先権限の変更は承認待ち。
plugin reportをsealed/completedとは扱わない。

残る運用前提はlocal fsync-capable filesystem、trusted source/Git/local account、cooperative locks。
初期inventory/lock marker保存の中断や回復不能なfilesystem failureはmanual reviewを要するSTOPとなる。
現在の2-file armed evidenceは読めるが、新ledger module hashを欠くため変更コードの実行は閉じる。
稼働monitor終了後の別Taskで、mergeとstate/unit切替を判断する。


merge readinessはNO。コード検証は上記範囲で完了しているが、正式Codex Security reportの
canonical保存・sealが未完了である。エラーは
`Scan output parent must not be group- or world-writable without the sticky bit.`。
保存先の祖先 `~/.codex` は0775で、0700への変更は承認待ち。本Taskから権限を変更しない。
既存scan ID `7e025114-659d-454f-98d2-a4b9ec1bfca8` とcandidate/validation/attack-path記録は保持した。
コードの未解決review指摘は0。正式report完了と稼働monitor終了後の別Taskでmerge可否を判断する。
