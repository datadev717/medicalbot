## Render.com ga deploy (tezkor yo‘riqnoma)

### 1) GitHub’ga yuklang
```bash
git init
git add .
git commit -m "Medical bot"
git remote add origin https://github.com/USERNAME/REPO.git
git push -u origin main
```

### 2) Render’da Web Service yarating
Render → **New +** → **Web Service** → GitHub repo’ni tanlang.

Render sozlamalari:
- **Build Command:** `pip install -r requirements.txt`
- **Start Command:** `gunicorn bot:app --bind 0.0.0.0:$PORT --workers 1 --timeout 120`

### 3) Environment Variables qo‘ying
Render → Service → **Environment**:
- `BOT_TOKEN` = Telegram bot token
- `ADMIN_ID` = sizning Telegram ID
- `TEST_MODE` = `true` (test) yoki `false` (prod)

Ixtiyoriy:
- `AUTO_SET_WEBHOOK` = `true` qilsangiz webhook avtomatik qo‘yiladi
- `PUBLIC_URL` = `https://SIZNING-SERVICE.onrender.com`

### 4) Webhook o‘rnating
Deploy tugagach, quyidagilardan birini qiling:

**Variant A (tavsiya):** `AUTO_SET_WEBHOOK=true` va `PUBLIC_URL` ni qo‘ying → qayta deploy/restart qiling.

**Variant B (qo‘lda):** brauzerda:
```
https://SIZNING-SERVICE.onrender.com/set_webhook?url=https://SIZNING-SERVICE.onrender.com
```

### 5) Tekshirish
- `GET /` → status ok qaytadi
- Telegram’da `/start` yuboring

### Eslatma
Bot SQLite fayl (`medical_bot.db`) bilan ishlaydi. Hosting restart/deploy’da fayl yo‘qolishi mumkin.
Ma’lumot doimiy bo‘lsin desangiz, Persistent Disk yoki tashqi DB’ga o‘tish kerak bo‘ladi.
