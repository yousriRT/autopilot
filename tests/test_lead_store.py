"""Tests pour LeadStore — persistance JSONL des leads."""

import csv
import json
import pytest
from datetime import datetime, timedelta
from lead_store import LeadStore


@pytest.fixture
def store(tmp_path):
    return LeadStore(store_path=tmp_path / "leads.jsonl")


def make_meta_lead(lead_id, email="jane@gmail.com"):
    return {
        "id": lead_id,
        "created_time": "2026-04-25T10:00:00",
        "form_id": "form_x",
        "field_data": [
            {"name": "email", "values": [email]},
            {"name": "phone_number", "values": ["+15145551234"]},
        ],
    }


class TestAdd:
    def test_add_persists(self, store):
        added = store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.85)
        assert added is True
        # Reload fresh instance
        store2 = LeadStore(store_path=store.path)
        assert store2.exists("L1")

    def test_add_idempotent(self, store):
        lead = make_meta_lead("L1")
        store.add(lead, "ad_1", "fibre", 0.85)
        added_again = store.add(lead, "ad_1", "fibre", 0.85)
        assert added_again is False
        # Une seule entrée dans le fichier
        with open(store.path) as f:
            assert sum(1 for _ in f) == 1

    def test_add_without_id_returns_false(self, store):
        added = store.add({"field_data": []}, "ad_1", "fibre", 0.5)
        assert added is False

    def test_add_stores_quality_score_and_metadata(self, store):
        store.add(make_meta_lead("L1"), "ad_xyz", "famille_4lignes", 0.92, funnel_status="received")
        leads = list(store.iter_leads())
        assert leads[0]["ad_id"] == "ad_xyz"
        assert leads[0]["vertical"] == "famille_4lignes"
        assert leads[0]["quality_score"] == 0.92
        assert leads[0]["funnel_status"] == "received"


class TestExists:
    def test_exists_returns_false_when_no_file(self, store):
        assert store.exists("any") is False

    def test_exists_after_add(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        assert store.exists("L1") is True
        assert store.exists("L_other") is False


class TestIterLeads:
    def test_iter_skips_corrupt_lines(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        with open(store.path, "a") as f:
            f.write("not json\n")
        store.add(make_meta_lead("L2"), "ad_2", "mobile", 0.6)
        leads = list(store.iter_leads())
        assert len(leads) == 2

    def test_iter_empty_when_no_file(self, store):
        assert list(store.iter_leads()) == []


class TestListRecent:
    def test_filters_by_hours(self, store):
        # Récent
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        # Vieux : on triche en réécrivant le fichier
        with open(store.path, "a") as f:
            f.write(json.dumps({
                "lead_id": "L_old", "ad_id": "ad_x", "vertical": "fibre",
                "quality_score": 0.5, "funnel_status": "received",
                "received_at": (datetime.now() - timedelta(hours=48)).isoformat(),
                "field_data": [],
            }) + "\n")
        recent = store.list_recent(hours=24)
        ids = [r["lead_id"] for r in recent]
        assert "L1" in ids
        assert "L_old" not in ids

    def test_filters_by_vertical(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        store.add(make_meta_lead("L2"), "ad_2", "mobile", 0.5)
        recent = store.list_recent(hours=24, vertical="fibre")
        assert len(recent) == 1
        assert recent[0]["vertical"] == "fibre"

    def test_invalid_received_at_skipped(self, store):
        with open(store.path, "a") as f:
            f.write(json.dumps({"lead_id": "L_x", "received_at": "garbage"}) + "\n")
        recent = store.list_recent(hours=24)
        assert all(r.get("lead_id") != "L_x" for r in recent)


class TestUpdateFunnelStatus:
    def test_update_changes_status(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        ok = store.update_funnel_status("L1", "contacted", note="appel 1")
        assert ok is True
        leads = list(store.iter_leads())
        assert leads[0]["funnel_status"] == "contacted"
        assert leads[0]["status_history"][0]["from"] == "received"
        assert leads[0]["status_history"][0]["to"] == "contacted"
        assert leads[0]["status_history"][0]["note"] == "appel 1"

    def test_update_unknown_lead_returns_false(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        ok = store.update_funnel_status("L_unknown", "contacted")
        assert ok is False

    def test_update_when_no_file_returns_false(self, store):
        ok = store.update_funnel_status("L_x", "contacted")
        assert ok is False

    def test_status_history_accumulates(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        store.update_funnel_status("L1", "contacted")
        store.update_funnel_status("L1", "converted", note="vente!")
        leads = list(store.iter_leads())
        assert len(leads[0]["status_history"]) == 2
        assert leads[0]["funnel_status"] == "converted"


class TestExportCSV:
    def test_export_writes_csv_with_field_data(self, store, tmp_path):
        store.add(make_meta_lead("L1", email="jane@gmail.com"), "ad_1", "fibre", 0.85)
        store.add(make_meta_lead("L2", email="bob@hotmail.com"), "ad_2", "mobile", 0.50)
        out = tmp_path / "export.csv"
        n = store.export_csv(out, since_hours=24)
        assert n == 2
        with open(out) as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        assert rows[0]["email"] in ("jane@gmail.com", "bob@hotmail.com")
        assert "vertical" in rows[0]
        assert "quality_score" in rows[0]

    def test_export_zero_when_no_recent(self, store, tmp_path):
        out = tmp_path / "export.csv"
        n = store.export_csv(out, since_hours=24)
        assert n == 0
        assert not out.exists()


class TestStats:
    def test_empty_store(self, store):
        s = store.stats()
        assert s["total"] == 0
        assert s["avg_quality"] is None

    def test_stats_aggregates(self, store):
        store.add(make_meta_lead("L1"), "ad_1", "fibre", 0.8)
        store.add(make_meta_lead("L2"), "ad_2", "mobile", 0.4)
        store.add(make_meta_lead("L3"), "ad_3", "fibre", 0.6)
        s = store.stats()
        assert s["total"] == 3
        assert s["by_vertical"]["fibre"] == 2
        assert s["by_vertical"]["mobile"] == 1
        assert s["by_status"]["received"] == 3
        assert s["avg_quality"] == pytest.approx(0.6, rel=1e-2)


class TestPathHandling:
    def test_accepts_string(self, tmp_path):
        s = LeadStore(store_path=str(tmp_path / "leads.jsonl"))
        s.add(make_meta_lead("L1"), "ad_1", "fibre", 0.5)
        assert s.path.exists()
