"""
SQLAlchemy Data Models for Project Vienna.
Core Entities: Company, SignalRecord, PipelineJob, SourceHealth
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def generate_uuid():
    return str(uuid.uuid4())

class Company(Base):
    __tablename__ = "companies"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    legal_name = Column(String(255), nullable=False)
    registration_number = Column(String(100), nullable=False, unique=True, index=True) # Handelsregister-Nummer (HRB/HRA)
    nace_code = Column(String(50), default="A01.1") # Sector code (default Agrifood)
    sector_name = Column(String(100), default="Agrifood & Agriculture")
    country = Column(String(100), default="Germany")
    website_url = Column(String(255), nullable=True)
    parent_company_id = Column(String(36), ForeignKey("companies.id"), nullable=True)
    segment = Column(String(50), default="Midcap") # 'Midcap' or 'SME'
    
    # Gate status
    is_shortlisted = Column(Boolean, default=False)
    shortlisted_at = Column(DateTime, nullable=True)

    # Relationships
    signals = relationship("SignalRecord", back_populates="company", cascade="all, delete-orphan")
    parent = relationship("Company", remote_side=[id], backref="subsidiaries")

    def to_dict(self):
        return {
            "id": self.id,
            "legal_name": self.legal_name,
            "registration_number": self.registration_number,
            "nace_code": self.nace_code,
            "sector_name": self.sector_name,
            "country": self.country,
            "website_url": self.website_url,
            "segment": self.segment,
            "is_shortlisted": self.is_shortlisted
        }

class SignalRecord(Base):
    """
    Every signal carries a tri-state status:
    - 'present': Value populated from external source
    - 'absent': Actively checked, confirmed no record exists (e.g. 0 patents)
    - 'not_yet_checked': Pipeline has not run this source yet
    - 'stale': Past freshness window, needs re-fetch flag
    """
    __tablename__ = "signal_records"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    signal_key = Column(String(100), nullable=False, index=True)
    source = Column(String(100), nullable=False)
    
    # Values
    numeric_value = Column(Float, nullable=True)
    text_value = Column(Text, nullable=True)
    
    # Tri-State Status
    status = Column(String(30), nullable=False, default="not_yet_checked") # present | absent | not_yet_checked | stale
    confidence = Column(Float, default=1.0)

    # Provenance: real fetch from the live source vs. a clearly-labeled fallback value
    # used when credentials/network aren't available. Never let simulated data render
    # identically to a real pull — see Section 1 of the Technical Brief.
    is_simulated = Column(Boolean, default=True, nullable=False)

    fetched_at = Column(DateTime, default=datetime.utcnow)
    raw_payload_ref = Column(Text, nullable=True) # Pointer or JSON dump

    company = relationship("Company", back_populates="signals")

    __table_args__ = (
        UniqueConstraint('company_id', 'signal_key', name='_company_signal_uc'),
    )

class PipelineJob(Base):
    """
    Tracks pipeline execution runs, queue status, rate limits, and failure logs per source.
    """
    __tablename__ = "pipeline_jobs"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    source_name = Column(String(100), nullable=False, index=True)
    phase = Column(Integer, nullable=False)
    status = Column(String(30), default="pending") # pending | running | completed | failed | gated
    
    records_processed = Column(Integer, default=0)
    records_updated = Column(Integer, default=0)
    call_count = Column(Integer, default=0)
    cost_incurred = Column(Float, default=0.0)
    
    started_at = Column(DateTime, default=datetime.utcnow)
    finished_at = Column(DateTime, nullable=True)
    error_log = Column(Text, nullable=True)

class SourceHealth(Base):
    """
    Operational control metrics per data source.
    """
    __tablename__ = "source_health"

    source_name = Column(String(100), primary_key=True)
    phase = Column(Integer, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    last_status = Column(String(30), default="idle")
    # 'live' once a real call has succeeded against the source at least once this run,
    # 'simulated' whenever credentials are missing or the real call failed and a
    # fallback value was used instead. Distinct from last_status (which is the run outcome).
    mode = Column(String(20), default="simulated")
    total_calls = Column(Integer, default=0)
    total_cost = Column(Float, default=0.0)
    error_count = Column(Integer, default=0)
    last_error_message = Column(Text, nullable=True)
