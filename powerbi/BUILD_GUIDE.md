# BharatInsight — Phase 4: Power BI Build Guide

> Yeh guide blueprint ka "detailed build spec" hai: **5 pages, 10 DAX measures, PostgreSQL connection.**
> Har step exactly bataya gaya hai — koi design decision tumhe khud nahi lena.
> Deliverables: `powerbi/bharatinsight_dashboard.pbix` + `powerbi/screenshots/` (5 PNG, ek per page).

---

## 0. Prerequisites (ek baar)

| # | Cheez | Kaise |
|---|-------|-------|
| 1 | **Power BI Desktop** (free) | Microsoft Store me "Power BI Desktop" search → Install. (YA https://aka.ms/pbidesktopstore) |
| 2 | PostgreSQL chal raha ho | Wahi Phase 0 wala `bharatinsight_db`, local — service running honi chahiye |
| 3 | Bridge CSVs export ho chuki hon | `python python\06_powerbi_exports.py` run karke `powerbi\data\` me **5 files** hone chahiye (neeche §2 dekho) |
| 4 | Theme file | `powerbi\theme_bharatinsight.json` (is repo me hai) |

⚠️ **Npgsql error:** PostgreSQL connect karte waqt agar *"This connector requires one or more additional components"* aaye to **Npgsql 4.0.17** install karo: https://github.com/npgsql/npgsql/releases/tag/v4.0.17 → `Npgsql-4.0.17.msi`. Power BI Desktop restart karo. (Zyada tar nayi Power BI versions me ye nahi aata.)

---

## 1. Data lao — PostgreSQL (5 tables)

`Home → Get Data → PostgreSQL database`
- **Server:** `localhost`  ·  **Database:** `bharatinsight_db`
- **Data Connectivity mode: Import** ✅ (DirectQuery nahi — Import fast hai, 19 lakh rows easily handle hota hai)
- Database credentials: apna postgres user/password, encryption checkbox hata do (local DB)

**5 tables select karo:** `users`, `restaurant`, `menu`, `food`, `orders` → **Load**

> Note: `menu` 10.6 lakh rows hai — Import me 1-2 min lagega, normal hai.

## 2. Data lao — Phase 3 bridge CSVs (4 tables)

`Get Data → Text/CSV` se `powerbi\data\` ki ye files:
1. `rfm_segments.csv`
2. `basket_rules.csv`
3. `forecast_weekly.csv`
4. `festivals.csv`

( `forecast_meta.txt` import nahi karna — wo sirf padhne ke liye hai; uske numbers Page 5 ke subtitle me manually likhoge. )

## 3. Date table banao (time intelligence ke liye)

`Modeling → New table`:
```dax
Date = CALENDARAUTO()
```
Phir `Table tools → Mark as date table` → date column = `Date`. ✅

## 4. Relationships (Model view me set karo)

| From (many side) | To (one side) | Column | Cardinality | Cross-filter |
|---|---|---|---|---|
| `orders` | `users` | user_id | *:1 | Single |
| `orders` | `restaurant` | restaurant_id | *:1 | Single |
| `orders` | `Date` | order_date | *:1 | Single |
| `menu` | `restaurant` | restaurant_id | *:1 | Single |
| `menu` | `food` | food_id | *:1 | Single |
| `rfm_segments` | `users` | user_id | 1:1 | Single |
| *(standalone — koi relationship nahi)* | `basket_rules`, `forecast_weekly`, `festivals` | — | — | — |

> ⚠️ `orders`→`restaurant` relationship **inactive** ban sakti hai (1,617 orders me `restaurant_id` NULL hai — Phase 0 Finding). Isse **active rakho**; NULL wale rows kabhi restaurant se match nahi honge — **yehi Rule R3 hai**: overall KPIs me counted, restaurant-level visuals me auto-excluded. Isko "fix" mat karna, documented behavior hai.

## 5. Reporting Rules (Phase 2/3 se — sab measures me baked)

| Rule | Kya | Power BI me kahan |
|---|---|---|
| **R1** | Revenue hamesha `currency='INR'` (USD segment alag hai) | Har revenue measure me filter |
| **R2** | Reference date = `MAX(order_date)+1`, kabhi TODAY() nahi | Snapshot measures me `LatestDataDate` measure use |
| **R3** | NULL `restaurant_id` orders overall me raho, restaurant rollups me nahi | Relationships khud handle karte hain |

## 6. DAX Measures (10) — `orders` table me New measure se banao

```dax
Total Revenue (INR) = CALCULATE ( SUM ( orders[sales_amount] ), orders[currency] = "INR" )
```
```dax
Total Orders = COUNTROWS ( orders )
```
```dax
Avg Order Value = 
DIVIDE (
    [Total Revenue (INR)],
    CALCULATE ( COUNTROWS ( orders ), orders[currency] = "INR" )
)
```
```dax
Avg Restaurant Rating = AVERAGE ( restaurant[rating] )
```
```dax
Latest Data Date = MAX ( orders[order_date] ) + 1
```
```dax
YoY Revenue Growth % = 
VAR rev_prior = CALCULATE ( [Total Revenue (INR)], SAMEPERIODLASTYEAR ( 'Date'[Date] ) )
RETURN DIVIDE ( [Total Revenue (INR)] - rev_prior, rev_prior )
```
```dax
Rolling 30-Day Revenue = 
CALCULATE (
    [Total Revenue (INR)],
    DATESINPERIOD ( 'Date'[Date], MAX ( 'Date'[Date] ), -30, DAY )
)
```
```dax
Repeat Customer Rate % = 
VAR repeat_buyers =
    COUNTROWS (
        FILTER (
            VALUES ( orders[user_id] ),
            CALCULATE ( COUNTROWS ( orders ) ) > 1
        )
    )
RETURN DIVIDE ( repeat_buyers, COUNTROWS ( VALUES ( orders[user_id] ) ) )
```
```dax
Revenue Share by Segment = 
DIVIDE (
    [Total Revenue (INR)],
    CALCULATE ( [Total Revenue (INR)], ALL ( rfm_segments ) )
)
```
```dax
Top Cuisine by Revenue = 
VAR ranked =
    ADDCOLUMNS (
        VALUES ( restaurant[cuisine] ),
        "rev", [Total Revenue (INR)]
    )
RETURN
    CONCATENATEX ( TOPN ( 1, ranked, [rev], DESC ), restaurant[cuisine], ", " )
```

**Format settings:** Revenue/AOV → Currency ₹ (`"₹" #,##0`), % measures → Percentage 1 decimal, Rating → 0.0 format.

## 7. Theme lagao

`View → Themes → Browse for themes → powerbi\theme_bharatinsight.json`
(Zomato-red `#E23744` primary + navy `#2C3E50` palette, sab 5 pages pe consistent.)

**Har page ke top pe ek Text box:** Title + subtitle = "business question" (neeche har page ke saath diya hai).

## 8. Page-by-Page Layout

### 📄 Page 1 — Overview
> *Title: "Business Overview" — Subtitle: "Kitna revenue, kitne orders, kahan se?"*

| Visual | Fields |
|---|---|
| **Card** × 4 (top row) | `Total Revenue (INR)` · `Total Orders` · `Avg Order Value` · `Avg Restaurant Rating` |
| **Map** (ya Filled map) | Location = `restaurant[location]`, Bubble size = `Total Revenue (INR)` |
| **Line chart** | Axis = `Date[Date]` (Month hierarchy), Values = `Total Revenue (INR)`; secondary = `Rolling 30-Day Revenue` |
| **Card** (chhota) | `Repeat Customer Rate %` |

### 📄 Page 2 — Restaurant Performance
> *Title: "Restaurant Performance" — Subtitle: "Kaun se restaurants/cuisines revenue laate hain?"*

| Visual | Fields |
|---|---|
| **Bar chart (Top N)** | Axis = `restaurant[name]`, Value = `Total Revenue (INR)` → Filter pane: `restaurant[name]` → Top N → **20 by Total Revenue** |
| **Scatter chart** | X = `restaurant[cost_for_two]`, Y = `restaurant[rating]`, Size = `restaurant[num_ratings]`, Details = `restaurant[name]` |
| **Treemap / Donut** | Group = `restaurant[cuisine]`, Values = `Total Revenue (INR)` |
| **Table** | `restaurant[location]`, revenue, orders count, avg rating — cost bracket context |

### 📄 Page 3 — Customer Analytics
> *Title: "Customer Analytics" — Subtitle: "Kaun customers Champions hain, kaun ja raha hai?"*

| Visual | Fields |
|---|---|
| **Donut chart** | Legend = `rfm_segments[segment]`, Values = Count of `rfm_segments[user_id]` |
| **Bar (100% ya stacked)** | Axis = `rfm_segments[segment]`, Value = `Revenue Share by Segment` |
| **Table (Top N 20)** | `users[name]`, `rfm_segments[segment]`, `rfm_segments[monetary_inr]`, `rfm_segments[frequency]` → sort desc by monetary; Filter Top N 20 |
| **Card** × 2 | `Repeat Customer Rate %` · One-time share (= 1 − repeat, text box me manual bhi chalega) |

### 📄 Page 4 — Market Basket & Menu
> *Title: "Market Basket & Menu" — Subtitle: "Kaun se cuisine combos saath order hote hain?"*

| Visual | Fields |
|---|---|
| **Horizontal bar** | Axis = `basket_rules[rule]`, Value = `basket_rules[lift]` → Top N **15 by lift**; data labels on |
| **Ribbon/area ya Clustered column** | Axis = `menu[cuisine]`, Value = Average of `menu[price]` (ya Median) — price distribution by cuisine |
| **Table** | `basket_rules[antecedents]`, `basket_rules[consequents]`, `support`, `confidence`, `lift` |

*(Power BI me built-in box plot nahi hota — cuisine-wise avg/median price bar honest approximation hai; NB1 ka price box plot PNG reference ke liye hai.)*

### 📄 Page 5 — Forecast & Trends
> *Title: "Revenue Forecast" — Subtitle: history + engine + MAPE — `forecast_meta.txt` se (e.g. "Holt-Winters, holdout MAPE = XX.X%")*

| Visual | Fields |
|---|---|
| **Line chart** (main) | Axis = `forecast_weekly[week]`, Values = `actual_revenue` + `forecast` (dono ek saath) |
| **Area/band trick** | Same chart ke Values me `lower` aur `upper` bhi daal do (light color) — confidence band approximation |
| **Table** | `festivals[festival]`, `festivals[date]` — festival callout reference |
| **Card** × 2 | last-week actual · last-week forecast |

## 9. Slicers + Cross-page sync

Teeno slicers **har page pe** rakho aur sync karo:
1. `restaurant[location]` (dropdown)
2. `restaurant[cuisine]` (dropdown)
3. `Date[Date]` (between slider)

`View → Sync slicers` → teeno slicers select → **Sync** column me sab 5 pages ✅, **Visible** sab pages ✅.

Extra polish: kisi bhi visual pe right-click → `Edit interactions` — Page 1 ka map Page-1 trend ko filter kare, ye default hi kaafi hai.

## 10. Save + Evidence (portfolio proof)

1. `File → Save As → powerbi\bharatinsight_dashboard.pbix`
2. **Har page ka PNG:** page kholo → `File → Export → ... ` nahi — seedha `Win+Shift+S` screenshot ya `File → Export to PDF` → har page ka screenshot lo → `powerbi\screenshots\page1_overview.png` … `page5_forecast.png` (exactly 5 files)
3. Git: `git add -A && git commit -m "Phase 4: Power BI 5-page dashboard + bridge exports"`

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| PostgreSQL connector "additional components" | Npgsql 4.0.17 msi install (§0) |
| `orders` load slow / fail on auth | Server=`localhost` (127.0.0.1 nahi), encryption checkbox off |
| SAMEPERIODLASTYEAR blank | §3 ka "Mark as date table" miss hua hai |
| Relationship inactive (grey dotted) | Dbl-click line → "Make this relationship active" ✅ |
| Currency mismatch (revenue zyada/kam dikhe) | Check karo measure me `orders[currency]="INR"` filter hai (Rule R1) |
| Power BI Service publish | Free tier me "Publish to web" only if org allows; warna `.pbix` + screenshots are the portfolio artifact |

---

**Done hone pe tumhare paas hoga:** live PostgreSQL-connected 5-page dashboard, 10 real DAX measures, Phase 3 ke RFM/basket/forecast results dashboard me surfaced, 5 screenshot evidence files. **No visual is decoration — har visual ek business question answer karta hai.**
