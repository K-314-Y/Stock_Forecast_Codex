# Legacy STOPの明示的manual review

Policy: [Reviewed legacy HARD STOP release](../config/operation_policy_v2.md)。
`legacy_stop_review.manual_review_release(root, ...)`だけが明示的APPROVEDを受け付ける。
acquisition、起動、gateは解除を作成しない。networkも実行しない。
`HARD_STOP_LEGACY_UNCLASSIFIED`かつ旧reasonがexactに`LISTING_EMPTY_OR_AMBIGUOUS`の、
旧HTML-only discovery recordだけが対象。current v2 STOPやsecurity/integrity STOPは対象外。

呼出しには`expected_stop_sha256`、`review_decision='APPROVED'`、`root_cause`、`resolution`、
`review_evidence`、`discovery_inputs`、`fix_refs`、`validation_refs`を明示する。
人間のidentityや電子署名は記録しない。明示的なmanual function実行を
`review_action=EXPLICIT_MANUAL_FUNCTION`として記録する。hash sidecarは改変検知であり電子署名ではない。

review evidenceはsealedな`jpx-json-discovery-v1`、`HREF_DISCOVERED`の保存済みJSON。
discovery inputsは`listing`、`monthly_list`、`monthly`のbody reference `{path, sha256}`。
official redirect chain、body hash、session、空HTML tableをofflineで再検証し、既存discovery関数で
endpoint、published month、exact Stocks hrefを再導出する。旧chainの`sha256` field aliasは
メモリ内でだけ変換し、元fileとparser/discovery semanticsを変更しない。

fix refsは現在のdiscovery、STOP loader、本review実装、Policyを含む。
validation refsはcreate-only sealedな`legacy-stop-validation-v1`、status PASSのrecord。
旧STOP、review evidence、discovery inputs、fix refsをexactに参照し、`live_jpx_requests=0`、
各discovery check PASSとlegacy release/STOP/crash/challenge/historical/JSON/partial/full offline/public-tree
の実行結果（status、tests_run、failures、errors）を持つ。実際に実行した結果だけを保存する。

receiptは`data/jpx_historical_direct/legacy_reviews_v1/reviewed-release-<STOP SHA>.json`と
hash sidecarへcreate-onlyで保存する。`schema_version=legacy-hard-stop-reviewed-release-v1`、
`policy_version=STOP_RETRY_RESUME_V2_REVIEWED_LEGACY_V1`、`state=REVIEWED_RELEASE`、
`release_type=LEGACY_HARD_STOP_MANUAL_REVIEW`、旧STOP ref/hash/内容、original reason、session、scope/target、
root cause、resolution、review/fix/validation refs、discovery replay、APPROVED、実released_atを記録する。
既存store lockでreceipt保存・検証後にcurrent indexへ登録する。元STOPとactive履歴は保持する。

起動と各gateで参照証拠を再検証し、exact旧STOPだけgateから除外する。他STOPは有効のまま。
receipt破損/削除、sidecar欠損、index登録漏れ、取得component変化、validation不一致はfail closed。
crash後の孤立receiptも自動採用しない。証拠を削除して復旧しない。
初回release作成時は現在のfix bytesと検証reportを照合する。既存generationのfix refsは
歴史的commit/blobと照合し、実行時は最新receiptのcomponent fingerprintを確認する。

evidenceはGit対象外のdata/analysisに保存する。解除は取得許可・Retry予算・Forecast gateを変更せず、
取得再開やForecast生成を実行しない。


## Task 10C-OPT-1 component binding

`current_code_commit`と`base_code_commit`はaudit lineageとして保持する。
既存renewalの実行にrepository全体のHEAD一致やclean treeを要求しない。
新しい明示的renewalの作成時だけ、従来のclean/current commit検証を維持する。

全byteを固定する範囲はhistorical_direct、historical_policy_v2、legacy_stop_review、
parser、acquisitionと、jpx_daily_acquisition_config、normalized_market_schema、operation_policy_v2。
取得call graphからminimal_trialのCORE/ROOT、canonical/hash/read_frozen/raw resolver/parsed reader、
official_gapのcalendar定数、expected_gap_sessions/stage_existing_pdf、storageのlock/seal/fsyncを固定する。
選択した関数の全sourceに加え、module imports、関数header/decorator/default、module-level実行文を
固定し、annotated/conditional/global再代入も検出する。取得に到達しない関数bodyは除外する。
README、monitor、Shadow ML、report、独立した無関係engine moduleは含めない。

engine、providers、providers.jp、providers.jp.jpx_manualのpackage initializerは
存在/不存在と全byteを固定し、追加・変更・symlink化でgateを閉じる。
新receiptは`jpx-acquisition-components-v2`を持つ。既存generation 2–4では記録commitの
component snapshotを使用し、このrefactorの完全一致したbefore/after集合だけを
`config/jpx_acquisition_binding_v2.json`で許可する。v1 mappingと既存証拠は変更しない。
mapperとv2 mappingは指定baseを祖先に持つv2 mappingの最初の導入commitからbyte変更を
許さない。比較済みmapping bytesだけをstrict JSONで解析する。
任意のsource差分やmapping編集は許可しない。generation 1は古いchain内で証拠を検証し、
最新のunanchored legacy receiptを自動承認しない。最適化だけのgeneration 5は不要。

詳細と検証結果は[EFFICIENCY_HARDENING.md](EFFICIENCY_HARDENING.md)を参照。
