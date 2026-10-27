"""
The deep-site collector must work on companies nobody wrote a special case for (ticket I99): addresses are found by
their SHAPE (street, postcode, locality, province), timeline sentences by generic corporate-event words plus a year.
Every town and company below is invented or absent from every golden file on purpose.
"""

from intel.collectors.deep_site import _extract_page_facts
from intel.fetch import Page


def facts_of(text):
    results = []
    _extract_page_facts(Page(url="https://www.example-unseen.it/azienda", ok=True, status=200, text=text), "example-unseen.it", results)
    return {f.key: f for f in results}


def test_registered_office_is_found_by_shape_in_towns_no_list_ever_named():
    for text, expected in (
        ("Sede legale: Via dei Tigli 4, 31044 Montebelluna (TV), Italia", "31044 Montebelluna (TV)"),
        ("Registered office: Strada Provinciale 12, 12051 Alba (CN)\nPhone +39 0173 000000", "12051 Alba (CN)"),
        ("Headquarters: Via Verdi 7, 35010 Villa del Conte (PD)", "35010 Villa del Conte (PD)"),
    ):
        fact = facts_of(text + "\nThe company designs and builds machines for packaging lines.").get("identity.registered_office")
        assert fact is not None, text
        assert expected in fact.value, (expected, fact.value)
        assert " ".join(fact.evidence[0].quote.split()) in " ".join(text.split())


def test_no_postcode_no_registered_office():
    assert "identity.registered_office" not in facts_of("We are located near the motorway exit, ask for directions.")


def test_timeline_uses_generic_event_words_and_requires_a_year():
    got = facts_of("In 2015 we opened a subsidiary in Brazil. Our team loves coffee.\nFounded in 1987 by two engineers.")
    assert "events.timeline" in got
    assert "subsidiary" in got["events.timeline"].value.lower() or "founded" in got["events.timeline"].value.lower()
    assert "events.timeline" not in facts_of("We have a branch in Dubai and a team in Pune, ask us anything.")   # no year, no event


def test_html_history_timeline_with_the_year_on_its_own_line():
    """The usual company-history layout: year alone on a line, the entry on the next lines (invented company)."""
    page = ("Our history\n1998\nFounded in Treviso by two brothers.\n2008\nZeta GmbH\n"
            "Opened a subsidiary, Zeta GmbH, in Stuttgart.\n2019\nSecond plant\nWe moved the foundry to a new site in Asolo.")
    timeline = facts_of(page)["events.timeline"]
    assert "Zeta GmbH" in timeline.value and "Stuttgart" in timeline.value and "Asolo" in timeline.value
    assert " ".join(timeline.evidence[0].quote.split()) in " ".join(page.split())
