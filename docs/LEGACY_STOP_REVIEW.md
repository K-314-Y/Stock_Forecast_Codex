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
receipt破損/削除、sidecar欠損、index登録漏れ、fix hash変化、validation不一致はfail closed。
crash後の孤立receiptも自動採用しない。証拠を削除して復旧しない。
将来fixが変わる場合の新しいreview/revision手順は、この初版では提供しない。

evidenceはGit対象外のdata/analysisに保存する。解除は取得許可・Retry予算・Forecast gateを変更せず、
取得再開やForecast生成を実行しない。
