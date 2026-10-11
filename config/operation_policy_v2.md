# STOP / Retry / Resume v2

確定日: 2026-10-11。状態: **Policy確定 / 実装未移行**。
基準: `baseline-public-v1` / `6ff68c7dde8d94877a79aec0e1cb9bfae8e6cd40`。

本書はHistorical Catch-upの停止・再試行・再開とDaily Forecastの運用判断を定義する。
現在のdisk上のコード・config・testsを実装状態の正本とし、本書の確定だけでは実行挙動を変更しない。
STOP / Retry / Resumeについて旧Policyと異なる判断は、本書をv2の仕様として採用する。
旧記録・旧ルールは削除せず、末尾の移行表に差と実装上の残件を残す。
取得許可範囲、official host allowlist、calendar、Terms、schedule、Forecast/Trialの有効化gateを拡張しない。

## 1. Evidence Strictness != Stop Strictness

**証拠整合性の厳格さと、運用停止の厳格さを分離する。**
Raw、source identity、URL導出経路、session、ticker、値の意味、hash、必要な時刻・lineageは厳密に検証する。
この検証を弱めて欠損を埋めたり、未検証の値をvalidと宣言したりしてはならない。
一方、軽微な形式差、修正済みsoft failure、optional metadata不足、個別tickerの通常欠損だけで永久停止しない。
停止は安全性・意味論・provenance/integrityの問題、または修復まで対象処理を保留すべき局所問題に限定する。
失敗の分類とscopeを先に判断し、一般例外やSTOPファイルの存在だけで全体を止めない。

Required evidenceは少なくとも、公式source identityと明示された取得経路、対象session/ticker、
RawとParsed/Manifestの参照・hash対応、OHLCの意味と整合性、適用するpoint-in-timeの根拠を含む。
Provider timestampとローカル観測・取得・生成時刻を区別する。時刻不明はnull/UNKNOWNとし、捏造しない。
Required inputの通常欠損は使用対象をSKIPとする。
source identity不明や既存証拠の矛盾・改ざん疑い・意味論不明は、通常欠損として扱わずHARD_STOPとする。

## 2. Output stateとscope

|state|意味と次の行動|
|---|---|
|`PASS`|対象のrequired validationがPASS。対応する証拠参照と集計を保存する。|
|`WARN`|required validationはPASS。許容できる形式差・optional欠損をcode付きで記録し継続する。|
|`SKIP`|対象を処理・予測・採点しない。通常欠損、未公開、既存有効データ、transient retry上限等を理由として記録し、独立した対象は継続する。|
|`RETRY`|許可されたtransient failureのみ、残予算内で同じ対象を再試行する途中state。|
|`SOFT_STOP`|原因・意味論・影響範囲が特定できる修復可能な局所障害。対象scopeを停止し、6節の条件で自動resumeできる。|
|`HARD_STOP`|安全性・provenance・integrity・意味論の問題、またはhost-level persistent 429。自動resume不可。|
|`RESUMED`|停止解除条件を満たした再開event。再開後の最終結果は別にPASS/WARN/SKIP/SOFT_STOP/HARD_STOPとして検証する。|

scopeは`ticker`、`session`、`host`、`global`を区別し、targetにticker/session/host等の識別子を持つ。
`global`は当該Historical Catch-up runまたは同じprovider/evidence系統の関連処理全体を指す。
`host`停止は同じhostへの全要求を止め、次sessionにも及ぶ。
停止範囲を狭めるには、共有source/parser/hash系統に問題が波及しないことの証拠が必要。
範囲不明のprovenance・systemic semantic問題を、個別tickerの欠損として扱わない。
run集計はstate別・code別の件数と未処理対象を残し、一部PASSを全Core6のPASSに読み替えない。

## 3. Historical Catch-upのHARD_STOP

### HTTP / Security

以下は即HARD_STOP。retryは0回。同じ取得目的で別URL・別sessionへ切り替えて回避してはならない。

- HTTP 403、CAPTCHA、security challenge、bot challenge、authentication challenge。
- official JPX外への予期しないredirect。redirect先を通信前に確認し、非公式hostへ接続しない。

HTTP 200や429でもchallenge本文があれば、この禁止を優先する。
CAPTCHA bypass、Cookie抽出、proxy迂回、security bypass、URL guessingは禁止。
403/CAPTCHA/challengeはhost/global STOPとして次sessionにも引き継ぐ。
非公式redirectも当該取得系統を止め、公式経路の確認前に代替pathで再要求しない。
通常の公式HTTPS redirectもallowlist・導出証拠・既存redirect上限を満たす必要がある。

### Evidence / Integrity

以下はHARD_STOP。対象の不確かな値は利用しない。

- hash conflict、source identity不明、source/value/hash不一致。
- session mismatch、ticker mismatch、識別が曖昧な重複ticker。
- OHLC semantic inconsistency、parserの意味論が不明確になった場合。
- URLを推測しないと取得できない状態。
- 同一sessionの異なるRawについて、official revisionか判断不能な場合。

session固有と立証できるmismatch/OHLC/hash問題はそのsessionを停止する。
unknown provenance、systemic parser semantic failure、globalに波及するhash integrity failureはglobal STOP。
hash問題の影響範囲が不明なら共有系統を停止し、原因調査前に別sessionへ広げない。
失敗時も取得済みRaw・receipt・hash・最後の成功状態を保持し、自動削除・再取得で競合を隠さない。

## 4. Transient RETRY

自動retry可能なのはHTTP 429、transient network error、temporary transport failureだけ。
**同一run・同一targetのretryは合計最大2回**。初回を含め最大3 attemptsとする。
同じtargetのdiscovery/downloadの途中で予算を再初期化せず、再開・別pathへの変更も既存予算を引き継ぐ。
HTTP 5xx、未知HTTP status、schema/parser/validation failureを、理由なくtransient扱いしない。

- 429ではRetry-After等の公式待機指示を尊重し、待機中に同じhostの別targetへ要求しない。
- 同一runで同じhostのretryも429となる等、429継続が確認されたら`HOST_PERSISTENT_429`でhost/global STOP。
  残retry予算があっても、そのhostへの追加要求は0。host別名への切替も回避として禁止する。
- 403/CAPTCHA/challengeに変わった時点で即HARD_STOP。待機・retry許可は撤回される。
- security/integrity問題を伴わないtransient failureが上限に達した場合は対象を`SKIP / RETRY_EXHAUSTED`。
  次の独立sessionは継続可能。ただしpersistent 429は上記global停止を優先する。
- attemptごとの実時刻・結果・証拠参照を保持する。無限retry、別run IDでの自動連続再試行は禁止。
  後日の通常runは別の観測機会として扱い、未解除のhost/HARD_STOPを引き継ぐ。

## 5. CONTINUE / WARN / SKIPとsession isolation

### Formal Content-Type mismatch

次の全条件がPASSなら、Content-Typeの形式差だけを理由に停止しない。

1. official JPX host・official URLで、明示された導出経路とredirect chainを確認できる。
2. expected schema/contentとdecodeが検証でき、security/authentication/bot challengeではない。
3. source/session/ticker/値の意味とhash/provenanceが整合する。

例: official JSON endpointが`application/json`ではなく`binary/octet-stream`を返す場合は
`WARN / CONTENT_TYPE_MISMATCH`で継続可能。受信したContent-Typeそのものも証拠に残す。
PDF等も同じ原則だが、拡張子・magicだけでvalidとせず、内容・session・OHLCを含む全検証が必要。
decode/schema検証不能はWARNで通さず、既知の局所形式差ならSOFT_STOP、意味論不明ならHARD_STOP。

### Optional metadata/input

provider timestamp、optional retrieval metadata、optional macro/news/cross-asset inputが欠けても、
required evidenceが揃っていれば`WARN / missing_optional`で継続する。
欠損はnull/UNKNOWNと理由を記録し、fabricationしない。必要な実取得時刻・PIT・hashをoptionalへ降格しない。
既存経路でHMAC等がrequired authentication/integrity evidenceなら省略不可。
optionalと確認できた旧schemaだけを、意味を保つ互換変換で扱う。

### Partial ticker failure

Core6の一部tickerだけrequired inputが通常欠損なら、欠損tickerを`SKIP / SKIPPED_MISSING_INPUT`とし、
他tickerの独立したrequired evidenceがPASSなら継続する。
interpolation、fill-forward、fabricationは禁止。全ticker欠損ならsessionをSKIPし、全体STOPにしない。
ただしsession/sourceそのもののintegrity failureやsemantic inconsistencyは3節のSTOPを優先する。
partial結果を全Core6検証済みsession、正式な全件Manifest、全件採点完了として登録しない。
新しいpartial状態の保存・既存validatorとの対応は実装時に明示し、現在の全6件必須schemaを偽装しない。

### Session isolation

未公開・通常欠損・transient上限・局所SOFT_STOPなど単一sessionの通常failureで、次sessionを自動停止しない。
次sessionは既存の取得許可範囲・calendar・MISSING_ONLYの範囲で進めてよい。
403、CAPTCHA、security/bot/authentication challenge、host-level persistent 429、unknown provenance、
systemic parser semantic failure、globalに波及するhash integrity failureはglobal STOPとして例外扱いする。
isolated HARD_STOPでも当該sessionの自動再開は禁止し、影響が独立と立証済みの次sessionだけ継続できる。

## 6. STOP lifetime / recoverability / Resume

STOPは永久ロックではなく、対象・原因・解除条件を持つ証拠記録とする。
概念上、少なくとも以下を保持する。これは新しい実行schemaの実装ではない。

|field|内容|
|---|---|
|`stop_code`|分類可能な原因code。一般例外名だけで分類を完了しない。|
|`scope` / `target`|停止範囲と対象識別子。|
|`evidence`|Raw/receipt/validation/fix/testのhash・参照。|
|`created_at`|実際のSTOP記録時刻。timezone付き。|
|`recoverability`|`HARD_STOP`または`SOFT_STOP`。|
|`resume_condition`|解除に必要な検証・外部状態・確認。|

加えてstop ID、run/attempt ID、Policy version、reason、last successful stateを参照できるようにする。
旧STOP本文は不変で保持し、`resolved` / `superseded` / `resumed`は参照付きのappend-only eventで記録する。
`superseded`は後継STOPへの引継ぎであり、それだけで解除済みにしない。

**HARD_STOP**: CAPTCHA、403、security challenge、unknown provenance、hash conflict、semantic ambiguity等。
自動resume不可。人間確認、または明確で独立に検証できる外部状態変化に基づく明示的な解除判定が必要。
外部状態変化の根拠とtarget evidenceの再検証結果を保存する。
単なる時間経過、STOP削除、run ID変更、別URLからの再要求は解除根拠にならない。
host persistent 429も同一runでは解除せず、次runへの無条件retryで迂回しない。

**SOFT_STOP**: parserの既知format差、temporary local validation failure、修正可能なmetadata/schema mismatch。
official source・semantics・integrityが既知で、局所修正と影響範囲を説明できる場合に限る。
未知のprovenance・意味論・hash競合をSOFT_STOPへ自動降格しない。

SOFT_STOPは以下の**全条件**を満たせば、旧STOPファイルが存在していても自動resumeできる。

1. root cause特定済み。
2. 原因に対するcode/config fix済み。修正版・差分/hashを参照できる。
3. relevant fixture/regression tests PASS。
4. 対象のrequired evidence再検証PASS。
5. STOP対象scope以外に影響なしと確認でき、再開対象に適用される他の未解除HARD/host/global STOPがない。
6. stop ID、cause/fix/test/evidence参照、実再開時刻、対象、予算を含むresume eventを記録する。

再開許可eventを処理開始前に保存し、再開後の結果も記録する。どれか未達なら停止を維持する。
再開に無意味な人間の「continue」操作を必須にしない。
未完attempt・既存artifactを上書きせず、別attempt/versionとして連結し、valid Rawは再利用する。
条件を満たした対象だけ再開し、retry budgetやFrozen Forecastを初期化しない。

旧`STOP_FOR_REVIEW_NO_AUTOMATIC_RETRY`は、未分類STOPの検知codeとして履歴に残す。
そのcodeや`STOP.json`の存在だけで永久停止しない。旧reason/evidenceを分類し、scope・解除条件を付与する。
分類根拠が不足する旧STOPは安全な解除を判断できないためHARD_STOP相当として確認を待つ。
現行adapterは依然存在チェックで停止するため、実装移行前にSTOPを削除してv2 resumeを模倣してはならない。

## 7. Official structure change / URL discovery / MISSING_ONLY

JPXの小さな構造変更は、official source確認済み、semantics不変、parser修正が局所的、
fixture/regression test追加・PASS、existing parsed semantics維持をすべて満たす場合に自動修正可能。
v2実装ではこの条件と6節のresume条件を満たして再開する。意味論が変わる可能性があればHARD_STOP。
本Policy定義タスクではparser・コード・testsを変更しない。

URLはofficial HTMLに明示されたURL、official inline script、official JSON index、official structured listing、
official link graphからのみ導出する。source本文/hash、明示hrefまたは導出規則、対象sessionとの対応を残す。
official scriptが明示したtemplateへの置換は、公式indexに実在する値だけを使う場合に許可する。
filename patternの推測、日付を入れたURL brute force、undocumented path guessingは禁止。
保存用のローカルfilenameを作ることは、取得URLの導出根拠にならない。

同じ公式情報の経路は、(1) structured official JSON、(2) explicit official HTML link、
(3) other explicit official sourceの順を基本とする。
優先経路の未公開なら他の明示された公式経路を確認できるが、HARD/host STOP後の代替取得は禁止。
複数pathの値が異なる場合、優先順位だけで片方を正しいと宣言せず8節でrevisionを確認する。

**MISSING_ONLY**を維持する。valid session、validated Raw PDF、hash validated Parsed、valid Manifestが
既にあれば再取得しない。artifact間の対応を検証し、足りないローカルmetadata/処理だけ補完する。
`RAW_VALIDATED_METADATA_PENDING`は既存PDFのprovenance確認・正規化を優先し、再downloadしない。
存在するだけでvalidと扱わず、hash競合はSTOP、未確認metadataは未確認のまま保持する。
既存有効データへの通常取得要求は0。confirmed revisionの処理は明示的なrevision処理として区別する。

## 8. Official revision

同一sessionの新official dataが既存dataと異なる場合、公式訂正告知・公式version/lineage等の根拠で
**confirmed official revision**と確認できた場合だけ、検証済み最新officialをactiveにする。
単に同じofficial URLのbytes/hashが後から変わったことはrevision確認にならない。

- old Raw、old Parsed/Manifest version、old outcome/scoreを保持する。古いRawを削除しない。
- old/new hash、official訂正根拠、取得時刻、active version、変更理由と対象session/tickerのlineageを記録する。
- affected derived resultのみ、新versionとして再計算する。関係のない結果を再計算しない。
- Frozen Forecast・当時のinput/baseline/threshold snapshot・評価対象Forecast IDは変更しない。
- outcome/scoreの新版は旧版を参照し、旧結果を消したり、有利な新版だけを選択したりしない。

revision reason unknownなら当該sessionをHARD_STOPし、両方のRawと矛盾証拠を保持する。
意味論・source identityまで不明、または共有hash系統への波及があればglobal STOP。
revision/conflictのRawは保護対象とし、従来の保存日数ルールで自動削除・trash移動しない。

## 9. Forecast timing / market feature horizon / Frozen Forecast

exact 08:45取得はrequiredではない。08:45は計画時刻として区別し、遅延実行を許可する。
`forecast_generated_at`に実際の生成時刻をtimezone付きで保存し、取得・生成・freeze時刻を区別する。
backdateとfake 08:45は禁止。旧schemaの`created_at` / `freeze_time`との対応は移行時に明示し、
旧Frozen recordを書き換えず、新版schema/recordで扱う。

Daily Forecastのmarket-price featureは、**latest fully completed prior trading session**までを使用する。
対象Forecastのtrading sessionより前で、生成時点にも完了・利用可能と検証できる最新sessionを使う。
遅延実行でも同日intraday price・同日CloseをこのDaily Forecastへ混入しない。同日intraday priceはProject 14。
古いsessionへの代替・fill-forwardで「latest」と偽装せず、必要な履歴/featureが不足するtickerはSKIPする。
必ず`feature_time <= forecast_generated_at`。さらに実際のevidence availability/receiptも生成時点以前と証明する。
今日取得した過去日付の値やprovider timestampだけで、過去のPIT availabilityを証明したことにしない。
optional情報も利用するなら実生成時点までに利用可能なものだけとし、不明はUNKNOWN/欠損として扱う。

遅延実行の許可は、実行済みの値を08:45 Forecastへ昇格させる許可ではない。
Formal/Trial gate、target return、採点集合・評価窓は別の既存契約として維持する。
特に開始済み評価窓のOpen→Closeを、事後生成Forecastの事前予測として扱わない。
遅延recordの適格性・表示は実装移行で明示し、既存late-researchの非採点recordを遡及昇格しない。

既存**Frozen Forecastは絶対に変更禁止**。
correction / alternativeはnew ID・new version・旧ID参照とし、old Forecastを保持する。
再開・official revision・採点結果を理由に、旧確率・factor・input・baseline・threshold・評価選択を変更しない。

## 10. Compact logging

|state|通常runのログ|
|---|---|
|PASS|aggregateとsmall machine-readable record。対象数・検証結果・証拠参照を中心にする。|
|WARN|one line: code、target、action。例: `missing_optional / ticker / continue`。|
|SKIP|one line: code、target、action。欠損tickerの件数を集計に残す。|
|RETRY|最終結果・retry count・target・attempt証拠参照。各attemptの本文を通常ログへ展開しない。|
|SOFT_STOP / HARD_STOP|reason、target、scope、last successful state、evidence hash/reference、resume conditionを詳細記録する。|
|RESUMED|旧stop ID、解除根拠・test/evidence参照、実再開時刻と結果への参照をsmall recordで残す。|

compactnessは証拠を捨てる意味ではない。Raw/response/STOP/fix/testの証拠は別の参照可能な履歴に保持する。
通常runはcurrent unresolved STOP summaryと必要な該当記録だけを読む。
過去STOPログ全件、analysis全体、Raw履歴をLLMへ一括読込しない。

## 11. 既存ルールとの差とmigration implication

以下の旧ルールは現在のdisk上に残す。v2仕様との矛盾は認識済みで、実装完了を意味しない。

|old rule / 所在|new rule|reason|migration implication|
|---|---|---|---|
|`historical_direct.acquire_one`: `STOP.json`存在で`STOP_FOR_REVIEW_NO_AUTOMATIC_RETRY`。全例外から共通STOPを作る。|cause/scope/recoverabilityで分類。条件を満たすSOFT_STOPは旧fileを保持してRESUMED。|修正済みの局所failureを永久・全session停止にしない。|旧reason/evidenceから分類する互換層とappend-only lifecycleが必要。未知STOPは解除しない。|
|同adapterのsession folderはcreate-onlyで、途中attemptのfolderがあると再実行不能。|旧attemptを保存し別attempt/versionで再開、有効Rawを再利用。|証拠保持と復旧を両立する。|attempt識別・lineage・重複防止の設計が必要。既存folderを消さない。|
|`historical_direct._policy`と`jpx_daily_acquisition_config.json#historical_direct.automatic_retry=false`。429は`RATE_LIMIT`で即STOP。|許可されたtransientのみretry最大2、継続429はhost/global STOP。|一過性と制限継続を分離する。|adapter/configの整合、予算継承、待機・host停止をまとめて実装。現行testは旧挙動のまま。|
|`jpx_computer_use_policy.json`: retry最大1、RATE_LIMIT/CORE6_MISSING/PARSER_FAILURE等を停止codeへ分類。|v2 Historical Catch-upでは最大2、codeの根拠とscopeでWARN/SKIP/SOFT/HARDを区別。|同じcodeでも原因・影響が異なる。|通常browserの実行許可やone-shot制約を勝手に拡張せず、経路別予算・分類の互換性を定義する。|
|`historical_direct.validate_pdf/listing_link`はContent-Typeを厳密一致で拒否。JSONは`.json`の`binary/octet-stream`を既に許容。|formal MIME差だけなら全内容検証後WARN。|形式差と安全性・semanticsを分離する。|既存JSON例外を保持しWARN記録を追加。PDF/HTMLの内容検証を弱めない。|
|`jpx_daily_acquisition_config.json#validation`・Parsed/Manifest経路はCore6全6件/missing=0を要求。|個別ticker通常欠損はSKIP、他ticker継続。全件valid宣言は不可。|局所欠損で全体を止めない。|partial保存・validator・consumer・集計を整合させる。既存all-6 schemaを偽装しない。|
|provider READMEはparser失敗・途中停止後の自動retry禁止、人間確認までSTOP保持。configは`parser_changes_allowed=false`。|既知の局所形式差は限定修正・fixture/regression PASS後自動resume可能。|原因修正後の無意味なcontinue操作を省く。|元のsecurity/semantic停止は維持。将来の局所修正・test追加は別Implementation Taskで行う。|
|`docs/OPERATING_POLICY.md` v1 Draftはretrieved_at/HMAC欠損をWARN/互換変換可と記す。|required provenance/PIT/hash/authenticationは省略不可。optionalのみWARN。|時刻偽装・署名省略で証拠を弱めない。|経路別required契約を明示。`test_fresh_bridge_evidence.py`の署名/時刻/固定hash保護を維持する。|
|`analysis_config.md`のJapan freeze/cutoffは08:45固定。`minimal_forward_trial_config.json`のfreeze_time・late-research cutoffも08:45固定。|08:45は計画時刻、遅延生成可。実生成時刻とprior completed session/PITを必須にする。|実時刻を保存し、同日intraday混入・backdateを禁止する。|新しい時刻/適格性契約とschema対応が必要。旧trial・late-research・Frozen recordを遡及変換しない。US時刻ルールは今回変更しない。|
|`raw_data_retention_policy.json`は30日等の条件付きtrash設計、cleanupは無効。|official revision/conflictのold Raw/outcome/score/lineageは保護・保持。|訂正の証拠と旧評価を失わない。|revision evidenceを保護classへ対応させる。cleanup有効化・Raw移動・削除は本タスクでは行わない。|
|`00_START_HERE.md`/provider READMEはhistorical対象を2026-10-05〜07と記すが、現config allowlistには10-08もある。|取得対象はdisk上の現configを確認し、v2は日付allowlistを拡張しない。|記述差を運用権限の拡大に使わない。|許可sessionのdoc整合は別の限定移行で扱う。本書は新しいsession取得を開始しない。|

### Compatibility mapping

|既存名称|v2での意味|
|---|---|
|`VALIDATED_PENDING_EXPLICIT_REGISTRATION`|acquisition stageのPASS。登録/Forecast適格性PASSとは別。|
|`SKIP_EXISTING_LOCAL`|`SKIP / EXISTING_VALID_LOCAL`。required evidenceを確認しweb requests=0。|
|`NOT_YET_AVAILABLE` / `PENDING_PUBLICATION`|`SKIP` + publication code。未公開をsecurity STOPにしない。|
|`CORE6_MISSING` / `PENDING_DATA`|通常欠損は`SKIP / SKIPPED_MISSING_INPUT`。source/semantic failureならHARD_STOP。|
|`RATE_LIMIT`|initial 429は予算内RETRY、継続は`HARD_STOP / HOST_PERSISTENT_429`。|
|`PAGE_CHANGED` / `PARSER_FAILURE` / `SCHEMA_FAIL`|既知・局所・semantics維持ならSOFT_STOP。意味論不明ならHARD_STOP。|
|`REVIEW_REQUIRED` / `STOP_FOR_THE_DAY` / `STOP_FOR_REVIEW_NO_AUTOMATIC_RETRY`|旧reason/evidenceからSOFT/HARDとscopeを分類。名称だけで自動解除しない。|

stateと登録・採点適格性は別軸。既存codeと旧eventは保持し、mappingは新Policyの分類として付加する。

## 12. 次のImplementation Taskの受入条件

実装移行では、403/challenge/非公式redirectへの追加要求0、transient最大2・persistent 429のhost遮断、
内容検証付きMIME WARN、個別ticker欠損とsession isolation、SOFT resumeの全条件と旧STOP保持、
unknown legacy STOPの保留、attempt予算継承、revision lineageとFrozen不変、実生成時刻/PITの保護を検証する。
既存security・provenance・OHLC・hashのregressionを維持し、旧retry/永久STOPを表すtestは明示的に移行する。
Policy定義の今回commitには実装・tests・Raw/data/state/log変更を含めず、pushもしない。
