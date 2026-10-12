# Task 10C-2: historical training readiness

実Forecast、Fresh取得、Forecast ID/Freeze、Forward Outcome scoringを実行しない。
Primary、Core6、価格9特徴量、探索空間、Jev、Task 10Bのscope/persistenceを変更しない。

`engine.parallel_training_local` は既存Cacheとretention manifestだけを読み、元ファイルを
変更せず候補inventoryを作る。JPXを再取得・再parseしない。source identity、hash、品質、
価格basis、corporate action、availabilityを独立に確認する。取得済みと学習適格を区別する。

`engine.parallel_training` はtarget_session×tickerの1対1joinを検査し、先行61セッション
から既存8価格特徴量を再現する。9番目のthreshold入力も、各targetのprevious session以前
の履歴だけから同じアルゴリズムでreplayする。label用のfold/final Thresholdと入力を分離する。
UNKNOWN、cutoff以降の入力、未解決basis/action、missingは除外し補完しない。
同一日の銘柄を別foldにしない。Partial tickerはその銘柄だけを除外し、日付groupは維持する。

OBSERVEDはcontentに紐づく信頼できる取得/公開時刻。現在vintageの実取得時刻は、その時刻
以降のavailability上限としてのみ使用でき、元の歴史的公開時刻を証明しない。
SOURCE_SCHEDULE_DERIVEDにはsource、適用期間、保証された保守的上限、証拠hash、
rule versionが必要。mtime、Git時刻、pathは根拠にしない。現時点のschedule rule registryは空。
JPXのexpected publication dateだけから時刻を作らない。

CV_THRESHOLD_REPLAYは既存short-oc-p33-v1/quality policyと同一の純粋計算を再利用する。
train_end以前、かつvalidation開始前に利用可能だった入力のみ。既存1,000有効行の要件、
隔離/rolling-window規則を維持する。これはモデルの最低60 train datesとは別のthreshold
warmup要件であり、未来のpromotion用60/120 forward sessionsとも別である。
検証専用でproduction_candidate=false。候補登録・production Threshold書換えは行わない。
各foldのtrain/validation target classには同じfold-local値を使う。
全9入力は各target以前、label Threshold fitは最初のvalidation cutoff以前。
過去のthreshold入力をfold末の値や最終production値へ書き換えない。

CV selection後だけFINAL_MODEL_LABEL_THRESHOLDとして既存のfrozen production thresholdを
最終fitのlabelに使用する。入力はpre-T replayのまま。最終trainingはapproved cutoff以前、
target session以前に限定する。
Primaryのfrozen baseline/thresholdがそのvalidation時点で未成立なら、同窓のPrimary評価は
UNAVAILABLEとして明示し、未来のfrozen tablesを過去へ適用しない。

dataset artifactは `data/training/parallel_models_v1/<dataset sha256>/` のcreate-only directory。
共通eligible rows、excluded metadata、source hashes、PIT audit、fold定義、CV replay metadataを
格納する。Raw本文/responseを複製しない。real model Freezeはdataset/join/2-fold/両MLの
validation PASS、BLOCKER/HIGHなしの場合だけ。synthetic testは実データ性能を示さない。

STOP時にも除外metadataとaudit artifactを保存できるが、実学習/Freezeは開始しない。
historical availabilityとOutcome source/basisが証明できない現行inventoryを適格化しない。
