# Stock_Forecast_Codex

日次株式市場の研究・確率予測・検証を行うためのCodex向けリポジトリです。
投資助言ではなく、予測をFreezeし、Outcome確定後にBrier Score等で検証する研究用途を目的とします。

## Start here
通常Runは `00_START_HERE.md` から開始します。
`MASTER_PROMPT.md`、`analysis/`、全configを毎回一括読込せず、必要な情報だけ条件付きで参照します。

## Repository
- `engine/` — forecast / scoring / bridge / validation logic
- `providers/` — provider adapters and JPX parser
- `tests/` — offline tests and fixtures
- `config/` — versioned configuration and policies
- `docs/` — operating policy and architecture
- `research/` — shadow-only research work
- `historical_shocks/` — compact research definitions

詳細は `docs/ARCHITECTURE.md` と `docs/OPERATING_POLICY.md` を参照してください。

## Data policy
PDF、Raw market data、runtime state、大量log、credential/API keyはGitHubへcommitしません。
それらはローカルで管理し、GitHubにはコード・config・policy・tests・小さい再現用fixtureを保存します。

公開候補の境界と監査結果は [docs/PUBLIC_BASELINE_AUDIT.md](docs/PUBLIC_BASELINE_AUDIT.md)、
今回の候補一覧は [docs/PUBLIC_BASELINE_FILES.txt](docs/PUBLIC_BASELINE_FILES.txt) を参照してください。
`analysis/` は明示した設計文書・空のtemplateだけを候補にします。
市場履歴から計算したconfigや実市場値を埋め込んだテストは、公開候補から除外しています。

## Development rule
`main` は確認済みの安定コードを置き、開発は `dev/*`、研究は `research/*` を基本とします。
Forecast logic、Threshold、Baseline、Model、Policyの意味を変える変更は、理由とdiffが追跡できる形で行います。
