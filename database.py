from sqlalchemy import create_engine, Column, Integer, String, DateTime, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base
from datetime import datetime

DB_URL = "sqlite:///./parking_enterprise.db"
engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class AccessList(Base):
    __tablename__ = "access_list"
    id = Column(Integer, primary_key=True)
    plate_number = Column(String, unique=True, index=True)
    category = Column(String, default="white") # white, black, temp, guest
    phone = Column(String, nullable=True)
    note = Column(String, nullable=True) # Причина бана или ФИО

class ParkingLog(Base):
    __tablename__ = "parking_log"
    id = Column(Integer, primary_key=True)
    plate_number = Column(String, index=True)
    entry_time = Column(DateTime, default=datetime.now)
    exit_time = Column(DateTime, nullable=True)
    gate_id = Column(String)
    direction = Column(String)
    is_confirmed = Column(Boolean, default=False)
    violation_type = Column(String, nullable=True) # 'overstay', 'tailgate'

def init_db():
    Base.metadata.create_all(bind=engine)