"""
Database models for the peer engine (ticket D03).
Defines dx_peer_stats on the shared models.Base.
Auto-imported by diagnosis/models.py during database.init_db().
"""

from datetime import datetime, timezone
from sqlalchemy import Column, DateTime, Float, Integer, JSON, String
from models import Base


class DxPeerStats(Base):
    """
    Cached group statistics and distributions for peer benchmarking.
    Enables instant lookup of peer medians and percentiles during portfolio runs.
    """
    __tablename__ = "dx_peer_stats"

    id = Column(String(100), primary_key=True)  # e.g. "{group_level}:{group_code}:{metric}"
    group_code = Column(String(32), nullable=False, index=True)
    group_level = Column(String(32), nullable=False, index=True)  # "6-digit", "5-digit", "4-digit", "3-digit", "2-digit", "all"
    metric = Column(String(64), nullable=False, index=True)
    peer_count = Column(Integer, nullable=False, default=0)
    median = Column(Float, nullable=True)
    p10 = Column(Float, nullable=True)
    p25 = Column(Float, nullable=True)
    p75 = Column(Float, nullable=True)
    p90 = Column(Float, nullable=True)
    mean = Column(Float, nullable=True)
    std = Column(Float, nullable=True)
    min_value = Column(Float, nullable=True)
    max_value = Column(Float, nullable=True)
    distribution_json = Column(JSON, nullable=True)
    computed_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    schema_version = Column(Integer, nullable=False, default=1)

    def to_dict(self):
        return {
            "id": self.id,
            "group_code": self.group_code,
            "group_level": self.group_level,
            "metric": self.metric,
            "peer_count": self.peer_count,
            "median": self.median,
            "p10": self.p10,
            "p25": self.p25,
            "p75": self.p75,
            "p90": self.p90,
            "mean": self.mean,
            "std": self.std,
            "min_value": self.min_value,
            "max_value": self.max_value,
            "distribution_json": self.distribution_json,
            "computed_at": self.computed_at.isoformat() if self.computed_at else None,
            "schema_version": self.schema_version,
        }
