# ⚓ STCW Sertifika Takip – Hızlı Kurulum ($0)

## 1) Gemini API Key (Google AI Studio, ücretsiz)
1. https://aistudio.google.com adresine Google hesabıyla girin.
2. Sol menüden **Get API key** → **Create API key** → (yeni veya mevcut proje seçin).
3. Anahtarı kopyalayın → `GEMINI_API_KEY`.
> Not: `gemini-1.5-flash` Google tarafından kapatıldı. Varsayılan model `gemini-2.5-flash`'tır (`GEMINI_MODEL` ile değiştirilebilir). Ücretsiz katmanda istek limiti vardır; ajan LLM'i yalnızca regex yetersiz kaldığında çağırır.

## 2) Neon PostgreSQL (ücretsiz)
1. https://neon.tech → proje oluşturun → **Connection string** (Pooled) kopyalayın → `DATABASE_URL`.
2. Tablolar uygulama ilk açılışta otomatik oluşturulur.

## 3) Gmail Uygulama Şifresi (SMTP)
1. Google Hesabı → **Güvenlik** → **2 Adımlı Doğrulama**'yı açın.
2. https://myaccount.google.com/apppasswords adresine gidin.
3. Uygulama adı: `STCW Tracker` → **Oluştur**.
4. Çıkan 16 haneli şifreyi **boşluksuz** `SMTP_PASSWORD` olarak kullanın; `SMTP_EMAIL` Gmail adresinizdir.

## 4) GitHub + Streamlit Cloud
1. Yeni **private** GitHub deposu oluşturun ve bu klasörü push edin:
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/KULLANICI/stcw-tracker.git
   git push -u origin main
   ```
   (`.env` ve `secrets.toml` `.gitignore`'dadır; asla commit etmeyin.)
2. https://share.streamlit.io → **Create app** → depoyu seçin, **Main file path:** `app.py`.
3. **Advanced settings** → Python **3.11** seçin → **Secrets** kutusuna `.streamlit/secrets.toml.example` içeriğini kendi değerlerinizle yapıştırın.
4. **Deploy**. Yerelde test: `pip install -r requirements.txt && streamlit run app.py`.

## 5) Günlük Zamanlayıcı Güvencesi (opsiyonel, ücretsiz)
Streamlit Cloud kullanılmayan uygulamayı uykuya alır ve APScheduler durur. `.github/workflows/daily.yml` her gün `python scheduler.py --once` çalıştırır. GitHub → **Settings → Secrets and variables → Actions** altına `DATABASE_URL`, `GEMINI_API_KEY`, `SMTP_EMAIL`, `SMTP_PASSWORD`, `NOTIFY_DEFAULT_EMAIL`, `WHATSAPP_API_KEY` ekleyin. Her iki tetikleyici aynı anda çalışsa da veritabanı bayrağı (`last_notified_type`) sayesinde mükerrer mesaj gitmez.

## Spam Engelleme Mantığı
`status ∈ {WARNING_15_DAYS, EXPIRED}` **ve** `last_notified_type ≠ status` ise gönderilir; en az bir kanal başarılıysa bayrak güncellenir. Bitiş tarihi düzenlenirse (belge yenilendi) bayrak sıfırlanır.
