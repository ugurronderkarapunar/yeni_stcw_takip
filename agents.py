"""3 otonom ajan: PDFParserAgent, ComplianceAgent, NotificationAgent."""
from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import re
import smtplib
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
# AJAN 1: PDF PARSER
# ----------------------------------------------------------------------------
@dataclass
class ParsedCertificate:
    seafarer_name: str = "UNKNOWN"
    certificate_name: str = "UNKNOWN CERTIFICATE"
    certificate_no: str = "UNKNOWN"
    issue_date: Optional[date] = None
    expiry_date: Optional[date] = None
    needs_review: bool = False
    warnings: list = field(default_factory=list)
    source_file: str = ""
    method: str = "regex"


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

_PROMPT = """You are a maritime STCW certificate analyst. Extract fields from the certificate.
Return ONLY a JSON object with keys: seafarer_name, certificate_name, certificate_no, issue_date, expiry_date.
Dates must be ISO YYYY-MM-DD (convert any format such as '15 OCT 2026', '15.10.2026', '2026/10/15').
Use null for any value that is not present. Do not invent values."""


class PDFParserAgent:
    def __init__(self):
        self.api_key = get_secret("GEMINI_API_KEY")
        self.model = get_secret("GEMINI_MODEL", "gemini-2.5-flash")

    @staticmethod
    def extract_text(pdf_bytes: bytes) -> str:
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                return "\n".join((p.extract_text() or "") for p in pdf.pages[:5]).strip()
        except Exception as e:  # bozuk/sifreli PDF: hata firlatma
            log.warning("pdfplumber hatasi: %s", e)
            return ""

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

    def _gemini_extract(self, text: str, pdf_bytes: bytes) -> dict:
        from google import genai
        from google.genai import types
        client = genai.Client(api_key=self.api_key)
        if len(text) >= 80:
            contents = [_PROMPT + "\n\nDOCUMENT TEXT:\n" + text[:12000]]
        else:  # taranmis PDF: Gemini'ye dogrudan PDF gonder (OCR yerine)
            contents = [types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf"), _PROMPT]
        resp = client.models.generate_content(
            model=self.model, contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json", temperature=0),
        )
        data = json.loads(resp.text)
        if isinstance(data, list):
            data = data[0] if data else {}
        return {
            "seafarer_name": data.get("seafarer_name"), "certificate_name": data.get("certificate_name"),
            "certificate_no": data.get("certificate_no"),
            "issue_date": normalize_date(data.get("issue_date")), "expiry_date": normalize_date(data.get("expiry_date")),
        }

    def parse(self, pdf_bytes: bytes, filename: str = "") -> ParsedCertificate:
        """Asla exception firlatmaz; eksik alanlar icin varsayilan + needs_review dondurur."""
        res = ParsedCertificate(source_file=filename)
        text = self.extract_text(pdf_bytes)
        data = self._regex_extract(text) if text else {}
        critical = ("seafarer_name", "certificate_name", "certificate_no", "expiry_date")

        if any(not data.get(k) for k in critical):
            if self.api_key:
                try:
                    llm = self._gemini_extract(text, pdf_bytes)
                    for k, v in llm.items():
                        if v and not data.get(k):
                            data[k] = v
                    res.method = "regex+gemini" if text else "gemini-vision"
                except Exception as e:
                    res.warnings.append(f"Gemini fallback basarisiz: {e}")
            else:
                res.warnings.append("GEMINI_API_KEY yok; LLM fallback atlandi.")
        if not text:
            res.warnings.append("PDF taranmis/gorsel gorunuyor; metin katmani yok.")

        stem = re.sub(r"\.pdf$", "", filename, flags=re.I) or "certificate"
        res.seafarer_name = data.get("seafarer_name") or "UNKNOWN"
        res.certificate_name = data.get("certificate_name") or stem.replace("_", " ")[:150]
        res.certificate_no = data.get("certificate_no") or "UNKNOWN-" + hashlib.md5(pdf_bytes).hexdigest()[:8].upper()
        res.issue_date, res.expiry_date = data.get("issue_date"), data.get("expiry_date")

        missing = [k for k in critical if not data.get(k)]
        if missing:
            res.warnings.append("Eksik alanlar (varsayilan atandi): " + ", ".join(missing))
        if res.issue_date and res.expiry_date and res.expiry_date < res.issue_date:
            res.warnings.append("Bitis tarihi veris tarihinden once; lutfen kontrol edin.")
        res.needs_review = bool(res.warnings)
        return res


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
