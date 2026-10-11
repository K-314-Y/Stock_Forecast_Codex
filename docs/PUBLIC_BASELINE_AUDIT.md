# Public baseline boundary audit

監査日: 2026-10-11。対象: `K-314-Y/Stock_Forecast_Codex` の現在のworking treeから選んだ公開候補。
過去のGit履歴・他branch/worktreeは公開候補に含めず、履歴を書き換えていない。
この文書はGit管理境界を定めるもので、Forecastや取得・STOP/Retry/Resumeの運用仕様を変更しない。

## 分類と対象

Git内部metadataを除く現存ファイル396件を分類した。開始時の392件と、この監査で追加した4件が対象。

|分類|件数|判断|
|---|---:|---|
|PUBLIC_OK|76|コード、選別したテスト、schema、policy、研究定義、docs、人工fixture、空template|
|LOCAL_ONLY|314|取得データ、runtime、ログ、cache、backup、export、移行用資料、不要な検証報告、ローカル監査証跡|
|REVIEW_REQUIRED|6|Provider実応答・市場由来値を含み、再配布条件確認または将来の分離が必要。今回は全て除外|

公開候補の全76パスは [PUBLIC_BASELINE_FILES.txt](PUBLIC_BASELINE_FILES.txt) に固定した。
現在存在するファイルのうち、Git追跡済みまたはignoreされていないものが、この一覧と一致することを確認する。
この一覧は今回の監査時点のもの。新規ファイルは由来・個人情報・秘密値を確認してから候補へ加える。
`.gitignore`は追跡済みファイルや強制追加を除外できないため、今後も候補一覧とindexを確認する。

## 除外したもの

`data/`、`logs/`、`state/`、`backups/`、`exports/`、PDF、Python cache、`config/runtime_status.json`はLOCAL_ONLY。
`analysis/data_source_samples_2026-09-12.json`、`analysis/provider_test_results/`、JPX一覧の取得保存物、
`analysis/automation/`、識別情報・calendarの取得検証結果と、その他の観測・検証報告もLOCAL_ONLY。
自作設計を含む古い実行報告でも、公開の必要性が低いものはローカルに保持する。

`analysis/`で許可するのは `.keep`、`minimal_forward_trial_design_2026-09-15.md` と
`japan_free_provider_comparison_template_v1_2_2026-09-13.json` のみ。
前者は自作の設計・人工検証の説明、後者は候補未登録かつ値が空のtemplateで、取得結果を含まない。
`historical_shocks/shock_library.json` と `config/causal_map.json` は空の研究定義で、第三者Rawを含まない。

REVIEW_REQUIREDは以下の6件。ファイルの内容は変更せず、Git対象から外した。

|ファイル|理由|
|---|---|
|`config/data_sources.md`|自作の方針説明とProvider clock応答sampleが混在|
|`config/trial_thresholds_candidate.json`|取得した市場履歴から計算した実測Threshold・volatility等|
|`config/trial_baselines_candidate.json`|同履歴から計算したBaseline確率・件数|
|`config/trial_baseline_state_tables_candidate.json`|同履歴から計算した状態別確率表|
|`providers/jp/jpx_manual/test_parser.py`|JPX実PDFから抽出したCore6のOHLC・出来高の期待値を埋め込み|
|`tests/test_historical_quality.py`|Yahoo由来Rawの実Close値をassertionに埋め込み|

Provider名、公開URL、schema、アルゴリズム、公開識別子を参照するコードは候補に含める。
取得済みの市場値をコピーしたfixtureは人工fixtureとして扱わない。
コードがローカルデータを読むこと自体と、そのデータを再配布することは別に判定する。
ローカルのRaw、PDF、runtime、既存テストは削除・書換えしていない。

## Privacy / Secret / Third-party data

公開候補76件の内容とパスを検査した。実個人情報0件、秘密値0件、取得Raw/sample0件。
OS username、現在のhostname、Linux home path、Windows user path、一般の絶対path、device情報、
メール、private IP、ローカルnetwork情報を検索した。
`LOCAL_LINUX`と`${PROJECT_ROOT}`は既存placeholderで、新たな個人情報置換は不要だった。

誤検知・許可した人工例:
- `tests/test_jpx_historical_direct.py` のURL userinfoは架空の拒否テスト用usernameで、個人メールではない。
- `tests/test_minimal_trial_engine.py` と `tests/test_one_session_dry_run.py` の `C:\old\...` は架空の旧path解決テスト。
- `hostname`、`browser_actor`等のschema/API名や共通actor enumはPC名・device IDではない。
- 小さい人工価格、合成系列、模擬HTTP/HTML応答は取得済みProvider sampleではない。
- credential、cookie、private key等への言及やテスト名は、秘密値の保存ではない。

Secret scanはOpenAI/GitHub/AWS key、Bearer値、JWT、OAuth/client secret、database URL、
Supabase service-role key、password、private key、Cookie/session tokenの値パターンと関連文脈を確認した。
秘密値は証跡にも記録しない。分類理由・hash・scan結果の詳細は
`analysis/public_baseline_audit/`へ保存し、同ディレクトリはGit対象外。

## 検証と次の境界

`git status`、`git diff --check`、Git候補一覧との一致、全除外ファイルのignore判定、
禁止ファイル0件、公開候補のPrivacy/Secret/第三者データ検査、開始時hashとの比較を行った。
Python、schema、config値、ローカルデータとruntimeに変更はなく、機能テスト・外部通信は実施していない。
既存のテスト結果と既知失敗は [BASELINE_STATUS.md](BASELINE_STATUS.md) に記録されている。
公開テストの一部は除外済みのローカルfixture・runtime・補助テストを必要とする。
今回の境界確定は、公開clone単独で全テストが実行できるという保証ではない。

Git authorはrepo-localで `K-314-Y` / `286165942+K-314-Y@users.noreply.github.com`。
既に適切だったため変更なし。global設定、branch、worktree、index、commit、remote、pushは変更していない。
公開候補への移行判断はYES。REVIEW_REQUIREDの6件は含めず、commit/pushは別の明示的な作業とする。
STOP/Retry/Resume、Candidate Discovery、企業間波及、モデル追加等の開発には進んでいない。
