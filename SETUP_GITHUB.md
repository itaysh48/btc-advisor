# הוראות הגדרה — GitHub Actions (רץ 24/7 גם כשהמחשב כבוי)

## שלב 1 — צור Repository ב-GitHub

1. לך ל-https://github.com/new
2. שם ה-repo: `btc-advisor` (או כל שם)
3. **Public** (חינמי ללא הגבלה)
4. לחץ **Create repository**

## שלב 2 — העלה את הפרויקט ל-GitHub

פתח Terminal בתיקייה זו והרץ:

```bash
cd "/Users/itaysh/ביטקיון חמש דקות/btc-advisor"
git init
git add .
git commit -m "initial commit"
git branch -M main
git remote add origin https://github.com/YOUR_USERNAME/btc-advisor.git
git push -u origin main
```

החלף `YOUR_USERNAME` בשם המשתמש שלך ב-GitHub.

## שלב 3 — הפעל GitHub Actions

1. לך ל-repo שלך ב-GitHub
2. לחץ על **Actions**
3. אם מוצג "Workflows aren't running" — לחץ "I understand my workflows, go ahead and enable them"
4. תראה את ה-workflow **"BTC Learn — 5min cron"**
5. לחץ **Run workflow** → **Run workflow** להפעלה ראשונה (יאמן את המודל — לוקח ~2 דקות)

## שלב 4 — חבר את האתר לנתוני הענן

לאחר שה-workflow רץ בהצלחה, תיווצר קובץ `btc-advisor/state/stats.json` ב-repo.

ה-URL שלו הוא:
```
https://raw.githubusercontent.com/YOUR_USERNAME/btc-advisor/main/btc-advisor/state/stats.json
```

1. פתח את `btc-advisor.html` בדפדפן
2. בתיבת ה-URL בראש הדף, הדבק את ה-URL
3. לחץ **שמור**
4. הסטטיסטיקה תופיע מיד

## מה קורה מעכשיו?

- **כל 5 דקות**: GitHub Actions מריץ את `backend/worker.py`
- Worker מוריד נרות עדכניים, בודק אם ההימור הקודם הצליח, לומד מהתוצאה
- תוצאות מתעדכנות ב-`state/stats.json` שבענן
- האתר שלך מציג את הנתונים בזמן אמת — גם כשהמחשב כבוי

## מעקב

- **GitHub Actions לוגים**: https://github.com/YOUR_USERNAME/btc-advisor/actions
- **stats.json עדכני**: https://raw.githubusercontent.com/YOUR_USERNAME/btc-advisor/main/btc-advisor/state/stats.json

## עלות

**$0** — GitHub Actions חינמי לחלוטין ל-public repositories.
