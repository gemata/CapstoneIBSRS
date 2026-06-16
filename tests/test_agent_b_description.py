from ibsrs.agents.agent_b_extraction import _assess_description

THRESHOLD = 0.8


def test_truncated_suffix_flags_review():
    conf, review, reasons = _assess_description("WIRE IN - NORTHWIND TRAD...", THRESHOLD)
    assert review is True
    assert "truncated_suffix" in reasons
    assert conf < THRESHOLD


def test_ambiguous_keyword_flags_review():
    conf, review, reasons = _assess_description("UNKNOWN PAYMENT", THRESHOLD)
    assert review is True
    assert "ambiguous_keyword" in reasons


def test_clean_description_not_flagged():
    conf, review, reasons = _assess_description(
        "CUSTOMER PAYMENT - ACME CORP", THRESHOLD)
    assert review is False
    assert reasons == []
    assert conf == 1.0


def test_empty_description_flags_review():
    _, review, reasons = _assess_description("", THRESHOLD)
    assert review is True
    assert "empty_description" in reasons


def test_generic_only_flags_review():
    _, review, reasons = _assess_description("PAYMENT", THRESHOLD)
    assert review is True
    assert "generic_description_only" in reasons
