# Operating Policy v1 Draft

このファイルは、日次株価予測プロジェクトの運用原則をGitで管理するための基準です。
詳細な実装は段階的に行い、この文書だけで既存Engineの挙動を変更しません。

STOP / Retry / Resumeの確定Policyは [STOP / Retry / Resume v2](../config/operation_policy_v2.md) を参照します。
以下のv1 Draftは履歴として保持し、v2との差・移行条件は同文書のmigration noteに記録します。現行実装は未移行です。

## 基本原則
- データ取得・公式仕様への互換対応は、必要以上に停止させない。
- Forecast生成後の予測値は変更しない。修正版が必要なら別Forecast IDを使う。
- Outcome・Scoreの公式訂正は最新版を採用し、旧版と旧Scoreも履歴として残す。
- 遅延実行は許可するが、実際の実行時刻を記録し、予定時刻へ遡及させない。
- URLを過去の命名規則から推測して取得しない。公式HTML/JS/JSON等から辿れる経路のみ使う。

## 取得失敗
- 一時的通信失敗・429は最大2回まで再試行できる。
- 403、CAPTCHA、security challengeは自動回避せず停止する。
- Session固有の欠損は、そのSessionまたは影響対象だけをskipし、影響しないSession/銘柄は続行できる。
- hash競合、OHLC不整合、source/session/ticker不一致は停止して確認する。
- 軽微な公式形式変更は、最小修正・関連テストPASS後に自動resumeしてよい。

## Forecast
- 予測に使用する情報は、実際のForecast生成時点までに利用可能な情報に限る。
- 1銘柄だけ必須入力が欠損した場合、その銘柄だけskipし、他銘柄は続行できる。補間は禁止。
- News、Macro、Cross-asset等のOptional入力欠損はWARNとして継続できる。
- retrieved_at欠損、HMAC欠損、旧receipt schemaは、必要値を正しく解釈できる場合はWARN/互換変換で継続できる。

## Logging
- PASSは集計中心。
- WARN / SKIP / RETRYは短いcodeと結果だけを保存する。
- STOPのみ詳細な証跡を残す。
- 通常Runで過去ログ全文をLLMへ読み込ませない。

## GitHub
- mainは確認済みの安定コードを置く。
- 開発はdev/*、研究はresearch/*を基本とする。
- Policy変更と実装変更は可能な限り別commitにする。
- PDF、Raw market data、大量log、credential/API keyはGitHubへcommitしない。
- Forecast logic、Threshold、Baseline、Model、Policyの意味変更は変更提案とdiffを明示する。
- 重要な安定点はGit tagで残す。
