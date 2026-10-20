from datetime import date, datetime

import pandas as pd
import streamlit as st

from agents import ComplianceAgent, NotificationAgent, PDFParserAgent
from config import get_secret
from database import init_db, session_scope
from models import NotificationLog, STATUS_ACTIVE, STATUS_EXPIRED, STATUS_WARNING
from scheduler import run_daily_job, start_scheduler

st.set_page_config(page_title="STCW Sertifika Takip", page_icon="⚓", layout="wide")


@st.cache_resource
def _boot():
    init_db()
    return start_scheduler()  # process basina bir kez


# --- basit parola korumasi (APP_PASSWORD tanimliysa) ---
_pw = get_secret("APP_PASSWORD")
if _pw and not st.session_state.get("auth"):
    entered = st.text_input("Panel şifresi", type="password")
    if entered:
        if entered == _pw:
            st.session_state["auth"] = True
            st.rerun()
        st.error("Hatalı şifre")
    st.stop()

try:
    scheduler = _boot()
except Exception as e:
    st.error(f"Veritabanı bağlantısı kurulamadı: {e}")
    st.stop()

compliance = ComplianceAgent()
MIN_D, MAX_D = date(1950, 1, 1), date(2100, 12, 31)
COLORS = {STATUS_ACTIVE: "#d4edda", STATUS_WARNING: "#fff3cd", STATUS_EXPIRED: "#f8d7da"}

st.title("⚓ STCW Sertifika Takip Paneli")
tab_up, tab_tbl, tab_notif = st.tabs(["📄 PDF Yükle & Ayrıştır", "📋 Takip Tablosu", "🔔 Bildirimler"])

# ============================ TAB 1 ============================
def _d(v):
    """data_editor degerini (NaT/Timestamp/date) guvenle date'e cevirir."""
    if v is None or pd.isna(v):
        return None
    return v.date() if isinstance(v, datetime) else v


with tab_up:
    files = st.file_uploader("STCW PDF belgeleri (taranmış olabilir, bir dosyada birden çok belge olabilir)",
                             type=["pdf"], accept_multiple_files=True)
    if files and st.button("🤖 PDF Parser Ajanını Çalıştır", type="primary"):
        parser, found, empty = PDFParserAgent(), [], []
        with st.spinner("Belgeler analiz ediliyor (taranmış PDF'lerde 10-30 sn sürebilir)..."):
            for f in files:
                res = parser.parse(f.getvalue(), f.name)
                (found.extend(res) if res else empty.append(f.name))
        st.session_state["parsed"], st.session_state["empty"] = found, empty

    for name in st.session_state.get("empty", []):
        st.info(f"ℹ️ **{name}**: sertifika verisi bulunamadı (açıklama/boş sayfa olabilir), atlandı.")

    parsed = st.session_state.get("parsed", [])
    if parsed:
        st.caption("Tabloyu doğrudan düzenleyebilirsiniz. Kaydetmek istemediklerinizin ✔ işaretini kaldırın.")
        df = pd.DataFrame([{
            "Kaydet": True, "Personel": p.seafarer_name, "Belge": p.certificate_name, "Belge No": p.certificate_no,
            "Veriliş": p.issue_date, "Bitiş": p.expiry_date, "Kaynak": p.source_file,
            "Uyarı": " | ".join(p.warnings + p.notes)} for p in parsed])
        edited = st.data_editor(
            df, hide_index=True, use_container_width=True, key="parsed_editor",
            disabled=["Kaynak", "Uyarı"],
            column_config={
                "Kaydet": st.column_config.CheckboxColumn(width="small"),
                "Veriliş": st.column_config.DateColumn(format="DD.MM.YYYY", min_value=MIN_D, max_value=MAX_D),
                "Bitiş": st.column_config.DateColumn(format="DD.MM.YYYY", min_value=MIN_D, max_value=MAX_D),
            })
        e1, e2 = st.columns(2)
        email = e1.text_input("Bildirim E-postası (opsiyonel, hepsine uygulanır)")
        phone = e2.text_input("WhatsApp No (905xxxxxxxxx, opsiyonel)")
        if st.button("✅ Seçilenleri Kaydet", type="primary"):
            saved = 0
            with session_scope() as s:
                for i, r in edited.iterrows():
                    if r["Kaydet"]:
                        compliance.upsert(s, seafarer_name=str(r["Personel"]).strip(), certificate_name=str(r["Belge"]).strip(),
                                          certificate_no=str(r["Belge No"]).strip(), issue_date=_d(r["Veriliş"]),
                                          expiry_date=_d(r["Bitiş"]), needs_review=False, source_file=r["Kaynak"],
                                          notify_email=email.strip() or None, notify_phone=phone.strip() or None)
                        saved += 1
            st.session_state["parsed"] = []
            st.success(f"{saved} belge kaydedildi. 'Takip Tablosu' sekmesine bakın.")

# ============================ TAB 2 ============================
with tab_tbl:
    with session_scope() as s:
        compliance.refresh_statuses(s)
        rows = [dict(id=c.id, personel=c.seafarer_name, belge=c.certificate_name, no=c.certificate_no,
                     issue=c.issue_date, expiry=c.expiry_date, status=c.status, review=c.needs_review,
                     email=c.notify_email, phone=c.notify_phone, notified=c.last_notified_type)
                for c in compliance.list_all(s)]
    if not rows:
        st.info("Henüz kayıt yok. İlk PDF'i yükleyin.")
    else:
        df = pd.DataFrame(rows)
        m1, m2, m3 = st.columns(3)
        m1.metric("🟢 Aktif", int((df.status == STATUS_ACTIVE).sum()))
        m2.metric("🟡 15 Gün İçinde", int((df.status == STATUS_WARNING).sum()))
        m3.metric("🔴 Süresi Dolmuş", int((df.status == STATUS_EXPIRED).sum()))

        today = date.today()
        view = pd.DataFrame({
            "ID": df.id, "Personel": df.personel, "Belge": df.belge, "Belge No": df.no,
            "Veriliş": df.issue, "Bitiş": df.expiry,
            "Kalan Gün": [(e - today).days if e else None for e in df.expiry],
            "Durum": df.status, "Son Bildirim": df.notified,
        })
        st.dataframe(view.style.apply(lambda r: [f"background-color:{COLORS.get(r['Durum'], '')};color:#111"] * len(r), axis=1),
                     use_container_width=True, hide_index=True)

        st.subheader("✏️ Düzenle / 🗑️ Sil")
        labels = {r["id"]: f"#{r['id']} · {r['personel']} · {r['belge']}" for r in rows}
        sel = st.selectbox("Kayıt", list(labels), format_func=labels.get)
        r = next(x for x in rows if x["id"] == sel)
        with st.form(f"edit_{sel}"):
            c1, c2, c3 = st.columns(3)
            n = c1.text_input("Personel Adı", r["personel"])
            cn = c2.text_input("Belge Adı", r["belge"])
            no = c3.text_input("Belge No", r["no"])
            d1, d2 = st.columns(2)
            iss = d1.date_input("Veriliş", r["issue"], min_value=MIN_D, max_value=MAX_D, format="DD/MM/YYYY")
            exp = d2.date_input("Bitiş", r["expiry"], min_value=MIN_D, max_value=MAX_D, format="DD/MM/YYYY")
            e1, e2 = st.columns(2)
            em = e1.text_input("E-posta", r["email"] or "")
            ph = e2.text_input("WhatsApp", r["phone"] or "")
            if st.form_submit_button("💾 Güncelle"):
                with session_scope() as s:
                    compliance.update(s, sel, seafarer_name=n.strip(), certificate_name=cn.strip(), certificate_no=no.strip(),
                                      issue_date=iss, expiry_date=exp, needs_review=False,
                                      notify_email=em.strip() or None, notify_phone=ph.strip() or None)
                st.success("Güncellendi (bitiş tarihi değiştiyse bildirim durumu sıfırlandı).")
                st.rerun()
        confirm = st.checkbox("Silmeyi onaylıyorum")
        if st.button("🗑️ Kaydı Sil", disabled=not confirm):
            with session_scope() as s:
                compliance.delete(s, sel)
            st.rerun()

# ============================ TAB 3 ============================
with tab_notif:
    job = scheduler.get_job("daily_stcw_check")
    st.caption(f"Sonraki otomatik kontrol: {job.next_run_time if job else '—'}")
    if st.button("▶️ Günlük Kontrolü Şimdi Çalıştır"):
        with st.spinner("Çalışıyor..."):
            res = run_daily_job()
        st.success(f"{res['status_changed']} durum güncellendi, {len(res['notifications'])} bildirim işlendi.")
        if res["notifications"]:
            st.json(res["notifications"])
    st.subheader("Son Bildirim Kayıtları")
    with session_scope() as s:
        logs = s.query(NotificationLog).order_by(NotificationLog.created_at.desc()).limit(50).all()
        data = [dict(Zaman=l.created_at, Belge_ID=l.certificate_id, Kanal=l.channel, Tür=l.notification_type,
                     Alıcı=l.recipient, Başarılı=l.success, Detay=l.detail) for l in logs]
    st.dataframe(pd.DataFrame(data), use_container_width=True, hide_index=True)
