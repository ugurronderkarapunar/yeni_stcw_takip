"""3 otonom ajan: PDFParserAgent, ComplianceAgent, NotificationAgent."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import smtplib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.message import EmailMessage
from typing import Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from config import get_int, get_secret
from models import (STATUS_ACTIVE, STATUS_EXPIRED, STATUS_WARNING, Certificate, NotificationLog)

log = logging.getLogger("stcw.agents")

# ----------------------------------------------------------------------------
# TARIH NORMALIZASYONU -> ISO YYYY-MM-DD
# ----------------------------------------------------------------------------
_TR_MONTHS = {"OCAK": "JAN", "ŞUBAT": "FEB", "SUBAT": "FEB", "MART": "MAR", "NİSAN": "APR", "NISAN": "APR",
              "MAYIS": "MAY", "HAZİRAN": "JUN", "HAZIRAN": "JUN", "TEMMUZ": "JUL", "AĞUSTOS": "AUG",
              "AGUSTOS": "AUG", "EYLÜL": "SEP", "EYLUL": "SEP", "EKİM": "OCT", "EKIM": "OCT",
              "KASIM": "NOV", "ARALIK": "DEC"}
_MONTHS = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def _year(y: int) -> int:
    return y if y >= 100 else (2000 + y if y < 70 else 1900 + y)


def normalize_date(raw) -> Optional[date]:
    """'15 OCT 2026', '15.10.2026', '2026/10/15', '15/10/2026' -> date. Basarisizsa None."""
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    s = str(raw).strip().upper()
    if not s:
        return None
    for tr, en in _TR_MONTHS.items():
        s = s.replace(tr, en)
    s = re.sub(r"[.,]", " ", s) if re.search(r"[A-Z]{3}", s) else s
    s = re.sub(r"(?<=[A-Z0-9])-(?=[A-Z0-9])", " ", s) if re.search(r"[A-Z]{3}", s) else s
    s = re.sub(r"\s+", " ", s).strip()
    try:
        m = re.fullmatch(r"(\d{1,4})[./\-](\d{1,2})[./\-](\d{1,4})", s)
        if m:
            a, b, c = m.groups()
            if len(a) == 4:                      # YYYY/MM/DD
                return date(int(a), int(b), int(c))
            d, mo, y = int(a), int(b), _year(int(c))
            if mo > 12 >= d:                     # MM/DD/YYYY olasiligi
                d, mo = mo, d
            return date(y, mo, d)
        m = re.fullmatch(r"(\d{1,2}) ([A-Z]{3,9}) (\d{2,4})", s)
        if m and m.group(2)[:3] in _MONTHS:
            return date(_year(int(m.group(3))), _MONTHS[m.group(2)[:3]], int(m.group(1)))
        m = re.fullmatch(r"([A-Z]{3,9}) (\d{1,2}) (\d{2,4})", s)
        if m and m.group(1)[:3] in _MONTHS:
            return date(_year(int(m.group(3))), _MONTHS[m.group(1)[:3]], int(m.group(2)))
    except ValueError:
        return None
    return None


# ----------------------------------------------------------------------------
# AJAN 1: PDF PARSER  (metin katmani -> regex | taranmis/coklu belge -> Gemini Vision)
# ----------------------------------------------------------------------------


def tr_upper(s: str) -> str:
    return re.sub(r"\s+", " ", s.replace("i", "İ").replace("ı", "I").upper()).strip()


@dataclass
class ParsedCertificate:
    seafarer_name: str = "UNKNOWN"
    certificate_name: str = "UNKNOWN CERTIFICATE"
    certificate_no: str = "UNKNOWN"
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    needs_review: bool = False
    warnings: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    source_file: str = ""
    method: str = "regex"
    doc_type: str = ""


_DATE = (r"(\d{1,2}[./\-]\d{1,2}[./\-]\d{2,4}|\d{4}[./\-]\d{1,2}[./\-]\d{1,2}"
         r"|\d{1,2}[\s\-]+[A-Za-zÇĞİÖŞÜçğıöşü]{3,9}\.?[\s\-,]*\d{2,4}|[A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4})")
_RE = {
    "issue": re.compile(r"(?:date\s+of\s+issue|issue\s*date|issued(?:\s+on)?|düzenlenme\s+tarihi|veriliş\s+tarihi)"
                        r"\s*[:\-]?\s*" + _DATE, re.I),
    "expiry": re.compile(r"(?:date\s+of\s+expiry|expiry\s*date|expires?(?:\s+on)?|valid\s+(?:until|till|to)"
                         r"|geçerlilik\s+tarihi|bitiş\s+tarihi)\s*[:\-]?\s*" + _DATE, re.I),
    "number": re.compile(r"(?:certificate|document|doc\.?|cert\.?|belge)\s*(?:no|number|nr|numarası)\.?\s*[:\-#]?\s*"
                         r"([A-Z0-9][A-Z0-9\-/\.]{3,})", re.I),
    "name": re.compile(r"(?:full\s+name|name\s+of\s+(?:the\s+)?(?:holder|seafarer)|holder|seafarer|name|ad\s*soyad)"
                       r"\s*[:\-]\s*([A-Za-zÇĞİÖŞÜçğıöşü' .\-]{3,60})", re.I),
    "cert": re.compile(r"(?im)^(.*\b(?:certificate\s+of\s+(?:proficiency|competency|endorsement)|basic\s+safety"
                       r"|advanced\s+fire\s*fighting|survival\s+craft|medical\s+(?:first\s+aid|care)"
                       r"|security\s+awareness|ECDIS|GMDSS|ARPA|BRM|ERM)\b.*)$"),
}

_PROMPT = """You are an expert reader of Turkish seafarer documents and STCW certificates.
The images are scanned pages (possibly rotated, tilted, bilingual Turkish/English, with stamps and signatures).
A page may contain ONE document or MANY (e.g. a table of 'Other Certificates of Proficiency' where every row is a
separate certificate with its own issue/expiry dates and its own certificate number, usually in parentheses).

Return ONLY JSON: {"documents":[{"document_type","seafarer_name","certificate_name","certificate_no","issue_date","expiry_date"}]}

Rules:
- One object per certificate/document. Pages with no certificate data (explanation/description pages, blank pages) -> {"documents":[]}.
- document_type: SEAFARER_ID | COMPETENCY | MEDICAL | TRAINING | OTHER.
- seafarer_name: given names then surname, e.g. "NAME SURNAME" (use the holder's name printed on the page; ignore MRZ lines).
- certificate_name: prefer the English title; keep the Turkish title in parentheses if short.
  Seafarer ID book -> "Seafarer's Identity Document". Certificate of competency -> include the capacity, e.g. "Certificate of Competency - Chief Engineer".
  Medical -> "Seafarer Medical Certificate".
- certificate_no by type: ID book = document number at top right; competency = 'Yeterlik Belgesi No / Certificate No';
  medical = 'Belge no / Certificate no'; training table = the code in parentheses after the title. Copy character by character.
- issue_date by type: ID = 'Veriliş Tarihi / Date of issue'; competency = 'Düzenleme tarihi / Issued on';
  medical = 'Muayene tarihi / Date of examination'; training table = first date in the left column.
- expiry_date by type: ID = 'Bitiş Tarihi / Date of expiry'; competency = the date in the sentence 'until / tarihine kadar' near the top;
  medical = 'Belgenin geçerliliğinin bitiş tarihi / Expiry date'; training table = second date in the left column.
  If a row shows only ONE date, that is the issue date and expiry_date is null (certificate does not expire).
- NEVER use the date of birth ('Doğum tarihi / Date of birth') as an issue or expiry date.
- Dates as ISO YYYY-MM-DD (Turkish dates are DD.MM.YYYY). If a value is not clearly legible use null. Never guess or invent.
"""


_MODEL_CACHE: dict = {}  # api_key -> sirali model listesi (surec boyunca bir kez listelenir)


class PDFParserAgent:
    def __init__(self):
        self.api_key = get_secret("GEMINI_API_KEY") or get_secret("GOOGLE_API_KEY")
        self.preferred = get_secret("GEMINI_MODEL")  # opsiyonel; bos birakirsaniz otomatik secilir
        self.last_model = None

    def _candidate_models(self, client) -> list[str]:
        """Google'in o an sundugu Flash modellerini listeler (en yeni -> eski). Sonuc onbellege alinir."""
        if _MODEL_CACHE.get(self.api_key):
            return _MODEL_CACHE[self.api_key]
        found = []
        listed = False
        try:
            for m in client.models.list():
                listed = True
                n = (m.name or "").replace("models/", "")
                acts = getattr(m, "supported_actions", None) or ["generateContent"]
                mm = re.fullmatch(r"gemini-(\d+(?:\.\d+)?)-flash(-lite)?(-preview[\w\-]*)?", n)
                if mm and "generateContent" in acts:
                    found.append((-float(mm.group(1)), 1 if mm.group(2) else 0, 1 if mm.group(3) else 0, n))
        except Exception as e:
            log.warning("Model listesi alinamadi: %s", e)
        names = [t[3] for t in sorted(found)]
        order = []
        if self.preferred and (not listed or self.preferred in names):
            order.append(self.preferred)
        order += names or ["gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3-flash-preview"]
        result = list(dict.fromkeys(order))[:4]
        if listed:
            _MODEL_CACHE[self.api_key] = result
        return result

    # ---------- yardimcilar ----------
    @staticmethod
    def extract_text(pdf_bytes: bytes) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages[:5]).strip()
        except Exception as e:
            log.warning("pdfplumber hatasi: %s", e)
            return ""

    @staticmethod
    def _render_pages(pdf_bytes: bytes, max_pages: int = 4) -> list[bytes]:
        """Taranmis PDF'i kucuk JPEG'lere cevirir (2-3 MB PDF -> ~300 KB gorsel)."""
        try:
            import pdfplumber
            out = []
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                for pg in pdf.pages[:max_pages]:
                    im = pg.to_image(resolution=150).original.convert("RGB")
                    im.thumbnail((1800, 1800))
                    buf = io.BytesIO()
                    im.save(buf, "JPEG", quality=85)
                    out.append(buf.getvalue())
            return out
        except Exception as e:
            log.warning("Sayfa render hatasi: %s", e)
            return []

    @staticmethod
    def _regex_extract(text: str) -> dict:
        out = {}
        for key in ("issue", "expiry"):
            m = _RE[key].search(text)
            out[key + "_date"] = normalize_date(m.group(1)) if m else None
        for key, field_name in (("number", "certificate_no"), ("name", "seafarer_name"), ("cert", "certificate_name")):
            m = _RE[key].search(text)
            out[field_name] = m.group(1).strip()[:150] if m else None
        return out

    def _gemini_extract(self, text: str, images: list[bytes], pdf_bytes: bytes):
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)
        parts = ([types.Part.from_bytes(data=b, mime_type="image/jpeg") for b in images]
                 or [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")])
        prompt = _PROMPT + (f"\nText-layer hint:\n{text[:6000]}" if text else "")
        errors = []
        for model in self._candidate_models(client):
            for attempt in range(3):
                try:
                    resp = client.models.generate_content(
                        model=model, contents=parts + [prompt],
                        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0))
                    raw = re.sub(r"^```(?:json)?|```$", "", (resp.text or "").strip(), flags=re.M).strip()
                    self.last_model = model
                    cache = _MODEL_CACHE.get(self.api_key)
                    if cache:  # calisan modeli one al: sonraki dosyalar hatali modelleri denemez
                        _MODEL_CACHE[self.api_key] = [model] + [m for m in cache if m != model]
                    return json.loads(raw)
                except Exception as e:
                    msg = str(e)
                    errors.append(f"{model}: {msg[:160]}")
                    if attempt < 2 and any(c in msg for c in ("429", "500", "503", "UNAVAILABLE", "RESOURCE_EXHAUSTED")):
                        time.sleep(4 * (attempt + 1))
                        continue
                    break
        raise RuntimeError(" | ".join(errors[-3:]))

    def _build(self, d: dict, filename: str, pdf_bytes: bytes, idx: int, method: str) -> ParsedCertificate:
        r = ParsedCertificate(source_file=filename, method=method, doc_type=str(d.get("document_type") or ""))
        stem = re.sub(r"\.pdf$", "", filename, flags=re.I).replace("_", " ") or "certificate"
        r.seafarer_name = tr_upper(d["seafarer_name"]) if d.get("seafarer_name") else "UNKNOWN"
        r.certificate_name = str(d.get("certificate_name") or stem)[:200]
        r.certificate_no = (re.sub(r"\s+", "", str(d["certificate_no"])) if d.get("certificate_no")
                            else f"UNKNOWN-{hashlib.md5(pdf_bytes).hexdigest()[:6].upper()}-{idx + 1}")
        r.issue_date, r.expiry_date = normalize_date(d.get("issue_date")), normalize_date(d.get("expiry_date"))

        missing = [lbl for lbl, ok in (("personel adı", d.get("seafarer_name")), ("belge adı", d.get("certificate_name")),
                                       ("belge no", d.get("certificate_no")), ("veriliş tarihi", r.issue_date)) if not ok]
        if missing:
            r.warnings.append("Eksik/okunamayan: " + ", ".join(missing))
        if not r.expiry_date:
            r.notes.append("Bitiş tarihi yok → süresiz kabul edildi (gerekiyorsa elle girin)")
        if r.issue_date and r.expiry_date and r.expiry_date < r.issue_date:
            r.warnings.append("Bitiş tarihi veriliş tarihinden önce")
        r.needs_review = bool(r.warnings)
        return r

    def parse_many(self, files: list[tuple[bytes, str]], max_workers: int = 3, on_done=None) -> list[tuple[str, list]]:
        """Dosyalari paralel isler (Gemini bekleme suresi cakisir). Sonuc girdi sirasini korur."""
        results: dict[int, list] = {}
        with ThreadPoolExecutor(max_workers=max(1, min(max_workers, len(files) or 1))) as ex:
            futs = {ex.submit(self.parse, data, name): i for i, (data, name) in enumerate(files)}
            for n, fut in enumerate(as_completed(futs), 1):
                i = futs[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # parse() zaten firlatmaz; ek guvence
                    r = ParsedCertificate(source_file=files[i][1], needs_review=True, warnings=[f"Beklenmeyen hata: {e}"])
                    results[i] = [r]
                if on_done:
                    on_done(n, len(files), files[i][1])
        return [(files[i][1], results[i]) for i in range(len(files))]

    # ---------- ana akis ----------
    def parse(self, pdf_bytes: bytes, filename: str = "") -> list[ParsedCertificate]:
        """Asla exception firlatmaz. Bir dosyadan 0..N belge dondurur (0 = belge verisi yok)."""
        critical = ("seafarer_name", "certificate_name", "certificate_no", "expiry_date")
        text = self.extract_text(pdf_bytes)
        regex_data = self._regex_extract(text) if len(text) >= 80 else {}
        if regex_data and all(regex_data.get(k) for k in critical):
            return [self._build(regex_data, filename, pdf_bytes, 0, "regex")]

        if not self.api_key:
            r = self._build(regex_data, filename, pdf_bytes, 0, "none")
            r.warnings.insert(0, "Taranmış PDF için GEMINI_API_KEY gerekli (.env dosyasını kontrol edin).")
            r.needs_review = True
            return [r]
        try:
            data = self._gemini_extract(text, self._render_pages(pdf_bytes), pdf_bytes)
        except Exception as e:
            r = self._build(regex_data, filename, pdf_bytes, 0, "gemini-error")
            r.warnings.insert(0, f"Gemini çağrısı başarısız: {e}")
            r.needs_review = True
            return [r]

        docs = data.get("documents", []) if isinstance(data, dict) else data
        docs = [x for x in (docs or []) if isinstance(x, dict) and (x.get("certificate_name") or x.get("certificate_no"))]
        names = [tr_upper(x["seafarer_name"]) for x in docs if x.get("seafarer_name")]
        common = max(set(names), key=names.count) if names else None
        for x in docs:  # ayni sayfadaki belgelerde bos kalan ismi tamamla
            if not x.get("seafarer_name") and common:
                x["seafarer_name"] = common
        return [self._build(x, filename, pdf_bytes, i, "gemini-vision") for i, x in enumerate(docs)]


# ----------------------------------------------------------------------------
# AJAN 2: COMPLIANCE & DB
# ----------------------------------------------------------------------------
class ComplianceAgent:
    def __init__(self, warning_days: Optional[int] = None):
        self.warning_days = warning_days or get_int("WARNING_DAYS", 15)

    def compute_status(self, expiry: Optional[date], today: Optional[date] = None) -> str:
        today = today or date.today()
        if expiry is None:
            return STATUS_ACTIVE
        if expiry < today:
            return STATUS_EXPIRED
        if (expiry - today).days <= self.warning_days:
            return STATUS_WARNING
        return STATUS_ACTIVE

    def upsert(self, s: Session, *, seafarer_name, certificate_name, certificate_no, issue_date, expiry_date,
               needs_review=False, source_file=None, notify_email=None, notify_phone=None) -> Certificate:
        cert = s.scalar(select(Certificate).where(Certificate.certificate_no == certificate_no,
                                                  Certificate.seafarer_name == seafarer_name))
        if cert is None:
            cert = Certificate(seafarer_name=seafarer_name, certificate_no=certificate_no)
            s.add(cert)
        self._apply(cert, certificate_name=certificate_name, issue_date=issue_date, expiry_date=expiry_date,
                    needs_review=needs_review, source_file=source_file,
                    notify_email=notify_email, notify_phone=notify_phone)
        s.flush()
        return cert

    def update(self, s: Session, cert_id: int, **fields) -> Optional[Certificate]:
        cert = s.get(Certificate, cert_id)
        if cert:
            self._apply(cert, **fields)
            s.flush()
        return cert

    def _apply(self, cert: Certificate, **fields):
        old_expiry = cert.expiry_date
        for k, v in fields.items():
            if k in ("seafarer_name", "certificate_no") and not v:
                continue
            setattr(cert, k, v)
        if "expiry_date" in fields and fields["expiry_date"] != old_expiry:
            cert.last_notified_type, cert.is_notified, cert.last_notified_at = None, False, None  # yenilenen belge
        cert.status = self.compute_status(cert.expiry_date)

    def delete(self, s: Session, cert_id: int) -> bool:
        cert = s.get(Certificate, cert_id)
        if cert:
            s.delete(cert)
            return True
        return False

    def refresh_statuses(self, s: Session) -> int:
        changed = 0
        for cert in s.scalars(select(Certificate)):
            new = self.compute_status(cert.expiry_date)
            if new != cert.status:
                cert.status = new
                changed += 1
                if new == STATUS_ACTIVE:
                    cert.last_notified_type, cert.is_notified = None, False
        s.flush()
        return changed

    def list_all(self, s: Session) -> list[Certificate]:
        return list(s.scalars(select(Certificate).order_by(Certificate.expiry_date.asc().nulls_last())))


# ----------------------------------------------------------------------------
# AJAN 3: NOTIFICATION
# ----------------------------------------------------------------------------
class NotificationAgent:
    def draft(self, cert: Certificate) -> tuple[str, str]:
        exp = cert.expiry_date.strftime("%d.%m.%Y") if cert.expiry_date else "-"
        if cert.status == STATUS_EXPIRED:
            subject = f"[SURESI DOLDU] {cert.certificate_name} - {cert.seafarer_name}"
            lead = f"{cert.seafarer_name} adli personelin '{cert.certificate_name}' belgesinin suresi {exp} tarihinde DOLMUSTUR."
        else:
            days = (cert.expiry_date - date.today()).days if cert.expiry_date else 0
            subject = f"[{days} GUN KALDI] {cert.certificate_name} - {cert.seafarer_name}"
            lead = (f"{cert.seafarer_name} adli personelin '{cert.certificate_name}' belgesinin suresi "
                    f"{exp} tarihinde dolacak ({days} gun kaldi).")
        body = f"{lead}\nBelge No: {cert.certificate_no}\nLutfen yenileme islemlerini baslatin.\n\n-- STCW Sertifika Takip Sistemi"
        return subject, body

    # --- kanallar ---
    @staticmethod
    def send_email(to: str, subject: str, body: str) -> None:
        user, pwd = get_secret("SMTP_EMAIL"), get_secret("SMTP_PASSWORD")
        if not (user and pwd):
            raise RuntimeError("SMTP_EMAIL / SMTP_PASSWORD tanimli degil")
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, to, subject
        msg.set_content(body)
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
            smtp.login(user, pwd)
            smtp.send_message(msg)

    @staticmethod
    def send_whatsapp(phone: str, body: str) -> None:
        provider = (get_secret("WHATSAPP_PROVIDER", "greenapi") or "greenapi").lower()
        key = get_secret("WHATSAPP_API_KEY")
        if not key:
            raise RuntimeError("WHATSAPP_API_KEY tanimli degil")
        phone = re.sub(r"\D", "", phone)
        if provider == "twilio":
            sid, _, token = key.partition(":")
            data = urllib.parse.urlencode({"From": get_secret("TWILIO_WHATSAPP_FROM", "whatsapp:+14155238886"),
                                           "To": f"whatsapp:+{phone}", "Body": body}).encode()
            req = urllib.request.Request(f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json", data=data)
            req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
        else:  # Green API
            inst = get_secret("GREEN_API_INSTANCE_ID")
            if not inst:
                raise RuntimeError("GREEN_API_INSTANCE_ID tanimli degil")
            req = urllib.request.Request(f"https://api.green-api.com/waInstance{inst}/sendMessage/{key}",
                                         data=json.dumps({"chatId": f"{phone}@c.us", "message": body}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            if r.status >= 300:
                raise RuntimeError(f"WhatsApp HTTP {r.status}")

    # --- ana akis ---
    def run(self, s: Session) -> list[dict]:
        """Spam korumasi: last_notified_type == status ise ASLA tekrar gonderilmez."""
        stmt = (select(Certificate)
                .where(Certificate.status.in_([STATUS_WARNING, STATUS_EXPIRED]),
                       or_(Certificate.last_notified_type.is_(None), Certificate.last_notified_type != Certificate.status))
                .with_for_update(skip_locked=True))  # es zamanli (Streamlit+Actions) cift gonderimi engeller
        results = []
        for cert in s.scalars(stmt).all():
            subject, body = self.draft(cert)
            email = cert.notify_email or get_secret("NOTIFY_DEFAULT_EMAIL") or get_secret("SMTP_EMAIL")
            phone = cert.notify_phone or get_secret("NOTIFY_DEFAULT_PHONE")
            sent_any, rec = False, {"certificate_id": cert.id, "name": cert.seafarer_name,
                                    "certificate": cert.certificate_name, "type": cert.status, "channels": {}}
            attempts = []
            if email:
                attempts.append(("email", email, lambda e=email: self.send_email(e, subject, body)))
            if phone and get_secret("WHATSAPP_API_KEY"):
                attempts.append(("whatsapp", phone, lambda p=phone: self.send_whatsapp(p, f"{subject}\n\n{body}")))
            for channel, rcpt, fn in attempts:
                try:
                    fn()
                    ok, detail = True, "sent"
                except Exception as e:
                    ok, detail = False, str(e)[:500]
                sent_any |= ok
                rec["channels"][channel] = detail
                s.add(NotificationLog(certificate_id=cert.id, channel=channel, notification_type=cert.status,
                                      recipient=rcpt, success=ok, detail=detail))
            if sent_any:
                cert.last_notified_type, cert.is_notified = cert.status, True
                cert.last_notified_at = datetime.now(timezone.utc)
            s.commit()  # her belge icin ayri commit: yarida kesilirse tekrar gonderim olmaz
            results.append(rec)
        return results
