"""
Database Session and Engine Management for Project Vienna
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Base, SourceHealth
from config import SQLALCHEMY_DATABASE_URI, SIGNAL_METADATA

# Setup engine with multi-thread safety for Streamlit
connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URI.startswith("sqlite") else {}
engine = create_engine(SQLALCHEMY_DATABASE_URI, connect_args=connect_args, echo=False)

SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
ScopedSession = scoped_session(SessionFactory)

def get_db_session():
    """Get a database session for query execution."""
    return ScopedSession()

def init_db():
    """Initialize database tables and default source health entries."""
    Base.metadata.create_all(bind=engine)
    
    db = get_db_session()
    try:
        # Seed SourceHealth entries if missing
        existing_sources = {s.source_name for s in db.query(SourceHealth).all()}
        
        sources_to_seed = set()
        for meta in SIGNAL_METADATA.values():
            sources_to_seed.add((meta["source"], meta["phase"]))
        
        for source_name, phase in sources_to_seed:
            if source_name not in existing_sources:
                db.add(SourceHealth(
                    source_name=source_name,
                    phase=phase,
                    last_status="idle",
                    total_calls=0,
                    total_cost=0.0
                ))
        db.commit()
    except Exception as e:
        db.rollback()
        raise e
    finally:
        db.close()
