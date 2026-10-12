# Model selection research v1 — 2026-10-13

Exa Search / fetchでprimary researchと公式実装資料を少数確認した。
研究記事をForecast入力にしない。指定済み3モデルの範囲は変更しない。

| 確認元 | 今回への適用 |
|---|---|
| [Gneiting & Ranjan, Combining Predictive Distributions](https://arxiv.org/abs/1106.1638) | linear poolによる確率分布の結合とcalibration/dispersionの検討。等重みは今回の指定policyとして固定。平均しただけでcalibrationが保証されるとは扱わない。 |
| [Gneiting & Raftery, Strictly Proper Scoring Rules](https://sites.stat.washington.edu/raftery/Research/PDF/Gneiting2007jasa.pdf) | categorical quadratic/logarithmic scoringの根拠。既存repoのmulticlass Brierはclass方向にsum、sample方向にmean。3で割らない。 |
| [sklearn Common pitfalls](https://scikit-learn.org/stable/common_pitfalls.html) | preprocessingやfeature selectionをtrainのみでfit。full-history scaler、validationを含むfeature選択を禁止。 |
| [sklearn TimeSeriesSplit](https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html) | expanding time splitの原則。ここでは行単位の標準splitを直接使わず、sessionごとにCore6をまとめる。 |
| [sklearn LogisticRegression](https://scikit-learn.org/stable/modules/generated/sklearn.linear_model.LogisticRegression.html) | lbfgsのmultinomial softmaxとL2。実行は固定sklearn 1.8.0、L2はl1_ratio=0として指定しdeprecated penalty引数を避ける。 |
| [LightGBM Parameters](https://lightgbm.readthedocs.io/en/stable/Parameters.html), [Parameters tuning](https://lightgbm.readthedocs.io/en/stable/Parameters-Tuning.html) | multiclass、seed/deterministic/single thread、leaf-wise overfittingをdepth/leaves/min_dataで制限。24候補に固定、100 rounds、full validationを用いたearly stoppingなし。 |
| [Bailey et al., The Probability of Backtest Overfitting](https://davidhbailey.com/dhbpapers/backtest-prob.pdf) | 多数のcandidateを試して最良結果だけを報告する金融backtestの選択バイアス。今回の選択foldの数値はmodel-selection診断で、未使用holdout性能やforward実績とは呼ばない。 |

実データ履歴のPIT-safe性は研究資料から推定できない。個々のavailable_at、
source hash、feature_end、Outcome availabilityをlocal contractで検証する。
固定Thresholdをそのtraining_end以前のhistorical validationへ持ち込まない。
追加market/news features、ticker intercept、calibration modelは今版では未採用。

Jevのローカル`--help`、`score --help`、`choice --help`、`ask --help`、
`presets validate`でCLI contractを確認。stdoutのtyped answer shapeは
[go-jevの実装](https://raw.githubusercontent.com/mattn/go-jev/main/jev.go)も参照した。
これは現在のローカルCLIのlive互換性や認証成功を証明するものではなく、
本TaskではJev APIを呼んでいない。ルールはnews triageのみ、数値filterはlocal code。
