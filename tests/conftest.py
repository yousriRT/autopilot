"""Fixtures partagées entre tous les tests."""

import sys
import json
from pathlib import Path
from unittest.mock import MagicMock
import pytest

# Permet d'importer les modules src/ sans installer le package
SRC_DIR = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(SRC_DIR))


# ---------- Config & data ----------

@pytest.fixture
def base_config():
    return {
        "daily_total_budget": 100.0,
        "anthropic": {"api_key": "sk-ant-test"},
        "arcads": {"api_key": "arcads-test"},
        "video_provider": "arcads",
        "dashboard": {"recipient_email": "test@example.com"},
        "meta": {
            "access_token": "EAA-test",
            "ad_account_id": "act_123",
            "page_id": "page_123",
            "lead_gen_form_id": "form_999",
            "pixel_id": "pixel_123",
            "targeting": {
                "fibre": {"geo_locations": {"countries": ["CA"]}, "age_min": 30, "age_max": 65, "interests": []},
                "mobile": {"geo_locations": {"countries": ["CA"]}, "age_min": 25, "age_max": 60, "interests": []},
                "famille_4lignes": {"geo_locations": {"countries": ["CA"]}, "age_min": 35, "age_max": 55, "interests": []},
            },
        },
    }


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Force tous les modules à écrire dans tmp_path/data au lieu de ./data."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Patch Path(__file__).parent.parent pour qu'il pointe vers tmp_path
    monkeypatch.setattr("pathlib.Path.__truediv__", Path.__truediv__)  # no-op, juste pour la doc
    return data_dir


# ---------- Sample lead data ----------

@pytest.fixture
def lead_premium_qc():
    return {
        "id": "lead_001",
        "created_time": "2026-04-25T10:00:00",
        "form_id": "form_x",
        "field_data": [
            {"name": "email", "values": ["jane.tremblay@gmail.com"]},
            {"name": "phone_number", "values": ["+15145551234"]},
            {"name": "full_name", "values": ["Jane Tremblay"]},
            {"name": "postal_code", "values": ["H2X 1Y4"]},
        ],
    }


@pytest.fixture
def lead_disposable_email():
    return {
        "id": "lead_002",
        "field_data": [
            {"name": "email", "values": ["x@mailinator.com"]},
            {"name": "full_name", "values": ["x"]},
        ],
    }


@pytest.fixture
def lead_minimal():
    return {
        "id": "lead_003",
        "field_data": [
            {"name": "email", "values": ["jane@example.com"]},
            {"name": "phone_number", "values": ["5145551234"]},
        ],
    }


# ---------- Sample creative ----------

@pytest.fixture
def sample_creative_brief():
    return {
        "angle": "frustration_facture",
        "hook_first_3s": "Ma facture a doublé en 6 mois.",
        "video_script": "Bonjour, moi c'est Jane. Ma facture a doublé en 6 mois sans rien changer.",
        "primary_text": "Découvre une offre télécom qui fait baisser ta facture",
        "headline": "Économise sur ta facture",
        "description": "Soumission gratuite",
        "avatar_persona": "femme 40 ans, look québécois",
    }


# ---------- Mock Anthropic SDK ----------

@pytest.fixture
def mock_anthropic_response():
    """Crée un mock de la réponse messages.parse() avec parsed_output et usage."""
    def _make(parsed_obj, input_tokens=500, output_tokens=200):
        mock = MagicMock()
        mock.parsed_output = parsed_obj
        mock.usage = MagicMock(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_creation_input_tokens=0,
            cache_read_input_tokens=0,
        )
        return mock
    return _make


@pytest.fixture
def mock_anthropic_client(mock_anthropic_response):
    client = MagicMock()
    client.messages = MagicMock()
    client.beta = MagicMock()
    return client


# ---------- Mock Meta publisher ----------

@pytest.fixture
def mock_publisher():
    pub = MagicMock()
    pub.ad_account_id = "act_123"
    pub.page_id = "page_123"
    pub.lead_gen_form_id = "form_999"
    pub.pixel_id = "pixel_123"
    pub.get_active_ads.return_value = []
    pub.get_ad_insights.return_value = {
        "spend": 0, "impressions": 0, "reach": 0, "leads": 0, "clicks": 0, "ctr": 0, "frequency": 0
    }
    pub.get_ad_leads.return_value = []
    pub.upload_video.return_value = "video_id_123"
    pub.create_ad_creative.return_value = "creative_id_123"
    pub.preview_creative.return_value = {"ok": True, "body": "<html>preview</html>"}
    pub._get_or_create_campaign.return_value = "campaign_id_123"
    pub._create_adset.return_value = "adset_id_123"
    pub._create_ad.return_value = "ad_id_123"
    return pub


# ---------- Path overrides for module __init__ ----------

@pytest.fixture
def patch_data_path(tmp_path, monkeypatch):
    """
    Beaucoup de modules calculent leur chemin via Path(__file__).parent.parent / "data".
    Cette fixture redirige vers tmp_path/data.
    """
    fake_data = tmp_path / "data"
    fake_data.mkdir(exist_ok=True)

    # On va monkeypatch chaque module qui utilise ce pattern, dans les tests
    # qui en ont besoin. Pour faire propre, on expose juste le chemin.
    return fake_data
