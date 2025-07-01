import datetime

from sqlalchemy import JSON, Column, DateTime, Integer, String

from app.db.session import Base


class Audit(Base):
    __tablename__ = "audits"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, index=True, nullable=False)
    status = Column(String, default="PENDING", nullable=False)
    report_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    # New fields to associate the audit with the requesting user/dashboard
    user_id = Column(String, nullable=True, index=True)
    user_audit_report_request_id = Column(String, nullable=True, index=True)

    # Error handling fields
    error_message = Column(String, nullable=True)  # User-friendly error message
    technical_error = Column(String, nullable=True)  # Technical error details

    def __repr__(self):
        return f"<Audit(id={self.id}, url='{self.url}', status='{self.status}')>"
