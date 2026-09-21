"""
Tables behind the valuation feature (valuation.py / valuation_data.py).

Kept in their own module rather than models.py so the valuation work stays self-contained;
anything that imports valuation_data (the app does, through views/valuation.py) registers
these on the shared Base, and database.init_db's create_all then creates them.
"""

from datetime import datetime

from sqlalchemy import Column, String, Float, DateTime, Text, JSON

from models import Base


class ValuationReference(Base):
    """
    One published reference dataset, stored exactly as parsed from its source — never edited
    by hand, only replaced by a refresh. Today these are Prof. Damodaran's free industry
    datasets (multiples, cost of capital, working capital) and his country-risk workbook.

    name: 'multiples' | 'wacc' | 'working_capital' | 'country_risk'
    as_of is the date the PUBLISHER stamped on the file (not when we fetched it), so the
    Valuation page can show how old the market data behind a number really is.
    """
    __tablename__ = "valuation_reference"

    name = Column(String(60), primary_key=True)
    region = Column(String(40), nullable=True)
    as_of = Column(String(20), nullable=True)
    source_url = Column(Text, nullable=True)
    fetched_at = Column(DateTime, default=datetime.utcnow)
    payload = Column(JSON, nullable=False)


class ValuationAssumption(Base):
    """
    An editable modelling assumption — the valuation counterpart of an IndicatorDefinition
    row. Every number the engine uses that is NOT a reported company figure or a published
    reference value lives here, with the reasoning and the source (or the honest statement
    that it is a judgment call) next to it.

    scope: '*' for the global default, or a country name ('Italy') / dataset name to override
    it for that scope only. Lookup order is most-specific first (see valuation_data.Assumptions).
    """
    __tablename__ = "valuation_assumptions"

    key = Column(String(80), primary_key=True)
    scope = Column(String(80), primary_key=True, default="*")
    value_num = Column(Float, nullable=True)
    value_text = Column(Text, nullable=True)
    label = Column(String(200), nullable=True)
    unit = Column(String(20), nullable=True)
    rationale = Column(Text, nullable=True)
    source = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ValuationSectorMap(Base):
    """
    NACE/ATECO code prefix -> the Damodaran industry whose multiples and cost of capital
    stand in for that sector. Longest matching prefix wins. A company whose code matches
    nothing is reported as "no sector reference" rather than valued against a guessed peer
    group. `note` starting with "approx" marks a fit that is only roughly right.
    """
    __tablename__ = "valuation_sector_map"

    nace_prefix = Column(String(12), primary_key=True)
    industry = Column(String(80), nullable=False)
    note = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
