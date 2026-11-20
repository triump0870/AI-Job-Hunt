"""Tailor / Approvals filtering: fit ≤ alongside fit ≥, and per-status counts for the status tabs."""
from app.workspace import APPROVAL_STATUS_FILTERS, filter_packet_rows


def row(i, fit, status="tailored"):
    return {"id": i, "title": f"Role {i}", "company": "Acme", "location": "Remote", "fit": fit, "reviewer_score": 80,
            "board": "Naukri", "status": status, "starred": False, "waiting": status == "tailored",
            "discovered_at": f"2026-11-{i:02d}"}


ROWS = [row(1, 85), row(2, 60), row(3, 40, "approved"), row(4, None)]


def run(**kw):
    args = {"q": "", "min_fit": 0, "min_review": 0, "board": "all", "status": "all", "sort": "",
            "status_filters": APPROVAL_STATUS_FILTERS, "with_facets": True} | kw
    return filter_packet_rows(ROWS, **args)


def test_max_fit_is_inclusive_and_leaves_out_unranked_jobs():
    assert [r["id"] for r in run(max_fit=60)["items"]] == [2, 3]
    assert [r["id"] for r in run(min_fit=50, max_fit=60)["items"]] == [2]


def test_status_counts_follow_the_other_filters_but_not_the_status_itself():
    out = run(max_fit=60, status="approved")
    assert [r["id"] for r in out["items"]] == [3]
    assert out["status_counts"]["all"] == 2
    assert out["status_counts"]["approved"] == 1 and out["status_counts"]["waiting"] == 1


def test_max_review_is_inclusive_and_leaves_out_unreviewed_packets():
    rows = [row(1, 80) | {"reviewer_score": 90}, row(2, 80) | {"reviewer_score": 70}, row(3, 80) | {"reviewer_score": None}]
    out = filter_packet_rows(rows, q="", min_fit=0, min_review=60, max_review=70, board="all", status="all", sort="",
                             status_filters=APPROVAL_STATUS_FILTERS, with_facets=False)
    assert [r["id"] for r in out] == [2]


def test_a_work_mode_prefix_is_not_a_place():
    from app.services.locations import location_key
    assert location_key("Hybrid - Bengaluru") == location_key("Bengaluru, Karnataka") == "Bengaluru"
    assert location_key("On-site: Pune, India") == "Pune" and location_key("Hybrid - Remote") == "Remote"
