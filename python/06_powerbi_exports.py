"""
BharatInsight — Phase 4 bridge: Phase 3 results -> flat CSVs for Power BI.

Blueprint Phase 4 requires three supplementary tables imported into the .pbix
as CSV-backed tables. This script recomputes them with EXACTLY the same logic
used in the notebooks (no new methodology — the notebooks stay the source of
truth) and writes:

    powerbi/data/rfm_segments.csv     — 1 row/user: R, F, M, scores, rule
                                        segment (NB3) + KMeans cluster (NB3)
    powerbi/data/basket_rules.csv     — cuisine-pair association rules ranked
                                        by lift (NB2)
    powerbi/data/forecast_weekly.csv  — weekly INR revenue history + the model
                                        fit / holdout forecast with CI band (NB5)
    powerbi/data/forecast_meta.txt    — engine used + holdout MAE/MAPE (NB5)
    powerbi/data/festivals.csv        — Diwali/Holi dates used as callouts (NB5)

Run from anywhere:
    python python/06_powerbi_exports.py

Uses DATABASE_URL from .env (repo root) or the environment — same as the notebooks.
Takes ~4-8 min on the real dataset (the KMeans silhouette sweep is the slow part).
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# ---------------------------------------------------------------- boilerplate
ROOT = Path(__file__).resolve().parents[1]  # repo root (python/ lives one level down)
OUT = ROOT / "powerbi" / "data"
OUT.mkdir(parents=True, exist_ok=True)
load_dotenv(ROOT / ".env")

DB_URL = os.getenv("DATABASE_URL")
if not DB_URL:
    sys.exit("DATABASE_URL missing — set it in .env at the repo root (same as Phase 3).")
engine = create_engine(DB_URL)


def q(sql: str) -> pd.DataFrame:
    """read_sql via text(): keeps parameters=None so literal '%' in ILIKE is safe."""
    with engine.connect() as conn:
        return pd.read_sql(text(sql), conn)


print(f"ROOT: {ROOT}\nOUT:  {OUT}\nDB connected OK", flush=True)
t0 = time.time()

# ====================================================== 1. RFM segments (NB3)
print("\n[1/3] RFM segmentation (same SQL + scoring as 03_RFM_Segmentation) ...", flush=True)
rfm = q("""
WITH ref AS (SELECT MAX(order_datetime)::date + 1 AS ref_date FROM orders)
SELECT o.user_id,
       (SELECT ref_date FROM ref) - MAX(o.order_datetime)::date AS recency_days,
       COUNT(*) AS frequency,
       SUM(o.sales_amount) FILTER (WHERE o.currency='INR') AS monetary_inr
FROM orders o WHERE o.user_id IS NOT NULL GROUP BY o.user_id
""")
rfm["monetary_inr"] = rfm["monetary_inr"].fillna(0)

def score(s, labels):
    return pd.qcut(s.rank(method="first"), 4, labels=labels).astype(int)

rfm["r_score"] = score(rfm["recency_days"], [4, 3, 2, 1])   # recent = high score
rfm["f_score"] = score(rfm["frequency"], [1, 2, 3, 4])
rfm["m_score"] = score(rfm["monetary_inr"], [1, 2, 3, 4])
tot = rfm["r_score"] + rfm["f_score"] + rfm["m_score"]
rfm["segment"] = np.select(
    [tot >= 11, tot >= 9, tot >= 7, tot >= 5],
    ["Champions", "Loyal", "Potential Loyalist", "At Risk"], default="Lost")

# KMeans lens — identical sweep to notebook 03 (k=2..8, silhouette picks k)
from sklearn.preprocessing import StandardScaler
from sklearn.cluster import KMeans
from sklearn.metrics import silhouette_score

X = StandardScaler().fit_transform(np.log1p(rfm[["recency_days", "frequency", "monetary_inr"]]))
scores = {}
for k in range(2, 9):
    lab = KMeans(n_clusters=k, n_init=10, random_state=42).fit_predict(X)
    scores[k] = silhouette_score(X, lab, sample_size=min(10000, len(X)), random_state=42)
    print(f"      k={k}  silhouette={scores[k]:.4f}", flush=True)
best_k = max(scores, key=scores.get)
print(f"      Chosen k = {best_k} (highest silhouette = {scores[best_k]:.4f})", flush=True)

rfm["cluster"] = KMeans(n_clusters=best_k, n_init=10, random_state=42).fit_predict(X)

rfm_out = rfm[["user_id", "recency_days", "frequency", "monetary_inr",
               "r_score", "f_score", "m_score", "segment", "cluster"]].copy()
rfm_out["monetary_inr"] = rfm_out["monetary_inr"].round(2)
rfm_path = OUT / "rfm_segments.csv"
rfm_out.to_csv(rfm_path, index=False)
print(f"      -> {rfm_path.name}: {len(rfm_out):,} rows | segments: "
      f"{rfm_out['segment'].value_counts().to_dict()}", flush=True)

# ================================================ 2. Basket rules (NB2)
print("\n[2/3] Market-basket rules (same matrix + apriori as 02_Market_Basket) ...", flush=True)
baskets = q("""
SELECT o.user_id, trim(tag) AS cuisine
FROM orders o
JOIN restaurant r ON r.restaurant_id = o.restaurant_id,
     LATERAL unnest(string_to_array(r.cuisine, ',')) AS tag
WHERE o.user_id IS NOT NULL AND r.cuisine IS NOT NULL
GROUP BY o.user_id, trim(tag)
""")
basket = pd.crosstab(baskets["user_id"], baskets["cuisine"]).astype(bool)
print(f"      basket matrix: {basket.shape[0]:,} users x {basket.shape[1]} cuisines", flush=True)

from mlxtend.frequent_patterns import apriori, association_rules

MIN_SUPPORT = 0.01   # 1% of ~100k users ~= 1000 baskets — scale-appropriate (NB2)
freq = apriori(basket, min_support=MIN_SUPPORT, use_colnames=True)
rules = association_rules(freq, metric="lift", min_threshold=1.0)
rules_out = (rules[["antecedents", "consequents", "support", "confidence", "lift"]]
             .sort_values("lift", ascending=False).head(50).copy())
for c in ["antecedents", "consequents"]:
    rules_out[c] = rules_out[c].apply(lambda s: " + ".join(sorted(s)))
rules_out[["support", "confidence", "lift"]] = rules_out[["support", "confidence", "lift"]].round(4)
rules_out.insert(0, "rule", rules_out["antecedents"] + "  ->  " + rules_out["consequents"])
rules_path = OUT / "basket_rules.csv"
rules_out.to_csv(rules_path, index=False)
print(f"      -> {rules_path.name}: {len(rules_out)} rules (lift>=1, top 50) "
      f"| best lift {rules_out['lift'].max():.2f}", flush=True)

# ================================================ 3. Forecast table (NB5)
print("\n[3/3] Weekly forecast table (same holdout + engines as 05_Sales_Forecasting) ...", flush=True)
ts = q("""
SELECT date_trunc('week', order_datetime)::date AS ds, SUM(sales_amount)::float AS y
FROM orders WHERE currency='INR' GROUP BY 1 ORDER BY 1
""")
ts["ds"] = pd.to_datetime(ts["ds"])

FEST = {
    "diwali": ["2017-10-19", "2018-11-07", "2019-10-27", "2020-11-14"],
    "holi":   ["2017-03-13", "2018-03-02", "2019-03-21", "2020-03-10"],
}
hol = pd.concat([pd.DataFrame({"holiday": k, "ds": pd.to_datetime(v),
                               "lower_window": -1, "upper_window": 1})
                 for k, v in FEST.items()], ignore_index=True)
HOLD = 12   # last 12 weeks = honest holdout (NB5)
train, test = ts.iloc[:-HOLD], ts.iloc[-HOLD:]

engine_used, fc = None, None
try:
    from prophet import Prophet
    m = Prophet(holidays=hol, yearly_seasonality=True,
                weekly_seasonality=False, daily_seasonality=False)
    m.fit(train.rename(columns={"ds": "ds", "y": "y"}))
    fut = m.make_future_dataframe(periods=HOLD, freq="W")
    fc = (m.predict(fut)[["ds", "yhat", "yhat_lower", "yhat_upper"]]
            .rename(columns={"yhat": "forecast", "yhat_lower": "lower", "yhat_upper": "upper"}))
    engine_used = "Prophet + Diwali/Holi holiday regressor"
except Exception as e:
    from statsmodels.tsa.holtwinters import ExponentialSmoothing
    s = train.set_index("ds")["y"]
    s.index = pd.DatetimeIndex(s.index)
    s = s.asfreq(pd.infer_freq(s.index) or "W-MON")   # keep week-anchor (Mon), not Sunday default
    m2 = ExponentialSmoothing(s, trend="add", seasonal="add", seasonal_periods=52).fit()
    vals = m2.forecast(HOLD)
    fc = pd.DataFrame({"ds": vals.index, "forecast": vals.values,
                       "lower": vals.values * 0.9, "upper": vals.values * 1.1})
    train_fit = pd.DataFrame({"ds": s.index, "forecast": m2.fittedvalues.values,
                              "lower": m2.fittedvalues.values, "upper": m2.fittedvalues.values})
    fc = pd.concat([train_fit, fc], ignore_index=True)
    engine_used = f"statsmodels Holt-Winters fallback ({type(e).__name__}: Prophet not installed)"

pred = fc.tail(HOLD).set_index("ds")["forecast"]
mae = float(np.mean(np.abs(pred.values - test["y"].values)))
mape = float(np.mean(np.abs((test["y"].values - pred.values)
                            / np.clip(test["y"].values, 1, None))) * 100)
print(f"      ENGINE: {engine_used}", flush=True)
print(f"      HOLDOUT  MAE = {mae:,.0f} INR   MAPE = {mape:.1f}%  (on {len(test)} unseen weeks)",
      flush=True)

fc = fc.rename(columns={"ds": "week"})
hist = ts.rename(columns={"ds": "week", "y": "actual_revenue"})
forecast_tbl = hist.merge(fc, on="week", how="outer").sort_values("week")
forecast_tbl["is_holdout_forecast"] = forecast_tbl["week"] > train["ds"].max()
for c in ["actual_revenue", "forecast", "lower", "upper"]:
    forecast_tbl[c] = forecast_tbl[c].round(2)
forecast_path = OUT / "forecast_weekly.csv"
forecast_tbl.to_csv(forecast_path, index=False)
print(f"      -> {forecast_path.name}: {len(forecast_tbl)} weeks "
      f"({forecast_tbl['is_holdout_forecast'].sum()} holdout)", flush=True)

meta_path = OUT / "forecast_meta.txt"
meta_path.write_text(
    f"engine: {engine_used}\n"
    f"holdout_weeks: {HOLD}\n"
    f"MAE_INR: {mae:,.0f}\n"
    f"MAPE_pct: {mape:.1f}\n",
    encoding="utf-8")

fest_tbl = (hol[["holiday", "ds"]]
            .rename(columns={"holiday": "festival", "ds": "date"}))
in_range = fest_tbl[(fest_tbl["date"] >= ts["ds"].min())
                    & (fest_tbl["date"] <= ts["ds"].max() + pd.Timedelta(weeks=HOLD))]
fest_path = OUT / "festivals.csv"
in_range.to_csv(fest_path, index=False)
print(f"      -> {fest_path.name}: {len(in_range)} festival dates in range", flush=True)

print(f"\nDONE in {time.time() - t0:.0f}s — Power BI import tables ready in {OUT}", flush=True)
print("Next: follow powerbi/BUILD_GUIDE.md (connect .pbix to PostgreSQL + these CSVs).", flush=True)
