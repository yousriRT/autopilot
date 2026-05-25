"""Tests pour CreativeGenerator — Claude (concept + regen) + OpenAI (image)."""

import base64
import pytest
from unittest.mock import MagicMock, patch
from creative_generator import CreativeGenerator, ConceptOutput, CONCEPT_SYSTEM


@pytest.fixture
def gen(base_config, mock_anthropic_client):
    return CreativeGenerator(base_config, claude_client=mock_anthropic_client)


def make_parse_response(parsed, input_tokens=500, output_tokens=300):
    mock = MagicMock()
    mock.parsed_output = parsed
    mock.usage = MagicMock(
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_creation_input_tokens=0, cache_read_input_tokens=0,
    )
    return mock


def make_concept(angle="frustration_facture"):
    return ConceptOutput(
        angle=angle,
        image_prompt="A candid lifestyle photo, natural light, no text, no logos.",
        primary_text="Primary text",
        headline="Headline",
        description="Desc",
    )


# ----------- generate_concept -----------

class TestGenerateConcept:
    def test_returns_dict(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        result = gen.generate_concept("famille_4lignes", past_winners=[])
        assert isinstance(result, dict)
        assert result["angle"] == "frustration_facture"

    def test_uses_opus_4_7(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.generate_concept("fibre", past_winners=[])
        assert mock_anthropic_client.messages.parse.call_args.kwargs["model"] == "claude-opus-4-7"

    def test_passes_winners_to_message(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        winners = ["frustration_facture (CPL 5€)", "voisin_fibre (CPL 7€)"]
        gen.generate_concept("fibre", past_winners=winners)
        msg_content = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "frustration_facture" in msg_content

    def test_handles_empty_winners(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.generate_concept("fibre", past_winners=[])
        msg_content = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "aucun" in msg_content.lower() or "première" in msg_content.lower()

    def test_with_diversity_category_constraint(self, base_config, mock_anthropic_client):
        diversity = MagicMock()
        diversity.get_brief.return_value = "Catégorie family : ado, conjoint..."
        gen = CreativeGenerator(base_config, claude_client=mock_anthropic_client, diversity=diversity)
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.generate_concept("famille_4lignes", past_winners=[], category="family")
        msg = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "family" in msg
        assert "Catégorie family" in msg

    def test_no_category_no_brief_in_message(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.generate_concept("fibre", past_winners=[], category=None)
        msg = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "CATÉGORIE D'ANGLE IMPOSÉE" not in msg


# ----------- regenerate_safe -----------

class TestRegenerateSafe:
    def test_with_policy_source(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept("nouveau"))
        original = make_concept("ancien").model_dump()
        result = gen.regenerate_safe(original, ["mention Bell"], source="policy")
        assert result["angle"] == "nouveau"
        msg = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "policy validator" in msg.lower() or "Claude" in msg

    def test_with_meta_preview_source(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept("v2"))
        original = make_concept("v1").model_dump()
        result = gen.regenerate_safe(original, ["text trop long"], source="meta_preview")
        msg = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "Meta Preview" in msg

    def test_includes_issues_list(self, gen, mock_anthropic_client):
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        original = make_concept().model_dump()
        gen.regenerate_safe(original, ["issue A", "issue B"], source="policy")
        msg = mock_anthropic_client.messages.parse.call_args.kwargs["messages"][0]["content"]
        assert "issue A" in msg
        assert "issue B" in msg


# ----------- Cost tracking (Claude) -----------

class TestCostTracking:
    def test_records_concept_cost(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(base_config, claude_client=mock_anthropic_client, cost_tracker=ct)
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.generate_concept("fibre", past_winners=[])
        ct.record_anthropic.assert_called_once()
        assert ct.record_anthropic.call_args.args[0] == "claude-opus-4-7"

    def test_records_regen_cost_with_purpose(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(base_config, claude_client=mock_anthropic_client, cost_tracker=ct)
        mock_anthropic_client.messages.parse.return_value = make_parse_response(make_concept())
        gen.regenerate_safe(make_concept().model_dump(), ["x"], source="meta_preview")
        purpose = ct.record_anthropic.call_args.kwargs.get("purpose")
        assert "meta_preview" in purpose


# ----------- OpenAI image generation -----------

def _ok_image_response(content=b"PNGDATA"):
    payload = base64.b64encode(content).decode("ascii")
    return MagicMock(
        json=lambda: {"data": [{"b64_json": payload}]},
        raise_for_status=lambda: None,
    )


class TestGenerateImage:
    def test_returns_image_bytes(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = _ok_image_response(b"PNGDATA")
            mock_req.RequestException = Exception
            out = gen.generate_image({"image_prompt": "a photo", "angle": "a"})
        assert out == b"PNGDATA"

    def test_calls_openai_endpoint_with_prompt(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = _ok_image_response()
            mock_req.RequestException = Exception
            gen.generate_image({"image_prompt": "kitchen scene", "angle": "a"})
        url = mock_req.post.call_args.args[0]
        body = mock_req.post.call_args.kwargs["json"]
        assert "api.openai.com" in url and "images/generations" in url
        assert body["prompt"] == "kitchen scene"
        assert body["model"] == "gpt-image-1"

    def test_records_openai_cost(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(base_config, claude_client=mock_anthropic_client, cost_tracker=ct)
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = _ok_image_response()
            mock_req.RequestException = Exception
            gen.generate_image({"image_prompt": "p", "angle": "a"})
        ct.record_openai_image.assert_called_once()

    def test_invalid_provider_raises(self, base_config, mock_anthropic_client):
        config = {**base_config, "image_provider": "unknown"}
        gen = CreativeGenerator(config, claude_client=mock_anthropic_client)
        with pytest.raises(ValueError, match="Provider image"):
            gen.generate_image({"image_prompt": "p"})

    def test_missing_api_key_raises(self, base_config, mock_anthropic_client, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        cfg = {**base_config, "openai": {}}
        gen = CreativeGenerator(cfg, claude_client=mock_anthropic_client)
        with pytest.raises(RuntimeError, match="OpenAI incomplète"):
            gen.generate_image({"image_prompt": "p"})

    def test_missing_prompt_raises(self, gen):
        with pytest.raises(RuntimeError, match="image_prompt manquant"):
            gen.generate_image({"angle": "a"})

    def test_unexpected_response_raises(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"oops": True}, raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with pytest.raises(RuntimeError, match="inattendue"):
                gen.generate_image({"image_prompt": "p", "angle": "a"})


class TestNormalizeLengths:
    def test_short_fields_unchanged(self):
        c = {"primary_text": "court", "headline": "ok", "description": "d"}
        out = CreativeGenerator._normalize_lengths(dict(c))
        assert out == c

    def test_long_primary_text_trimmed_no_word_cut(self):
        long = ("Tu paies des frais de déplacement à chaque visite technique "
                "et personne ne te prévient jamais avant. Vois si t'es éligible "
                "à une meilleure offre dès maintenant aujourd'hui.")
        out = CreativeGenerator._normalize_lengths({"primary_text": long})
        assert len(out["primary_text"]) <= 125
        assert not out["primary_text"].endswith(" ")
        # pas de mot coupé : le dernier token est entier
        assert long.startswith(out["primary_text"].rstrip())

    def test_headline_and_description_capped(self):
        out = CreativeGenerator._normalize_lengths({
            "headline": "x" * 100, "description": "y" * 100,
        })
        assert len(out["headline"]) <= 40
        assert len(out["description"]) <= 30

    def test_concept_output_accepts_overlong_then_normalized(self):
        # Le modèle ne doit PLUS lever de ValidationError sur longueur
        long_pt = "a" * 400
        co = ConceptOutput(
            angle="a", image_prompt="s",
            primary_text=long_pt, headline="b" * 80, description="c" * 80,
        )
        norm = CreativeGenerator._normalize_lengths(co.model_dump())
        assert len(norm["primary_text"]) <= 125


# ----------- System prompt content -----------

class TestSystemPromptContent:
    def test_telus_in_blacklist(self):
        # Mémoire : Telus DOIT être listé interdit
        assert "Telus" in CONCEPT_SYSTEM

    def test_no_brand_constraint_present(self):
        assert "Ne nomme JAMAIS de marque" in CONCEPT_SYSTEM
        assert "Bell" in CONCEPT_SYSTEM

    def test_famille_bundle_offer_described(self):
        # L'offre unique (4 lignes + TV + internet, famille) doit cadrer le prompt
        assert "4 lignes" in CONCEPT_SYSTEM
        assert "ado" in CONCEPT_SYSTEM.lower()

    def test_image_prompt_constraint_present(self):
        # Le prompt doit cadrer l'image : pas de texte, pas de logo
        assert "image_prompt" in CONCEPT_SYSTEM
