from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.ext.declarative import declarative_base
from app.core.config import settings

# Get the database URL from our validated Pydantic settings
DATABASE_URL = settings.DATABASE_URL

# The application will fail to start if this is not set, which is the desired behavior.
if not DATABASE_URL:
    raise ValueError("FATAL: DATABASE_URL environment variable is not set.")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close() 