# Task 10C-1: parallel models v1

このTaskは実装・offline validationのみ。2026-10-13 Fresh取得、実Forecast/ID/Freeze、
Outcome取得、Forward scoringは実行しない。Task 10Bのscope・terms/persistence・STOP・
Retry/Resumeは変更しない。自動学習、自動昇格、scheduleは無効のまま。

モデルは `equal-linear-pool-v1` (PRIMARY)、`multinomial-logistic-v1` と
`lightgbm-multiclass-v1` (SHADOW) の3本に限定する。
Primaryは `minimal-baselines-v1` のBase Rate/Momentum/MA Trendを等重みで平均。
既存 `short-oc-p33-v1` のOutcome定義・Core6・9特徴量を再利用する。
Primaryの5pp丸めはfloor後、既存[5,95] boundsへ制限し、rawとの差が最大のclassへ
5ppずつ配分する。下限制限で100を超える場合は超過最大のclassから5ppを減らす。
完全tieはFLAT, UP, DOWN。raw確率と丸め確率の両方を保持し、比較はPrimaryの
実際の出力(丸め後)、Shadowはraw確率、Baselineはfrozen empirical確率を用いる。

## Dataと入力

既存data構造を移動しない。新しいderived artifactの論理配置は
`data/features/`, `data/news/`, `data/forecasts/`, `data/model_freezes/`。
数値のcandidate metadataはメモリ中でsource/timestamp/available_at/session/currency/
finite/duplicate/missing/PIT/anomalyを検証する。取得universeはconfig参照。
`acquired/validated/candidate_feature/model_input/model_versions_allowed`を区別する。
取得したRawを自動でmodel入力にしない。追加providerをproductionへ導入しない。
数値にはJevを呼ばない。Jevはnews本文をメモリとstdinでのみ扱い、同一eventまたは
headline hashを除重複する。KEEP/DROPともallowlist metadataのみ保存する。
ニュースの欠損/失敗を0件と偽装しない。news aggregateはcandidate、v1入力はprice9のみ。

共通snapshotはCore6×sessionの1個。feature_end、cutoff、created_at、各特徴のavailable_at、
source hashes、missing/PIT status、frozen baseline probabilitiesを含む。
allowed numeric derived features以外、本文・headline・provider response・raw priceを含めない。
execution snapshotはTask 10Bの同一process/one-use入力をconsumeして構築する。
保存snapshotの再生は実Forecast経路へ入れない。

## 学習・検証

LogisticはCore6 pooled L2 multinomial (`lbfgs`, C=1) とtrain-only StandardScaler。
MA_stateは固定 -1/0/1、ticker interceptと追加4特徴は未採用。
LightGBMもpooled multiclass、同じprice9だけを最初のfeature setにする。
広いmarket/sector/news universeはcandidateのまま保持する。feature selectionを
validation全体や全履歴へfitしない。今版にはデータ依存のfeature selectionがない。
LightGBMのgridはdepth/leaves制約を満たす24通り、100 boosting rounds、single thread、
seed=314。Brier優先で選び、tieはconfig順。Log Loss/Accuracy/Macro F1/
classwise calibration binsを記録。外側の性能推定とは呼ばずmodel-selection診断と明示する。

日付group expanding: 最低60 train sessions + 20 validation sessions × 3 folds。
利用可能期間の末尾60 sessionsを3分割、前の全sessionがtrain。
同日のCore6をtrain/validation間で分けない。Outcome利用可能時刻はvalidation開始cutoff
より厳密に前。最終学習はtargetより前かつmodel作成時点まで利用可能なOutcomeのみ。
特徴量の補完なし、required欠損は拒否。threshold training_endは各feature_end以下。
過去のavailable_atをpublication推測や現在の取得時刻から捏造しない。
既存履歴はoffline quality検査対象だが、historical PIT provenance不足の場合は
実モデルFreezeを拒否する。syntheticテスト成功は実データ性能の証明ではない。

## Freezeとrunner

モデルartifactとmetadataはhashを結び、training period、feature/threshold versions、
hyperparameters、dataset hash、walk-forward summary、created_at、code commit、
dependency versionsを記録する。create-only directory、内容hash検証、上書き禁止。
forecast bundleは共通snapshotと同一generated_at/freeze_time、1 PRIMARY + 2 SHADOW、
18 model×ticker records、および比較用frozen baseline 18 recordsを参照する。
relative_strengthはmissing_optional / RULE_NOT_FROZEN。
本Taskではlive runnerは明示的に無効。synthetic probeはtemporary directoryだけで検証。
future `run_forward` は既存scope gateとone-use入力を要求し、同じsnapshotから3モデルを
生成してtarget単位にatomic/create-only Freezeする。現在のconfigでは入力消費前に拒否。
実行activation flagはmodel specification hashから分離し、後日のactivationで学習済み
仕様hashを変えない。実行gateは生成・書込に適用し、immutable artifactのreadには適用しない。
scoring entryは追加しない。将来同じOutcomeを6系列へjoinできるcomparison rowsを用意する。
60 sessions経過でも自動昇格しない。昇格は別policy/review/approval。

## Offline commands

Python 3.14、`uv venv --python python3.14 .venv`、
`uv pip install --python .venv/bin/python -r requirements-parallel-models.txt`。
`PYTHONPATH=.:tests .venv/bin/python -m unittest tests.test_parallel_models tests.test_parallel_data tests.test_jev_news -v`
`PYTHONPATH=.:tests .venv/bin/python -m unittest discover -s tests -p 'test_*.py'`
実Forecast CLI、automatic acquisition、Outcome scoring CLIは提供しない。
