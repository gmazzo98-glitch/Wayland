"""
Database Session and Engine Management for Project Vienna
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, scoped_session
from models import Base, SourceHealth, IndicatorDefinition
from config import SQLALCHEMY_DATABASE_URI
from indicators import seed_indicator_definitions

# Setup engine with multi-thread safety for Streamlit
connect_args = {"check_same_thread": False} if SQLALCHEMY_DATABASE_URI.startswith("sqlite") else {}
engine = create_engine(SQLALCHEMY_DATABASE_URI, connect_args=connect_args, echo=False)

SessionFactory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
ScopedSession = scoped_session(SessionFactory)

from sqlalchemy import inspect, text

def _migrate_sqlite_schema(target_engine):
    """Automatically adds missing columns to existing SQLite tables if models have changed."""
    inspector = inspect(target_engine)
    with target_engine.begin() as conn:
        for table_name, table in Base.metadata.tables.items():
            if inspector.has_table(table_name):
                existing_cols = {col["name"] for col in inspector.get_columns(table_name)}
                for col in table.columns:
                    if col.name not in existing_cols:
                        col_type = col.type.compile(target_engine.dialect)
                        conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {col.name} {col_type}"))

def get_db_session():
    """Get a database session for query execution."""
    return ScopedSession()

def init_db():
    """Initialize database tables, the indicator catalog, and default source health entries."""
    Base.metadata.create_all(bind=engine)
    _migrate_sqlite_schema(engine)

    db = get_db_session()
    try:
        # Indicator catalog first — SourceHealth rows below are derived from it.
        # Only inserts rows that don't exist yet, so edited weights are never clobbered.
        seed_indicator_definitions(db)

        existing_sources = {s.source_name for s in db.query(SourceHealth).all()}

        sources_to_seed = set()
        for ind in db.query(IndicatorDefinition).filter_by(is_active=True).all():
            if ind.source_system:
                sources_to_seed.add((ind.source_system, ind.phase))

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
