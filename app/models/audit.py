import datetime
from sqlalchemy import Column, Integer, String, DateTime, JSON
from app.db.session import Base

class Audit(Base):
    __tablename__ = "audits"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, index=True, nullable=False)
    status = Column(String, default='PENDING', nullable=False)
    report_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<Audit(id={self.id}, url='{self.url}', status='{self.status}')>" 