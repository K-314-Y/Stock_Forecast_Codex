# JPX手動取得経路のローカル処理設計

## Reviewed legacy release renewalと旧attempt（2026-10-12）

[Policyのgeneration/supersession規定](../../../config/operation_policy_v2.md)に従う。
既存receiptはgeneration 1として不変で保持し、明示的manual renewalでgeneration 2以降を追加する。
latest generationだけがcurrent source/commitをbindする。以前のgenerationもsealed evidenceとlineageを
再検証するが、旧source hashをcurrent working treeへ要求しない。source変更後はrenewalまでfail closed。
renewalはclean/committed code、新regression PASS証跡、exact previous receipt/STOPを要求する。
orphan、欠損sidecar、duplicate/fork、壊れたprevious evidenceはapproval扱いしない。
通常取得・schedulerからrenewalを自動呼出ししない。

rootにsealed start/failureの4ファイルだけがあり、failureがreview済みexact旧STOPに完全一致し、
`attempts/`が存在しない場合だけ、旧証拠を保持して`attempts/000001/`を一度開始できる。
新attemptの途中crash、追加artifact、未review/unrelated failureは従来どおり停止する。
renewal/recognitionのoffline検証だけではfresh acquisitionを開始しない。

## Historical DirectのSTOP / Retry / Resume v2（2026-10-11）

Historical Directだけ[確定Policy v2](../../../config/operation_policy_v2.md)へ移行。
`acquire_one`とJSON diagnosticは未解除HARD/host/global STOPを共有し、既知の局所metadata/format failureだけSOFTへ分類する。
未知の旧`STOP.json`は`HARD_STOP_LEGACY_UNCLASSIFIED`。旧本文・Raw・失敗attemptは不変で保持する。
current STOP summaryは`current_stops_v2.json`、追加STOP/RESUMED証跡は`stops_v2/`。

`run_state=historical_direct.RunState()`を同一runで共有すると、listing/JSON/PDFを通したsession別retry予算が最大2回となる。
予算はrun記録とcurrent summaryのtarget別stateへ保存し、run ID変更・diagnostic・再開でも引き継ぐ。
上限到達後は同じAsia/Tokyo日付の新run IDでも要求0。Policyの「後日の通常run」は別の観測機会とする。
応答不明の中断requestはreview待ちとし、run ID変更で初回attemptを復活させない。403/challengeはretry 0。
public diagnosticは常にstore lock/STOP gateを通り、内部contextの注入による迂回は受け付けない。
429再試行も429ならPolicy 4節に従い、その時点でhost停止。Retry-Afterは尊重する。
transient上限はsession SKIP。同日の同一session再呼出しは保存済みSKIPを返し、run ID変更で連続retryしない。
正常200で429 streakをresetする。security判定時は例外を外へ返す前にdurable STOPを保存する。
公式JSON/PDFのoctet-streamはdecode/schema/parser/provenance PASS後にContent-Type WARNを保存する。

SOFT resumeは`resolution=<JSON path>`を渡す。resolutionには`stop_reference`（path/sha256）、`scope`、`target`、
`root_cause`、`scope_isolation=PASS`、現在の修正code/configを指す`fix_refs`、`validation_refs`、旧STOPと同じ`evidence_refs`が必要。
validation reportは`status=PASS`、`checks={fixture: PASS, regression: PASS}`と同じ`fix_refs`を持つ。
各参照は`historical_policy_v2.reference(path)`のpath/sha256形式。PASS表記だけでは解除せず、保存済み公式リンク・Raw hash・PDF・実parser・既存validatorをoffline再検証する。
再検証で新しいintegrity conflictが判明すれば旧SOFT証拠を保持したHARD_STOPへ昇格する。
STOP/RESUMEDはatomic create-only + file/directory fsyncで保存する。
current summaryは旧STOP・resolution・resume receiptを参照し、receipt欠損/破損ならfail closed。
RESUMEDをdurable保存できた場合だけ解除し、別attemptに結果を保存する。Rawの再downloadや旧attemptの上書きはしない。

有効な既存bundle/Manifestは再検証してrequest 0。証拠が未確認の既存ファイルも再取得せず`EXISTING_LOCAL_REQUIRES_VALIDATION`を返す。
host STOP中も安全なoffline検証/SKIPは許可し、network boundary直前では必ずSTOPを適用する。
Partial tickerは`DEFERRED_PARTIAL_TICKER`（Task 7C/7D）。全6件必須schemaとconsumerの移行を別変更に分離する。
通常Browser経路・Forecast engine・登録gateは今回の移行対象外。以下の旧手順は履歴として保持する。

## Historical Direct限定例外（2026-10-08）

`historical_direct.acquire_one(root, session, transport=...)` は1 Session限定の取得adapter。
対象はconfigの2026-10-05〜07だけ。transportを省略できず、importやCLIで通信は開始しない。
通常Browser方式・Fresh Close・Forecast・Outcome取得・scheduleは変更しない。
公式一覧の正常なHTML構造と対象行の株式相場表hrefを確認し、各redirect先を通信前に検査する。
制限・未知応答・途中停止・Parser失敗後に自動retryしない。失敗時のSTOP証跡は人間確認まで保持する。

`data/jpx_historical_direct/<session>/` にRaw、listing HTML、Direct取得証跡、Parsed、検証layerをcreate-only保存する。
取得metadataは `OFFICIAL_LISTING_DIRECT_HTTPS` / browser_actor=null。Parser v0.2.2はこのmetadata許可だけを追加し、抽出・Core6・OHLC・Schema検証を再利用する。
成功は `VALIDATED_PENDING_EXPLICIT_REGISTRATION`。candidate Manifest entryは実Manifestへ自動追加しない。
次の明示的な登録では、Rawを既存Inboxへcreate-only配置（既存ならhash照合して再利用）し、candidateのfilenameと実Raw、Parsed hash、取得証跡を照合したうえでManifestへ登録する。
その後、既存 `engine/historical_close_bridge.py` を使用する。新しい取得Originをmanual/browserへ置き換えない。
実通信は未検証、Terms=UNRESOLVED、Forecast/Trial gateは無効のまま。

## 2026-09-25 現在の補足

旧手動取得手順は履歴として維持する。現在の単発技術試験では、内蔵BrowserでJPX日報PDFを1件保存し、ローカルParser v0.2へ `--acquisition-method BROWSER_ASSISTED_SINGLE_FILE --browser-actor CHATGPT_DESKTOP_BUILT_IN_BROWSER` を明示する。旧手動サンプルは引数なしの `HUMAN_MANUAL_DOWNLOAD` として引き続き解析可能。過去のv0.1出力は上書きしない。Browser経路のWeb大量取得は禁止、日次取得・Schedule・Trialは無効、JPX TermsはUNRESOLVED。Parserはネットワーク取得を行わない。

## 実物検証追記（2026-09-18）

現在は `PASS_SINGLE_REAL_SAMPLE`。以下の初期設計は履歴として残します。`parser.py` を実装し、手動PDF `stq_20260915.pdf` の2026-09-15・Core6抽出と既存Normalized Schema検証に成功しました。全406ページをローカルでセクション・銘柄コード検索し、該当6ページだけのセルを抽出します。全文をLLMへ渡す必要はありません。

大見出しで確認したセクション番号を継続ページへ引き継ぎ、見出しと罫線から列を特定。前場始値、後場終値、両場の高値最大・安値最小を採用し、元の四本値も保持します。千株は1000倍して株数、千円は1000倍して円へ単位換算。価格調整は行いません。どちらかの場の値が欠ける場合は停止します。

出力先は `data/outcome_parsed/jpx/` に変更。既存出力は上書き拒否です。`retrieved_at` はローカル読取時刻で、手動ダウンロード・公開時刻ではありません。不明な時刻はnullです。

依存: Python 3.12、pdfplumber 0.11.9、pypdf 6.9.1、jsonschema 4.26.0。通常の実行形は `python -B providers/jp/jpx_manual/parser.py <manual.pdf> --expected-session YYYY-MM-DD --output <new-output.json>`。テストは `python -B providers/jp/jpx_manual/test_parser.py`。プロジェクトルートで実行します。Parser自身はネットワークを利用しません。

Corporate Actionのコード欄凡例はPDFの画像部分にありますが、機械読取・発生日判定は未実装です。状態UNKNOWNを保持し、適格性はNOT_VALIDATED、ゲートfalse。前後場の例外・休場日・将来レイアウト・利用条件・現行費用条件は別途検証が必要です。通常採点LLM Calls=0の方針は維持しますが、本Parserは採点エンジンではありません。

## 初期設計（履歴）

状態: `PARSER_PENDING_REAL_SAMPLE`。実ファイル形式が未確認のため、Parserを実装したとは扱いません。本書は実装仕様です。ネットワーク取得機能は作りません。

## 対象

|コード|会社|
|---|---|
|6857|アドバンテスト|
|7203|トヨタ自動車|
|8306|三菱UFJフィナンシャル・グループ|
|1605|INPEX|
|9433|KDDI|
|3197|すかいらーくホールディングス|

銘柄コードと会社名・市場の対応を資料で確認します。ADRや別市場で代替しません。

## 将来の処理順序

1. 手動取得原本を読み取り専用で扱い、処理前後のSHA-256一致を確認。
2. 実物の形式と列定義を確認して決定論的Parserを実装。スキャンPDFで安定抽出できなければ人間確認を要求し、LLM全文読込で代替しない。
3. 本文から立会日を抽出。発行日・取得日と区別し、既存JPX Calendarと照合。
4. 日中全体の四本値か、前場・後場・別取引区分かを確認。列名だけから終値と断定しない。
5. 円・株などの原単位を保存。千株等の場合は資料で確認した倍率だけで単位換算し、元値と倍率を保持。価格の調整とは区別する。
6. Core6の一意性、日付、Open/High/Low/Close/Volume、通貨、市場を検査。繰返し見出し・重複銘柄・欠損・非正価格・負出来高・OHLC大小関係の異常は停止flag。ゼロ出来高は人間確認。
7. 正規化結果は別フォルダへ。原本ハッシュ、抽出箇所、Parser版、取得日時、公開日時（不明ならnull）、資料定義の根拠を保持。
8. 実物でCore6抽出・Schema検証・原本不変性がPASSし、定義・利用条件・費用条件も満たした場合のみゲート変更候補とする。現在はfalse。
9. 将来の開始承認後のみ、固定済みForecastと同一立会日の結果をローカル採点。予測時刻の入力とは分離する。今回は採点しない。

## 事前のCorporate Actionルール

Corporate Action（分割等の権利変更）発生日はflagを保持します。通常の翌日分割でも、当日のOpenとCloseが同じ未調整基準と確認できれば、それだけを理由に除外しません。自動補正・配当再投資・日をまたぐ価格接続はしません。

権利変更の有無が未確認ならUNKNOWN。価格基準不明、欠損、取引停止、銘柄識別不明、原本訂正未解決の場合は `PENDING_DATA` として採点しません。解決後は同じ事前ルールで再検証し、結果の良し悪しを見た裁量除外は禁止。保留件数も報告し、AIとBaselineで同じ対象集合を使います。

## Schemaと実装上の残件

既存 `trial_score_schema.json` は `OFFICIAL_OPEN` / `OFFICIAL_CLOSE` を要求します。今回の `OFFICIAL_SESSION_OPEN` / `OFFICIAL_SESSION_CLOSE` 候補との対応は、公式仕様確認後の明示的なAdapter変換とテストが必要です。文字列の置換だけで公式値へ昇格させません。

抽出Schema、実ファイルテスト、Parser、immutable writer、実採点エンジンは未完成です。ローカル処理許可は実装完了を意味しません。

## Token方針

通常採点のLLM Call予算は0。資料全文はLLMへ投入せず、必要な6行と検証要約のみを扱います。Parser破損時は人間確認または限定Reviewとし、自動復旧・Web再取得はしません。
