"""Tests pour CreativeGenerator — Claude (concept + regen) + Arcads (vidéo)."""

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
        hook_first_3s="Hook test 3s",
        video_script="Script complet test",
        primary_text="Primary text",
        headline="Headline",
        description="Desc",
        avatar_persona="femme 40 ans QC",
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


# ----------- Cost tracking -----------

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

    def test_records_arcads_video(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(base_config, claude_client=mock_anthropic_client, cost_tracker=ct)

        # Mock arcads completion
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"job_id": "job_x"},
                raise_for_status=lambda: None,
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://video.test/x.mp4"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception  # for retry decorator
            with patch("creative_generator.time.sleep"):  # accélérer
                url = gen.generate_video({"video_script": "s", "avatar_persona": "p"})
        assert url.startswith("https://")
        ct.record_arcads.assert_called_once()


# ----------- Arcads video generation -----------

class TestGenerateArcads:
    def test_returns_video_url_on_completion(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"job_id": "j1"},
                raise_for_status=lambda: None,
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "completed", "video_url": "https://x/y.mp4"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                url = gen.generate_video({"video_script": "s", "avatar_persona": "p"})
        assert url == "https://x/y.mp4"

    def test_raises_on_arcads_failed(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "failed", "error": "bad script"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                with pytest.raises(RuntimeError, match="bad script"):
                    gen.generate_video({"video_script": "s", "avatar_persona": "p"})

    def test_raises_on_timeout(self, gen):
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"job_id": "j1"}, raise_for_status=lambda: None
            )
            # Toujours processing
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "processing"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                with pytest.raises(TimeoutError):
                    gen.generate_video({"video_script": "s", "avatar_persona": "p"})

    def test_invalid_provider_raises(self, base_config, mock_anthropic_client):
        config = {**base_config, "video_provider": "unknown"}
        gen = CreativeGenerator(config, claude_client=mock_anthropic_client)
        with pytest.raises(ValueError, match="Provider"):
            gen.generate_video({"video_script": "s", "avatar_persona": "p"})


def _heygen_config(base_config):
    return {
        **base_config,
        "video_provider": "heygen",
        "heygen": {
            "api_key": "hg_test",
            "avatar_id": "av_1",
            "voice_source": "heygen",
            "voice_id": "vo_frca",
            "dimension": {"width": 720, "height": 1280},
        },
    }


class TestGenerateHeygen:
    def test_returns_video_url_on_completion(self, base_config, mock_anthropic_client):
        gen = CreativeGenerator(_heygen_config(base_config), claude_client=mock_anthropic_client)
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"error": None, "data": {"video_id": "vid_1"}},
                raise_for_status=lambda: None,
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"data": {"status": "completed", "video_url": "https://hg/v.mp4"}},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                url = gen.generate_video({"video_script": "s", "avatar_persona": "p", "angle": "a"})
        assert url == "https://hg/v.mp4"

    def test_records_heygen_cost(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(_heygen_config(base_config), claude_client=mock_anthropic_client, cost_tracker=ct)
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"data": {"video_id": "vid_1"}}, raise_for_status=lambda: None
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"data": {"status": "completed", "video_url": "https://hg/v.mp4"}},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                gen.generate_video({"video_script": "s", "avatar_persona": "p"})
        ct.record_heygen.assert_called_once()

    def test_raises_on_heygen_failed(self, base_config, mock_anthropic_client):
        gen = CreativeGenerator(_heygen_config(base_config), claude_client=mock_anthropic_client)
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"data": {"video_id": "vid_1"}}, raise_for_status=lambda: None
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"data": {"status": "failed", "error": "bad voice"}},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                with pytest.raises(RuntimeError, match="bad voice"):
                    gen.generate_video({"video_script": "s", "avatar_persona": "p"})

    def test_raises_when_config_incomplete(self, base_config, mock_anthropic_client):
        config = {**base_config, "video_provider": "heygen", "heygen": {"api_key": "k"}}
        gen = CreativeGenerator(config, claude_client=mock_anthropic_client)
        with pytest.raises(RuntimeError, match="HeyGen incomplète"):
            gen.generate_video({"video_script": "s", "avatar_persona": "p"})


def _heygen_elevenlabs_config(base_config):
    return {
        **base_config,
        "video_provider": "heygen",
        "heygen": {
            "api_key": "hg_test",
            "avatar_id": "av_1",
            "voice_source": "elevenlabs",
            "dimension": {"width": 720, "height": 1280},
        },
        "elevenlabs": {
            "api_key": "el_test",
            "voice_id": "qc_voice",
            "model_id": "eleven_multilingual_v2",
        },
    }


class TestHeygenElevenLabs:
    """Pipeline retenu : voix québécoise ElevenLabs → avatar HeyGen (lip-sync)."""

    def test_elevenlabs_audio_piped_to_heygen(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(
            _heygen_elevenlabs_config(base_config),
            claude_client=mock_anthropic_client, cost_tracker=ct,
        )
        el_resp = MagicMock(content=b"mp3-bytes", raise_for_status=lambda: None)
        upload_resp = MagicMock(
            json=lambda: {"data": {"id": "asset_99"}}, raise_for_status=lambda: None
        )
        gen_resp = MagicMock(
            json=lambda: {"data": {"video_id": "vid_1"}}, raise_for_status=lambda: None
        )
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.side_effect = [el_resp, upload_resp, gen_resp]
            mock_req.get.return_value = MagicMock(
                json=lambda: {"data": {"status": "completed", "video_url": "https://hg/v.mp4"}},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                url = gen.generate_video({"video_script": "Bonjour", "avatar_persona": "p"})

        assert url == "https://hg/v.mp4"
        # 1er POST = ElevenLabs TTS, 2e = upload HeyGen, 3e = generate
        first_call = mock_req.post.call_args_list[0]
        assert "elevenlabs.io" in first_call.args[0]
        # le payload generate doit utiliser l'audio uploadé, pas une voix HeyGen
        gen_payload = mock_req.post.call_args_list[2].kwargs["json"]
        voice = gen_payload["video_inputs"][0]["voice"]
        assert voice == {"type": "audio", "audio_asset_id": "asset_99"}
        ct.record_heygen.assert_called_once()

    def test_raises_when_elevenlabs_config_missing(self, base_config, mock_anthropic_client):
        cfg = _heygen_elevenlabs_config(base_config)
        cfg["elevenlabs"] = {}  # pas de clé ni voice_id
        gen = CreativeGenerator(cfg, claude_client=mock_anthropic_client)
        with pytest.raises(RuntimeError, match="ElevenLabs incomplète"):
            gen.generate_video({"video_script": "s", "avatar_persona": "p"})


def _creatify_config(base_config):
    return {
        **base_config,
        "video_provider": "creatify",
        "creatify": {
            "api_id": "cid",
            "api_key": "ckey",
            "persona_id": "persona_1",
            "voice_id": "voice_fr",
            "aspect_ratio": "9x16",
            "no_caption": True,
        },
    }


class TestGenerateCreatify:
    def test_create_then_poll_returns_url(self, base_config, mock_anthropic_client):
        ct = MagicMock()
        gen = CreativeGenerator(
            _creatify_config(base_config),
            claude_client=mock_anthropic_client, cost_tracker=ct,
        )
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"id": "job1"}, content=b"{}",
                raise_for_status=lambda: None,
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "done", "output": "https://c/v.mp4"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                url = gen.generate_video({"video_script": "Bonjour [ton posé]",
                                          "avatar_persona": "p", "angle": "a"})
        assert url == "https://c/v.mp4"
        # Un seul POST (create) — PAS d'appel /render/
        assert mock_req.post.call_count == 1
        assert mock_req.post.call_args.args[0].endswith("/lipsyncs/")
        # script nettoyé (didascalie retirée avant envoi)
        sent = mock_req.post.call_args.kwargs["json"]["text"]
        assert "[" not in sent and "ton posé" not in sent
        ct.record_creatify.assert_called_once()

    def test_failed_status_raises(self, base_config, mock_anthropic_client):
        gen = CreativeGenerator(_creatify_config(base_config), claude_client=mock_anthropic_client)
        with patch("creative_generator.requests") as mock_req:
            mock_req.post.return_value = MagicMock(
                json=lambda: {"id": "j"}, content=b"{}", raise_for_status=lambda: None
            )
            mock_req.get.return_value = MagicMock(
                json=lambda: {"status": "failed", "failed_reason": "bad persona"},
                raise_for_status=lambda: None,
            )
            mock_req.RequestException = Exception
            with patch("creative_generator.time.sleep"):
                with pytest.raises(RuntimeError, match="bad persona"):
                    gen.generate_video({"video_script": "s", "avatar_persona": "p"})

    def test_incomplete_config_raises(self, base_config, mock_anthropic_client):
        cfg = {**base_config, "video_provider": "creatify", "creatify": {"api_id": "x"}}
        gen = CreativeGenerator(cfg, claude_client=mock_anthropic_client)
        with pytest.raises(RuntimeError, match="Creatify incomplète"):
            gen.generate_video({"video_script": "s", "avatar_persona": "p"})


class TestCleanScript:
    def test_strips_stage_directions(self):
        raw = "[ton posé] Salut! (sourire) Ma facture montait. CTA: vas-y."
        out = CreativeGenerator._clean_script(raw)
        assert "[" not in out and "]" not in out
        assert "(" not in out and ")" not in out
        assert "ton posé" not in out
        assert "sourire" not in out
        assert not out.lower().startswith("cta")
        assert "Ma facture montait." in out

    def test_strips_line_prefixes_and_normalizes(self):
        raw = "Hook: T'es tanné?\nVoix off - Change pour mieux."
        out = CreativeGenerator._clean_script(raw)
        assert "Hook:" not in out
        assert "Voix off" not in out
        assert out == "T'es tanné? Change pour mieux."

    def test_plain_script_untouched(self):
        raw = "Franchement, ça vaut la peine d'essayer."
        assert CreativeGenerator._clean_script(raw) == raw


class TestNormalizeLengths:
    def test_short_fields_unchanged(self):
        c = {"primary_text": "court", "headline": "ok", "description": "d",
             "video_script": "Phrase normale."}
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

    def test_video_script_trimmed_at_sentence(self):
        # Script réaliste : plusieurs phrases ~54 car. -> une frontière de
        # phrase existe bien après limite*0.5, donc coupe nette sur un point.
        s = "Ça vaut la peine de comparer ton forfait aujourd'hui. " * 12
        out = CreativeGenerator._normalize_lengths({"video_script": s})
        assert len(out["video_script"]) <= 420
        assert out["video_script"].endswith(".")

    def test_video_script_word_trim_when_no_late_sentence(self):
        # Pas de point après limite*0.5 -> repli sur frontière de mot,
        # jamais de mot coupé.
        s = "Phrase courte. " + "mot " * 300
        out = CreativeGenerator._normalize_lengths({"video_script": s})
        assert len(out["video_script"]) <= 420
        assert out["video_script"].endswith("mot")

    def test_concept_output_accepts_overlong_then_normalized(self):
        # Le modèle ne doit PLUS lever de ValidationError sur longueur
        long_pt = "a" * 400
        co = ConceptOutput(
            angle="a", hook_first_3s="h", video_script="s",
            primary_text=long_pt, headline="b" * 80, description="c" * 80,
            avatar_persona="p",
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

    def test_famille_angle_recommended(self):
        assert "famille_4lignes" in CONCEPT_SYSTEM
        assert "ado" in CONCEPT_SYSTEM.lower()
