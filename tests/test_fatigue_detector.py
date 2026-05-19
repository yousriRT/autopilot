"""Tests pour FatigueDetector — score 0-100 + flag should_pause."""

import pytest
from datetime import datetime, timedelta
from fatigue_detector import (
    FatigueDetector,
    FREQUENCY_WARNING, FREQUENCY_CRITICAL,
    CTR_DROP_WARNING, CTR_DROP_CRITICAL,
    DAYS_ACTIVE_WARNING, DAYS_ACTIVE_CRITICAL,
    MIN_IMPRESSIONS_TO_TRUST_SIGNAL,
)


@pytest.fixture
def detector():
    return FatigueDetector()


@pytest.fixture
def now():
    return datetime.now()


def make_history(now, days_back_to: int, daily_impressions: int, daily_clicks: int, daily_reach: int):
    """Génère un historique synthétique day-by-day."""
    return [
        {
            "ts": (now - timedelta(days=d)).isoformat(),
            "impressions": daily_impressions,
            "clicks": daily_clicks,
            "reach": daily_reach,
        }
        for d in range(days_back_to, 0, -1)
    ]


class TestFreshAd:
    def test_no_history_no_pause(self, detector, now):
        result = detector.compute_score([], created_at=now.isoformat())
        assert result["score"] == 0
        assert result["level"] == "fresh"
        assert result["should_pause"] is False

    def test_below_min_impressions_fresh(self, detector, now):
        # 100 impressions cumulées < MIN_IMPRESSIONS_TO_TRUST_SIGNAL (1000)
        history = [{"ts": now.isoformat(), "impressions": 100, "clicks": 2, "reach": 80}]
        result = detector.compute_score(history, created_at=now.isoformat())
        assert result["level"] == "fresh"
        assert "pas assez" in result["reasons"][0].lower()


class TestFrequencySignals:
    def test_frequency_below_warning_no_score(self, detector, now):
        # Frequency = 1500/1000 = 1.5, < 4
        history = [{"ts": now.isoformat(), "impressions": 1500, "clicks": 30, "reach": 1000}]
        result = detector.compute_score(history, created_at=now.isoformat())
        # Score peut être >0 si CTR/days mais pas à cause de frequency
        for r in result["reasons"]:
            assert "frequency" not in r.lower() or "élevée" not in r.lower()

    def test_frequency_warning_zone(self, detector, now):
        # frequency = 5.0 (entre 4 et 6)
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 50, "reach": 1000}]
        result = detector.compute_score(history, created_at=now.isoformat())
        assert any("frequency" in r.lower() for r in result["reasons"])
        assert result["score"] > 0

    def test_frequency_critical_pause(self, detector, now):
        # 14 jours de daily 2000 imp / 200 reach → freq cumulée = 28000/2800 = 10
        # +CTR drop semaine 2 (clicks 40 -> 8) +old → score critique → pause
        history = []
        for d in range(14, 7, -1):
            history.append({"ts": (now - timedelta(days=d)).isoformat(),
                           "impressions": 2000, "clicks": 40, "reach": 200})
        for d in range(7, 0, -1):
            history.append({"ts": (now - timedelta(days=d)).isoformat(),
                           "impressions": 2000, "clicks": 8, "reach": 200})
        result = detector.compute_score(history, created_at=(now - timedelta(days=35)).isoformat())
        assert result["frequency"] >= FREQUENCY_CRITICAL
        assert result["should_pause"] is True


class TestCTRMomentum:
    def test_ctr_drop_critical_contributes(self, detector, now):
        # 14 entrées : semaine 1 bon CTR (2%), semaine 2 catastrophique (0.4%)
        history = []
        for d in range(14, 7, -1):
            history.append({
                "ts": (now - timedelta(days=d)).isoformat(),
                "impressions": 800, "clicks": 16, "reach": 400
            })
        for d in range(7, 0, -1):
            history.append({
                "ts": (now - timedelta(days=d)).isoformat(),
                "impressions": 800, "clicks": 3, "reach": 350
            })
        result = detector.compute_score(history, created_at=(now - timedelta(days=20)).isoformat())
        assert result["ctr_momentum"] is not None
        assert result["ctr_momentum"] < CTR_DROP_CRITICAL
        assert any("CTR" in r for r in result["reasons"])

    def test_no_ctr_signal_too_few_entries(self, detector, now):
        history = [
            {"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000},
        ]  # 1 entrée < 4 min
        result = detector.compute_score(history, created_at=now.isoformat())
        assert result["ctr_momentum"] is None

    def test_no_ctr_signal_low_impressions_per_window(self, detector, now):
        # >4 entries mais chaque fenêtre a < 200 impressions
        history = []
        for d in range(14, 0, -1):
            history.append({
                "ts": (now - timedelta(days=d)).isoformat(),
                "impressions": 50, "clicks": 1, "reach": 30
            })
        result = detector.compute_score(history, created_at=now.isoformat())
        # cumulé = 700 < MIN_IMPRESSIONS donc fresh (court-circuit)
        assert result["level"] == "fresh"


class TestDaysActive:
    def test_recent_ad_no_age_score(self, detector, now):
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000}]
        result = detector.compute_score(history, created_at=now.isoformat())
        assert result["days_active"] == 0

    def test_old_ad_age_score(self, detector, now):
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000}]
        result = detector.compute_score(history, created_at=(now - timedelta(days=35)).isoformat())
        assert result["days_active"] >= DAYS_ACTIVE_CRITICAL
        assert any("vol" in r.lower() for r in result["reasons"])

    def test_invalid_created_at_returns_zero_days(self, detector, now):
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000}]
        result = detector.compute_score(history, created_at="not-a-date")
        assert result["days_active"] == 0

    def test_no_created_at_returns_zero_days(self, detector, now):
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000}]
        result = detector.compute_score(history, created_at=None)
        assert result["days_active"] == 0


class TestExtremeCase:
    def test_full_extreme_pause(self, detector, now):
        """Cas extrême : freq>6, CTR drop -75%, age >30j → score ≥ 70 + pause."""
        history = []
        for d in range(14, 7, -1):
            history.append({"ts": (now - timedelta(days=d)).isoformat(),
                           "impressions": 1000, "clicks": 20, "reach": 250})  # freq~4
        for d in range(7, 0, -1):
            history.append({"ts": (now - timedelta(days=d)).isoformat(),
                           "impressions": 2000, "clicks": 8, "reach": 200})  # freq~10, CTR drop
        result = detector.compute_score(history, created_at=(now - timedelta(days=35)).isoformat())
        assert result["score"] >= 70
        assert result["level"] == "critical"
        assert result["should_pause"] is True


class TestParseEdgeCases:
    def test_invalid_ts_in_history_doesnt_crash(self, detector, now):
        history = [
            {"ts": "garbage", "impressions": 5000, "clicks": 100, "reach": 2000},
            {"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 2000},
        ]
        result = detector.compute_score(history, created_at=now.isoformat())
        assert isinstance(result["score"], int)

    def test_zero_reach_no_division_error(self, detector, now):
        history = [{"ts": now.isoformat(), "impressions": 5000, "clicks": 100, "reach": 0}]
        result = detector.compute_score(history, created_at=now.isoformat())
        # Pas de crash, frequency=None
        assert result["frequency"] is None
