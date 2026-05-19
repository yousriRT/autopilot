"""Tests pour PolicyValidator — validation Sonnet 4.6 + adaptive thinking."""

import pytest
from unittest.mock import MagicMock
from policy_validator import PolicyValidator, PolicyResult


@pytest.fixture
def validator(base_config, mock_anthropic_client):
    return PolicyValidator(base_config, claude_client=mock_anthropic_client)


def make_parse_response(parsed, input_tokens=500, output_tokens=200):
    mock = MagicMock()
    mock.parsed_output = parsed
    mock.usage = MagicMock(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return mock


class TestValidate:
    def test_returns_valid_for_clean_creative(self, validator, mock_anthropic_client, sample_creative_brief):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True, issues=[])
        )
        ok, issues = validator.validate(sample_creative_brief)
        assert ok is True
        assert issues == []

    def test_returns_invalid_with_issues(self, validator, mock_anthropic_client, sample_creative_brief):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=False, issues=["Mention de Bell détectée", "Promesse absolue 'garantie'"])
        )
        ok, issues = validator.validate(sample_creative_brief)
        assert ok is False
        assert len(issues) == 2

    def test_uses_sonnet_4_6_with_adaptive_thinking(self, validator, mock_anthropic_client, sample_creative_brief):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True)
        )
        validator.validate(sample_creative_brief)
        call_kwargs = mock_anthropic_client.messages.parse.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-6"
        assert call_kwargs["thinking"] == {"type": "adaptive"}

    def test_uses_cache_control_on_system(self, validator, mock_anthropic_client, sample_creative_brief):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True)
        )
        validator.validate(sample_creative_brief)
        call_kwargs = mock_anthropic_client.messages.parse.call_args.kwargs
        system = call_kwargs["system"]
        assert system[0]["cache_control"] == {"type": "ephemeral"}

    def test_uses_pydantic_output_format(self, validator, mock_anthropic_client, sample_creative_brief):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True)
        )
        validator.validate(sample_creative_brief)
        call_kwargs = mock_anthropic_client.messages.parse.call_args.kwargs
        assert call_kwargs["output_format"] == PolicyResult


class TestCostTrackerIntegration:
    def test_records_cost_when_tracker_provided(self, base_config, mock_anthropic_client, sample_creative_brief):
        cost_tracker = MagicMock()
        v = PolicyValidator(base_config, claude_client=mock_anthropic_client, cost_tracker=cost_tracker)
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True), input_tokens=1000, output_tokens=300
        )
        v.validate(sample_creative_brief)
        cost_tracker.record_anthropic.assert_called_once()
        call_args = cost_tracker.record_anthropic.call_args
        assert call_args.args[0] == "claude-sonnet-4-6"

    def test_no_cost_recorded_when_no_tracker(self, validator, mock_anthropic_client, sample_creative_brief):
        # Pas de cost_tracker → pas d'erreur
        mock_anthropic_client.messages.parse.return_value = make_parse_response(
            PolicyResult(valid=True)
        )
        ok, _ = validator.validate(sample_creative_brief)
        assert ok is True


class TestSystemPromptContent:
    def test_telus_in_blacklist(self, validator):
        from policy_validator import VALIDATION_SYSTEM
        # Mémoire : Telus DOIT figurer dans la liste interdite
        assert "Telus" in VALIDATION_SYSTEM

    def test_brand_mention_is_critical_rule_1(self, validator):
        from policy_validator import VALIDATION_SYSTEM
        assert "Brand Mention" in VALIDATION_SYSTEM
        # La règle #1 doit être brand mention (priorité business)
        assert VALIDATION_SYSTEM.index("Brand Mention") < VALIDATION_SYSTEM.index("Personal Attributes")

    def test_minor_protection_rule_for_family(self, validator):
        from policy_validator import VALIDATION_SYSTEM
        assert "famille_4lignes" in VALIDATION_SYSTEM
        assert "mineur" in VALIDATION_SYSTEM.lower() or "Mineurs" in VALIDATION_SYSTEM
