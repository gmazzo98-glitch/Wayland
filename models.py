"""
SQLAlchemy Data Models for Project Vienna.
Core Entities: Company, SignalRecord, PipelineJob, SourceHealth
"""

import uuid
from datetime import datetime
from sqlalchemy import Column, String, Float, Integer, DateTime, ForeignKey, Text, Boolean, UniqueConstraint, JSON
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

def generate_uuid():
    return str(uuid.uuid4())

SHORTLIST_STATUSES = ("candidate", "shortlisted", "in_pilot", "rejected")


class Company(Base):
    __tablename__ = "companies"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    legal_name = Column(String(255), nullable=False)
    registration_number = Column(String(100), nullable=False, unique=True, index=True) # Handelsregister-Nummer (HRB/HRA)
    nace_code = Column(String(50), default="A01.1") # Sector code (default Agrifood)
    # Text, not String(N): real ATECO/NACE sector descriptions from injected
    # datasets run well past 100 chars (e.g. "Fabbricazione di macchine e
    # apparecchi per le industrie chimiche, petrolchimiche e petrolifere
    # (incluse parti e accessori)" is 133) — SQLite never enforced the old
    # VARCHAR(100) cap, so this only surfaced once real data hit Postgres.
    sector_name = Column(Text, default="Agrifood & Agriculture")
    country = Column(String(100), default="Germany")
    website_url = Column(String(255), nullable=True)
    parent_company_id = Column(String(36), ForeignKey("companies.id"), nullable=True)
    segment = Column(String(50), default="Midcap") # 'Midcap' or 'SME'

    # Denormalized from the number_of_employees signal so segment/headcount
    # filtering doesn't require a join+score computation just to list
    # companies. headcount_source_tier tracks how THIS copy was populated
    # ('T1'/'T2'/'T3'/None), independent of whatever the signal's own tier is.
    headcount = Column(Integer, nullable=True)
    headcount_source_tier = Column(String(5), nullable=True)

    # Gate status — 'candidate' | 'shortlisted' | 'in_pilot' | 'rejected'
    # (see SHORTLIST_STATUSES). Paid ingestion (Phase 3/5) is gated behind
    # 'shortlisted' or 'in_pilot', never 'candidate' — see paid_shortlist_gate.py.
    shortlist_status = Column(String(20), default="candidate", nullable=False)
    shortlisted_at = Column(DateTime, nullable=True)

    # Cached from the last calculate_company_scores() call so list views don't
    # need to recompute for every row just to sort/filter — still always
    # recomputed and overwritten on each score-affecting page render, never
    # hand-edited.
    need_score = Column(Float, nullable=True)
    readiness_score = Column(Float, nullable=True)
    last_scored_at = Column(DateTime, nullable=True)

    # VIENNA readiness-level classification (NOWAY/SKEPTIC/DREAMER/EXPLORER/
    # OPERATOR/ORCHESTRATOR) — deferred per Section 5.4 of the Indicator
    # Prompt: the level-boundary definitions haven't been supplied yet, so
    # nothing writes to this column today. Column exists now so the BASELINE-
    # tagged Prior Open-Innovation Channel Usage signal has somewhere to land
    # once a classifier is built, without another migration.
    vienna_level = Column(String(20), nullable=True)

    # Registry/identity metadata populated by the flexible column-mapping data
    # feeder (company_service.py's apply_data_import) — optional, sourced from
    # whatever dataset the user maps a column onto these targets from. None of
    # these drive scoring; they're descriptive registry fields, same spirit as
    # nace_code/sector_name/website_url above.
    region = Column(String(100), nullable=True)
    province = Column(String(100), nullable=True)
    legal_form = Column(String(100), nullable=True)
    incorporation_date = Column(DateTime, nullable=True)
    registry_status = Column(String(50), nullable=True)  # e.g. Active/Dissolved — distinct from shortlist_status above
    # Passthrough id from whatever source dataset (AIDA company_id, etc.) —
    # provenance only, never used to match/dedup rows (registration_number is).
    external_ref_id = Column(String(100), nullable=True)

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
            "headcount": self.headcount,
            "headcount_source_tier": self.headcount_source_tier,
            "shortlist_status": self.shortlist_status,
            "need_score": self.need_score,
            "readiness_score": self.readiness_score,
            "last_scored_at": self.last_scored_at,
            "vienna_level": self.vienna_level,
            "region": self.region,
            "province": self.province,
            "legal_form": self.legal_form,
            "incorporation_date": self.incorporation_date,
            "registry_status": self.registry_status,
            "external_ref_id": self.external_ref_id,
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

class IndicatorDefinition(Base):
    """
    The full indicator catalog — imported from Indicators.xlsx — and the single
    editable source of truth for scoring. This table IS the "weighting system":
    weight/axis/invert/normalization bounds live here, not in Python, so they can
    be tuned from the Indicator Weights page without a code change or redeploy.

    axis: 'need' | 'readiness' | 'both' | 'context'
      'context' rows are never scored — they're informational tags/moderators
      (e.g. product type, family ownership) that the spreadsheet itself flags as
      not meaningfully scoreable on a need/readiness scale.
    invert: True means a LOW raw value drives the score UP (e.g. thin interest
      cover = high need; low in-house R&D = better GG-fit).
    curve_type: 'linear' (default) maps [raw_min, raw_max] to [0, 100], or
      [raw_max, raw_min] if inverted. 'band' treats [raw_min, raw_max] as a
      sweet spot scoring 100 anywhere inside it, tapering to 0 outside — for
      signals like Total Assets where too little AND too much both score low.
    is_gate / gate_penalty_multiplier: a small number of readiness signals are
      closer to a precondition than a graded score (e.g. "is there a named
      innovation-lead role at all") — when checked-and-unfavorable, they
      multiply the axis score down rather than just contributing their own
      weighted share.

    redundancy_group / automation_tier / axis_modifier: the three fields added
    when reconciling this catalog against gg_indicators.json (see indicators.py's
    module docstring for the reconciliation notes).
      redundancy_group: variables sharing a group measure overlapping
        underlying constructs (e.g. Materials/Labour/Logistics/Energy/COGS are
        all "cost pressure") and must not each count at full weight in the
        same axis — scoring.py dampens same-group contributions within a
        single axis evaluation (full weight to the highest-weighted member,
        half to the next, a quarter after that, and so on). None/blank means
        "stands alone, never dampened."
      automation_tier: 'T1' (structured register/API), 'T2' (scrape + extract,
        carries a confidence), or 'T3' (not observable pre-contact — manual
        entry only). Distinct from `phase`, which is this codebase's own
        finer-grained build-order/pipeline-trigger grouping and is what
        actually drives ingestion — automation_tier is the coarser label from
        the source spreadsheet, kept for traceability and display.
      axis_modifier: free-text tag carried from the source data ('', NEGATIVE,
        CAVEAT, GATING, BASELINE, GG-FIT GAP, MODERATOR, or the source's own
        'READINESS CAVEAT' on Debt/Leverage) — informational/display only.
        Where a modifier implies real scoring behavior (NEGATIVE, GG-FIT GAP
        -> invert=True; GATING -> is_gate=True), that behavior is already
        encoded via invert/is_gate above; this field does not independently
        drive scoring, so editing it alone from the Indicator Weights page has
        no numeric effect — it exists so the UI can show *why* a row behaves
        the way it does without re-deriving it from invert/is_gate/weight.
    """
    __tablename__ = "indicator_definitions"

    key = Column(String(100), primary_key=True)
    label = Column(String(255), nullable=False)
    category = Column(String(100), nullable=False, index=True)
    axis = Column(String(20), nullable=False, default="readiness")
    invert = Column(Boolean, default=False, nullable=False)
    curve_type = Column(String(20), default="linear", nullable=False)
    raw_min = Column(Float, nullable=True)
    raw_max = Column(Float, nullable=True)
    weight = Column(Float, default=3.0, nullable=False)  # editable importance, 0-5
    is_gate = Column(Boolean, default=False, nullable=False)
    gate_penalty_multiplier = Column(Float, default=1.0, nullable=False)
    freshness_days = Column(Integer, default=365, nullable=False)
    cost_per_pull = Column(Float, default=0.0, nullable=False)
    phase = Column(Integer, default=1, nullable=False)
    source_system = Column(String(100), nullable=True)  # short pipeline-grouping tag
    redundancy_group = Column(String(50), nullable=True, index=True)
    automation_tier = Column(String(5), nullable=True)  # 'T1' | 'T2' | 'T3'
    axis_modifier = Column(String(30), default="", nullable=False)
    proxy = Column(Text, nullable=True)
    rationale = Column(Text, nullable=True)
    comment = Column(Text, nullable=True)
    source_description = Column(Text, nullable=True)
    example_status = Column(String(150), nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


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


class PilotOutcome(Base):
    """
    The calibration loop's raw material (Section 6 of the Indicator Prompt).
    None of indicators.py's weights are validated yet — GG hasn't run a pilot.
    This table exists so that plumbing is ready the moment real outcomes
    start arriving, without a schema change at that point.

    Deliberately NOT a live "current score" join: need_score_at_start /
    readiness_score_at_start / signal_snapshot_json are a frozen copy taken
    when the pilot began, because Company.need_score keeps changing as more
    signals get checked after that date — joining PilotOutcome to the live
    score would silently compare "how the company looked at pilot start" against
    "how it happens to look today," which is a different, invalid question.

    calibration.py reads this table (joined on company_id for context only,
    never for the score itself) and reports simple directional stats — which
    variables' snapshotted values differed most between successful and
    unsuccessful pilots. It does not, and per Section 6 should not, fit any
    predictive model: with single-digit outcome counts there's no valid
    statistical basis for one. That's a manual, human-reviewed step until
    there are roughly 10-15 scored pilots.
    """
    __tablename__ = "pilot_outcomes"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    pilot_label = Column(String(255), nullable=False)  # short human name, e.g. "AgriTech Innovationen - Q1 2027 pricing pilot"

    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    ended_at = Column(DateTime, nullable=True)

    # Frozen at pilot start — see class docstring for why this can't be a join.
    need_score_at_start = Column(Float, nullable=True)
    readiness_score_at_start = Column(Float, nullable=True)
    completeness_pct_at_start = Column(Float, nullable=True)
    signal_snapshot_json = Column(Text, nullable=True)  # {signal_key: {value, status, normalized}} at started_at

    # Outcome, filled in once the pilot concludes.
    outcome_success = Column(Boolean, nullable=True)  # null until scored
    outcome_metric = Column(Float, nullable=True)  # e.g. % forecast-accuracy improvement — metric definition is per-pilot, documented in notes
    scored_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)

    company = relationship("Company")

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class RawImportRecord(Base):
    """
    The complete original row exactly as injected via the flexible data
    feeder, kept per company per dataset — every column from the source
    file, not just the ones mapped onto an indicator/field at import time.

    This is the "structured vs. blob, twofold" storage the user asked for:
    company_service.apply_data_import() writes the interpreted subset into
    SignalRecord/Company columns for day-to-day use (the structured side),
    and writes the untouched row here (the blob side) so a column that
    wasn't mapped to anything yet — or a new indicator idea entirely — can
    be applied retroactively later without re-uploading the source file.

    One row per (company, dataset_name): re-uploading the same dataset
    overwrites this row (matching the structured side's conflict/overwrite
    rule), a different dataset_name for the same company gets its own row.
    raw_row/mapping_snapshot use JSON (JSONB on Postgres/Supabase, plain
    TEXT-backed JSON on SQLite) so the blob stays queryable, not just an
    opaque string.
    """
    __tablename__ = "raw_import_records"
    __table_args__ = (UniqueConstraint("company_id", "dataset_name", name="uq_raw_import_company_dataset"),)

    id = Column(String(36), primary_key=True, default=generate_uuid)
    company_id = Column(String(36), ForeignKey("companies.id"), nullable=False, index=True)
    dataset_name = Column(String(150), nullable=False, index=True)
    source_filename = Column(String(255), nullable=True)
    source_row_index = Column(Integer, nullable=True)
    raw_row = Column(JSON, nullable=False)          # {original_column_name: raw_value}, every column
    mapping_snapshot = Column(JSON, nullable=True)  # {group_base: target_key} in effect for this import
    imported_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    company = relationship("Company", backref="raw_import_records")

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}


class ColumnMappingProfile(Base):
    """
    A saved column->field/indicator mapping for the flexible data feeder
    (company_service.py's detect_column_groups/apply_data_import). Lets a
    recurring dataset shape (e.g. "AIDA Financials") be re-uploaded without
    re-doing the mapping step from scratch — the saved assignments pre-fill
    the mapping UI for review, they're never silently re-applied.

    dataset_name is also the conflict-scoping key: apply_data_import tags
    every SignalRecord it writes with source=dataset_name, and treats a
    company that already has a present signal under that same source as a
    conflict (needs an explicit overwrite), while a different dataset_name
    for the same company just merges in. See company_service.py.
    """
    __tablename__ = "column_mapping_profiles"

    id = Column(String(36), primary_key=True, default=generate_uuid)
    dataset_name = Column(String(150), nullable=False, unique=True, index=True)
    country = Column(String(50), nullable=False, default="Germany")
    # {group_base_name: target_key} — target_key is a Company field name
    # (e.g. "legal_name"), an IndicatorDefinition key, or None (ignored).
    mapping_json = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def to_dict(self):
        return {c.name: getattr(self, c.name) for c in self.__table__.columns}
