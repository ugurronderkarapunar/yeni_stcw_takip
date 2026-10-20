from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

STATUS_ACTIVE = "ACTIVE"
STATUS_WARNING = "WARNING_15_DAYS"
STATUS_EXPIRED = "EXPIRED"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Certificate(Base):
    __tablename__ = "certificates"
    __table_args__ = (UniqueConstraint("certificate_no", "seafarer_name", name="uq_cert_no_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    seafarer_name: Mapped[str] = mapped_column(String(200), index=True)
    certificate_name: Mapped[str] = mapped_column(String(300))
    certificate_no: Mapped[str] = mapped_column(String(120))
    issue_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    expiry_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default=STATUS_ACTIVE, index=True)
    needs_review: Mapped[bool] = mapped_column(Boolean, default=False)
    source_file: Mapped[Optional[str]] = mapped_column(String(300), nullable=True)
    notify_email: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    notify_phone: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)

    # Spam engelleme: en son hangi kategoride bildirim gitti? (None | WARNING_15_DAYS | EXPIRED)
    is_notified: Mapped[bool] = mapped_column(Boolean, default=False)
    last_notified_type: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    last_notified_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    logs: Mapped[list["NotificationLog"]] = relationship(
        back_populates="certificate", cascade="all, delete-orphan", passive_deletes=True
    )


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    certificate_id: Mapped[int] = mapped_column(ForeignKey("certificates.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(20))
    notification_type: Mapped[str] = mapped_column(String(30))
    recipient: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    success: Mapped[bool] = mapped_column(Boolean, default=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    certificate: Mapped[Certificate] = relationship(back_populates="logs")
