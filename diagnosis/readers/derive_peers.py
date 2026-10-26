"""
Deriver for peer percentiles and group medians (ticket D03).
Registers @deriver("peer_percentiles", after=("financial",)) so it is auto-discovered
and runs after the core financial facts are available.
"""

from typing import Iterable
from diagnosis.contracts import CompanyFacts, Fact
from diagnosis.peers import get_peer_index
from diagnosis.registry import BuildContext, deriver


@deriver("peer_percentiles", after=("financial",))
def derive_peers(facts: CompanyFacts, ctx: BuildContext) -> Iterable[Fact]:
    """
    Derives peer group metadata, medians, and mid-rank percentiles for all PEER_METRICS.
    Uses ctx.peer_index if provided, otherwise loads the cached cohort index.
    """
    peer_idx = ctx.peer_index
    if peer_idx is None:
        peer_idx = get_peer_index()

    return peer_idx.get_company_peer_facts(facts.company_id, facts_in=facts)
