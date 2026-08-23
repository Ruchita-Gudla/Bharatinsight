# data/raw — original Kaggle CSVs (never edited in place)

Download the dataset from:
https://www.kaggle.com/datasets/anas123siddiqui/zomato-database

Extract it here and rename the files to **exactly** these names:

| Expected filename | Contents |
|---|---|
| `users.csv` | customer demographics |
| `restaurant.csv` | restaurant metadata |
| `menu.csv` | restaurant ↔ food ↔ price mapping |
| `food.csv` | food item catalog |
| `orders.csv` | transactions |

These files are git-ignored on purpose (size + redistribution). The first
thing to do after placing them here is:

```powershell
python scripts/verify_schema.py
```

…then read the generated `schema_report.md` in the project root.
