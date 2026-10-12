from people_sync import google_cleanup


def test_the_company_push_google_writes_is_neither_cleared_nor_a_gap():
    groups = {"contactGroups/1": "Springfield"}
    raw = {
        "labels": [{"contactGroupMembership": {"contactGroupResourceName": "contactGroups/1"}}],
        "org": [{"name": "Springfield, Lakeside", "metadata": {"primary": True}}],
    }
    labels, orgs, gaps = google_cleanup.decide(raw, ["Springfield", "Lakeside"], [], groups)
    assert gaps == [] and orgs == []
    assert labels == [("contactGroups/1", "Springfield")]


def test_selftest_still_holds():
    google_cleanup.selftest()
