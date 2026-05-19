"""Tests pour LeadQualityScorer — heuristique de scoring 0..1."""

import pytest
from lead_quality import LeadQualityScorer, TRUSTED_EMAIL_DOMAINS, DISPOSABLE_DOMAINS, QUEBEC_AREA_CODES


class TestHeuristicScoring:
    def test_premium_qc_lead_scores_max(self, lead_premium_qc):
        s = LeadQualityScorer()
        score = s.score(lead_premium_qc)
        assert score >= 0.95, f"Premium QC lead should score near 1.0, got {score}"

    def test_disposable_email_scores_min(self, lead_disposable_email):
        s = LeadQualityScorer()
        score = s.score(lead_disposable_email)
        assert score <= 0.10, f"Disposable email should score near 0, got {score}"

    def test_minimal_lead_mid_range(self, lead_minimal):
        s = LeadQualityScorer()
        score = s.score(lead_minimal)
        # email jane@example.com (domaine inconnu mais plausible +0.05) + tel QC 514 (+0.25) - no name (-0.15)
        # baseline 0.5 + 0.05 + 0.25 - 0.15 = 0.65
        assert 0.55 <= score <= 0.75

    def test_no_email_penalized(self):
        s = LeadQualityScorer()
        score = s.score({"id": "x", "field_data": []})
        # baseline 0.5 - 0.3 (no email) = 0.2
        assert score <= 0.25

    def test_unknown_domain_neutral(self):
        s = LeadQualityScorer()
        lead = {"id": "x", "field_data": [
            {"name": "email", "values": ["jean@plomberie-pro.qc.ca"]}
        ]}
        score = s.score(lead)
        # baseline 0.5 + 0.05 (domaine plausible) - 0.15 (nom court/absent)
        # On vérifie juste qu'on est dans une zone neutre, pas pénalisé
        assert 0.30 <= score <= 0.65

    def test_weird_email_penalized(self):
        s = LeadQualityScorer()
        lead = {"id": "x", "field_data": [
            {"name": "email", "values": ["bizarre@qq"]}
        ]}
        score = s.score(lead)
        # domaine sans point = -0.2
        assert score < 0.45

    @pytest.mark.parametrize("phone,min_score,max_score", [
        # baseline 0.5 - 0.3 (no email) - 0.15 (no name) = 0.05
        # + bonus phone (variable selon phone)
        ("+1 514-555-1234", 0.25, 0.40),  # +0.25 (10dig + QC) → ~0.30
        ("+1 416-555-1234", 0.15, 0.30),  # +0.15 (10dig, non-QC) → ~0.20
        ("5145551234", 0.25, 0.40),       # +0.25 → ~0.30
        ("123", 0.0, 0.20),               # -0.10 → ~0.05 (cap à 0)
    ])
    def test_phone_scoring(self, phone, min_score, max_score):
        s = LeadQualityScorer()
        lead = {"id": "x", "field_data": [
            {"name": "phone_number", "values": [phone]}
        ]}
        score = s.score(lead)
        assert min_score <= score <= max_score, (
            f"phone={phone!r}: score={score} hors range [{min_score}, {max_score}]"
        )

    def test_qc_postal_code_bonus(self):
        s = LeadQualityScorer()
        # baseline 0.5 + 0.15 (gmail) - 0.15 (no name) + 0.10 (QC postal) = 0.60
        lead_with_qc = {"id": "x", "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "postal_code", "values": ["H2X1Y4"]},
        ]}
        # baseline 0.5 + 0.15 (gmail) - 0.15 (no name) = 0.50
        lead_without = {"id": "y", "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
        ]}
        # Le delta est ce qu'on teste
        assert s.score(lead_with_qc) > s.score(lead_without)
        assert s.score(lead_with_qc) - s.score(lead_without) == pytest.approx(0.10, abs=0.01)

    def test_non_qc_postal_no_bonus(self):
        s = LeadQualityScorer()
        lead_qc = {"id": "x", "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "postal_code", "values": ["H2X 1Y4"]},
        ]}
        lead_on = {"id": "y", "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "postal_code", "values": ["M5V 3A8"]},  # Toronto
        ]}
        # Le QC doit avoir +0.10 vs Ontario (pas de bonus)
        assert s.score(lead_qc) > s.score(lead_on)

    def test_score_clamped_to_0_1(self):
        s = LeadQualityScorer()
        # Lead "trop bon" pour vérifier le cap
        lead = {"id": "x", "field_data": [
            {"name": "email", "values": ["jane@gmail.com"]},
            {"name": "phone_number", "values": ["+15145551234"]},
            {"name": "full_name", "values": ["Jane Tremblay"]},
            {"name": "postal_code", "values": ["H2X 1Y4"]},
        ]}
        assert 0.0 <= s.score(lead) <= 1.0


class TestExternalScorer:
    def test_external_scorer_called(self, lead_minimal):
        external = lambda lead: 0.42
        s = LeadQualityScorer(external_scorer=external)
        assert s.score(lead_minimal) == 0.42

    def test_external_scorer_failure_falls_back(self, lead_minimal):
        def broken(lead):
            raise RuntimeError("CRM down")
        s = LeadQualityScorer(external_scorer=broken)
        score = s.score(lead_minimal)
        # Fallback heuristique → score raisonnable
        assert 0.0 <= score <= 1.0

    def test_external_scorer_clamped(self, lead_minimal):
        s = LeadQualityScorer(external_scorer=lambda l: 5.0)  # > 1
        assert s.score(lead_minimal) == 1.0
        s2 = LeadQualityScorer(external_scorer=lambda l: -1.0)  # < 0
        assert s2.score(lead_minimal) == 0.0


class TestAggregation:
    def test_aggregate_quality_ratio(self):
        s = LeadQualityScorer()
        assert s.aggregate_quality_ratio([0.9, 0.8, 0.7]) == pytest.approx(0.8)

    def test_aggregate_empty_returns_zero(self):
        s = LeadQualityScorer()
        assert s.aggregate_quality_ratio([]) == 0.0


class TestConstants:
    def test_qc_area_codes_are_strings(self):
        assert all(isinstance(c, str) and len(c) == 3 for c in QUEBEC_AREA_CODES)

    def test_disposable_excludes_trusted(self):
        # Sanity : pas de chevauchement
        assert not (TRUSTED_EMAIL_DOMAINS & DISPOSABLE_DOMAINS)

    def test_telus_not_in_trusted_domains(self):
        # Mémoire : aucune trace de telus dans le code (sauf blacklists explicites)
        assert "telus.net" not in TRUSTED_EMAIL_DOMAINS
