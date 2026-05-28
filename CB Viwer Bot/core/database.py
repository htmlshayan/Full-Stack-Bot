import os
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import Column, String, Text, Boolean, text

# Use SQLite by default for localhost, can be overridden by DATABASE_URL env var
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/bot.db")
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://")

engine = create_async_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
)

SessionLocal = async_sessionmaker(autocommit=False, autoflush=False, bind=engine, class_=AsyncSession)

class Base(DeclarativeBase):
    pass

class AccountModel(Base):
    __tablename__ = "accounts"
    
    id = Column(String, primary_key=True, index=True)
    username = Column(String, index=True)
    password = Column(String)
    proxies = Column(Text, nullable=True)
    cookies = Column(Text, nullable=True)
    enabled = Column(Boolean, default=True)

class SettingsModel(Base):
    __tablename__ = "settings"
    
    key = Column(String, primary_key=True)
    value = Column(Text)

class EmployeeModel(Base):
    __tablename__ = "employees"

    id = Column(String, primary_key=True, index=True)
    username = Column(String, index=True, unique=True)
    password_hash = Column(String)
    role = Column(String, default="employee")

class TargetModel(Base):
    __tablename__ = "targets"
    
    id = Column(String, primary_key=True, index=True)
    username = Column(String, index=True)
    description = Column(String, nullable=True)
    enabled = Column(Boolean, default=True)

async def init_db():
    # Ensure data directory exists
    os.makedirs("data", exist_ok=True)
    
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in DATABASE_URL:
            result = await conn.execute(text("PRAGMA table_info(accounts)"))
            columns = [row[1] for row in result]
            if "enabled" not in columns:
                await conn.execute(text("ALTER TABLE accounts ADD COLUMN enabled BOOLEAN DEFAULT 1"))
            result = await conn.execute(text("PRAGMA table_info(targets)"))
            columns = [row[1] for row in result]
            if "enabled" not in columns:
                await conn.execute(text("ALTER TABLE targets ADD COLUMN enabled BOOLEAN DEFAULT 1"))

async def get_db():
    async with SessionLocal() as session:
        yield session
