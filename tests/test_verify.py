from outreach.verify import check


def row(cid, name, status="PASS", message=True, **kw):
    r = {"channel_id": cid, "name": name, "filter_status": status, "email_status": "not_contacted",
         "dm_status": "not_contacted", "message": {"email": f"Hi {name}, hello", "dm": "d"} if message else None}
    r.update(kw)
    return r


STATS = {"influencers": 3, "qualified": 2, "qualified_with_email": 1, "qualified_with_message": 1, "outreach": {}}
ROWS = [row("A", "Ami"), row("B", "Bea", message=False), row("C", "Cy", status="FAIL", message=False)]


def levels(results):
    return [lvl for lvl, _ in results]


def test_all_checks_pass_for_consistent_data():
    res = check(STATS, ROWS, {"real_sending": False}, total=3, qualified=2, contactable=1)
    assert levels(res) == ["PASS"] * len(res)


def test_wrong_counts_duplicates_and_outreach_on_unqualified_are_failures():
    res = check(STATS, ROWS, {"real_sending": False}, total=391, qualified=53, contactable=24)
    assert levels(res).count("FAIL") == 3
    dup = ROWS + [row("A", "Ami")]
    assert "FAIL" in levels(check({**STATS, "influencers": 4}, dup, {}, total=4, qualified=2, contactable=1))
    bad = [row("A", "Ami"), row("B", "Bea", message=False), row("C", "Cy", status="FAIL", message=False,
                                                                     email_status="simulated")]
    assert "FAIL" in levels(check(STATS, bad, {}, total=3, qualified=2, contactable=1))


def test_mismatched_message_and_real_sending_only_warn():
    rows = [row("A", "Ami", message=True), row("B", "Bea", message=False), row("C", "Cy", status="FAIL", message=False)]
    rows[0]["message"]["email"] = "Hi someone else"
    res = check(STATS, rows, {"real_sending": True}, total=3, qualified=2, contactable=1)
    assert levels(res).count("WARN") == 2 and "FAIL" not in levels(res)
