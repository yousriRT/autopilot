"""Tests pour CostTracker — pricing Anthropic + OpenAI image + plafond."""

import json
import pytest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from cost_tracker import CostTracker, ANTHROPIC_PRICING, OPENAI_IMAGE_USD_DEFAULT


@pytest.fixture
def tracker(tmp_path, base_config):
    return CostTracker(base_config, log_path=tmp_path / "costs.jsonl")


def make_usage(input_tokens=0, output_tokens=0, cache_creation=0, cache_read=0):
    """Helper qui mime response.usage côté Anthropic SDK (Pydantic-like)."""
    u = MagicMock(spec=["input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"])
    u.input_tokens = input_tokens
    u.output_tokens = output_tokens
    u.cache_creation_input_tokens = cache_creation
    u.cache_read_input_tokens = cache_read
    return u


class TestAnthropicPricing:
    def test_opus_4_7_basic_cost(self, tracker):
        # 1000 input + 500 output @ Opus 4.7: 1000/1M*$5 + 500/1M*$25 = 0.005 + 0.0125 = 0.0175
        cost = tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 500))
        assert cost == pytest.approx(0.0175, rel=1e-3)

    def test_sonnet_4_6_cheaper_than_opus(self, tracker):
        cost_opus = tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 500))
        cost_sonnet = tracker.record_anthropic("claude-sonnet-4-6", make_usage(1000, 500))
        assert cost_sonnet < cost_opus
        # Sonnet : 1000/1M*$3 + 500/1M*$15 = 0.003 + 0.0075 = 0.0105
        assert cost_sonnet == pytest.approx(0.0105, rel=1e-3)

    def test_haiku_cheapest(self, tracker):
        cost = tracker.record_anthropic("claude-haiku-4-5", make_usage(1000, 500))
        # Haiku : 1000/1M*$1 + 500/1M*$5 = 0.001 + 0.0025 = 0.0035
        assert cost == pytest.approx(0.0035, rel=1e-3)

    def test_cache_creation_costs_1_25x(self, tracker):
        cost_no_cache = tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 0))
        cost_with_cache = tracker.record_anthropic("claude-opus-4-7", make_usage(0, 0, cache_creation=1000))
        # Cache write = input × 1.25 → 1.25x du coût input
        assert cost_with_cache == pytest.approx(cost_no_cache * 1.25, rel=1e-3)

    def test_cache_read_costs_0_1x(self, tracker):
        cost_no_cache = tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 0))
        cost_with_cache = tracker.record_anthropic("claude-opus-4-7", make_usage(0, 0, cache_read=1000))
        assert cost_with_cache == pytest.approx(cost_no_cache * 0.1, rel=1e-3)

    def test_unknown_model_falls_back_to_opus_pricing(self, tracker):
        cost_unknown = tracker.record_anthropic("claude-unknown-model", make_usage(1000, 500))
        cost_opus = tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 500))
        assert cost_unknown == cost_opus

    def test_all_models_in_pricing_table(self):
        assert "claude-opus-4-7" in ANTHROPIC_PRICING
        assert "claude-sonnet-4-6" in ANTHROPIC_PRICING
        assert "claude-haiku-4-5" in ANTHROPIC_PRICING


class TestUsageFormats:
    def test_pydantic_model_dump(self, tracker):
        # Mock a Pydantic-like object with model_dump
        u = MagicMock(spec=["model_dump"])
        u.model_dump.return_value = {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        }
        cost = tracker.record_anthropic("claude-opus-4-7", u)
        assert cost == pytest.approx(0.0175, rel=1e-3)

    def test_dict_usage(self, tracker):
        cost = tracker.record_anthropic("claude-opus-4-7", {
            "input_tokens": 1000, "output_tokens": 500,
            "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
        })
        assert cost == pytest.approx(0.0175, rel=1e-3)

    def test_none_usage_doesnt_crash(self, tracker):
        # Si usage est vide/None
        cost = tracker.record_anthropic("claude-opus-4-7", make_usage())
        assert cost == 0.0


class TestOpenAIImage:
    def test_record_image_default_cost(self, tracker):
        cost = tracker.record_openai_image()
        assert cost == OPENAI_IMAGE_USD_DEFAULT

    def test_record_image_quality_tiers(self, tracker):
        low = tracker.record_openai_image(quality="low")
        high = tracker.record_openai_image(quality="high")
        assert low < OPENAI_IMAGE_USD_DEFAULT < high

    def test_unknown_quality_falls_back(self, tracker):
        cost = tracker.record_openai_image(quality="ultra")
        assert cost == OPENAI_IMAGE_USD_DEFAULT


class TestDailyAggregation:
    def test_get_daily_costs_aggregates(self, tracker):
        tracker.record_anthropic("claude-opus-4-7", make_usage(1000, 500))
        tracker.record_anthropic("claude-sonnet-4-6", make_usage(1000, 500))
        tracker.record_openai_image()
        tracker.record_openai_image()
        totals = tracker.get_daily_costs()
        assert totals["anthropic_calls"] == 2
        assert totals["openai_image_calls"] == 2
        assert totals["openai_image"] == pytest.approx(2 * OPENAI_IMAGE_USD_DEFAULT)
        assert totals["creation_total_usd"] == pytest.approx(
            totals["anthropic"] + totals["openai_image"]
        )

    def test_get_daily_costs_filters_by_date(self, tracker, tmp_path):
        # On écrit manuellement une entrée d'hier
        with open(tracker.log_path, "a") as f:
            f.write(json.dumps({
                "ts": (datetime.now() - timedelta(days=1)).isoformat(),
                "type": "openai_image", "cost_usd": 0.042,
            }) + "\n")
        tracker.record_openai_image()
        totals = tracker.get_daily_costs()
        assert totals["openai_image_calls"] == 1  # seulement aujourd'hui

    def test_get_daily_costs_no_log(self, tracker):
        # Avant tout enregistrement
        totals = tracker.get_daily_costs()
        assert totals["anthropic"] == 0.0
        assert totals["openai_image"] == 0.0

    def test_corrupt_log_lines_skipped(self, tracker):
        with open(tracker.log_path, "a") as f:
            f.write("not json\n")
            f.write(json.dumps({"ts": datetime.now().isoformat(), "type": "openai_image", "cost_usd": 0.042}) + "\n")
        totals = tracker.get_daily_costs()
        assert totals["openai_image_calls"] == 1


class TestCreationCap:
    def test_under_cap_not_capped(self, tracker):
        tracker.record_openai_image()  # ~$0.042 < cap $30 (30% de $100)
        capped, totals = tracker.is_creation_capped()
        assert capped is False

    def test_over_cap_triggered(self, tmp_path, base_config):
        config = {**base_config, "daily_creation_cost_cap_usd": 0.05}
        t = CostTracker(config, log_path=tmp_path / "costs.jsonl")
        t.record_openai_image()
        t.record_openai_image()  # ~$0.084 > $0.05
        capped, totals = t.is_creation_capped()
        assert capped is True
        assert totals["creation_total_usd"] >= t.daily_cap_creation_usd

    def test_cap_configurable(self, tmp_path, base_config):
        config = {**base_config, "daily_creation_cost_cap_usd": 0.02}
        t = CostTracker(config, log_path=tmp_path / "costs.jsonl")
        t.record_openai_image()  # ~$0.042 > $0.02
        capped, _ = t.is_creation_capped()
        assert capped is True


class TestPathHandling:
    def test_accepts_string_path(self, tmp_path, base_config):
        # Bug fix : accepter str ET Path
        t = CostTracker(base_config, log_path=str(tmp_path / "costs.jsonl"))
        t.record_openai_image()
        assert t.log_path.exists()
