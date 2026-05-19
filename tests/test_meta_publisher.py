"""Tests pour MetaPublisher — wrapper Marketing API + retry + Preview."""

import pytest
import requests
from unittest.mock import patch, MagicMock
from meta_publisher import MetaPublisher


@pytest.fixture
def publisher(base_config):
    return MetaPublisher(base_config)


def make_response(status_code=200, json_body=None, text=""):
    r = MagicMock(spec=requests.Response)
    r.status_code = status_code
    r.ok = 200 <= status_code < 300
    r.json.return_value = json_body or {}
    r.text = text
    if not r.ok:
        r.raise_for_status.side_effect = requests.HTTPError(f"{status_code}", response=r)
    else:
        r.raise_for_status.return_value = None
    return r


class TestRequestRetry:
    def test_get_succeeds(self, publisher):
        with patch("meta_publisher.requests.get") as mget:
            mget.return_value = make_response(json_body={"data": [{"id": "x"}]})
            result = publisher._request("GET", "act_123/ads")
            assert "data" in result

    def test_post_succeeds(self, publisher):
        with patch("meta_publisher.requests.post") as mpost:
            mpost.return_value = make_response(json_body={"id": "ad_x"})
            result = publisher._request("POST", "endpoint", data={"k": "v"})
            assert result["id"] == "ad_x"

    def test_retry_on_5xx(self, publisher):
        # Le retry décorateur retry sur RequestException — HTTPError sur 5xx en fait partie
        with patch("meta_publisher.requests.get") as mget, \
             patch("meta_publisher.time.sleep"):
            mget.side_effect = [
                requests.ConnectionError("network blip"),
                requests.ConnectionError("again"),
                make_response(json_body={"ok": True}),
            ]
            result = publisher._request("GET", "any")
            assert result == {"ok": True}
            assert mget.call_count == 3

    def test_no_retry_on_4xx(self, publisher):
        # 4xx (mauvais token, 400) ne doit pas retry
        with patch("meta_publisher.requests.get") as mget:
            r = make_response(status_code=400, text="bad request")
            mget.return_value = r
            with pytest.raises(requests.HTTPError):
                publisher._request("GET", "any")
            # 1 seul appel (pas de retry)
            assert mget.call_count == 1

    def test_max_attempts_exhausted(self, publisher):
        with patch("meta_publisher.requests.get") as mget, \
             patch("meta_publisher.time.sleep"):
            mget.side_effect = requests.ConnectionError("permanent")
            with pytest.raises(requests.ConnectionError):
                publisher._request("GET", "any")
            assert mget.call_count == 3  # max_attempts default

    def test_access_token_added_to_params(self, publisher):
        with patch("meta_publisher.requests.get") as mget:
            mget.return_value = make_response(json_body={})
            publisher._request("GET", "x")
            params = mget.call_args.kwargs["params"]
            assert params["access_token"] == "EAA-test"


class TestUploadVideo:
    def test_upload_returns_id(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"id": "video_xyz"}
            assert publisher.upload_video("https://video/x.mp4") == "video_xyz"
            assert mreq.call_args.args == ("POST", "act_123/advideos")


class TestCreateAdCreative:
    def test_returns_creative_id(self, publisher):
        with patch.object(publisher, "_request") as mreq, \
             patch("meta_publisher.time.sleep"):
            # 1er appel = GET thumbnail, 2e = POST adcreatives
            mreq.side_effect = [
                {"thumbnails": {"data": [
                    {"uri": "https://t/x.jpg", "is_preferred": True}
                ]}},
                {"id": "creative_xyz"},
            ]
            cid = publisher.create_ad_creative(
                "video_1", "primary", "headline", "desc"
            )
            assert cid == "creative_xyz"
            data = mreq.call_args.kwargs["data"]  # dernier appel = POST
            import json as jsonmod
            spec = jsonmod.loads(data["object_story_spec"])
            assert spec["page_id"] == "page_123"
            assert spec["video_data"]["video_id"] == "video_1"
            # Miniature obligatoire pour Meta (sinon erreur 1443226)
            assert spec["video_data"]["image_url"] == "https://t/x.jpg"
            cta = spec["video_data"]["call_to_action"]
            assert cta["value"]["lead_gen_form_id"] == "form_999"
            assert "link" not in cta["value"]

    def test_thumbnail_polls_until_ready(self, publisher):
        with patch.object(publisher, "_request") as mreq, \
             patch("meta_publisher.time.sleep"):
            mreq.side_effect = [
                {"status": {"video_status": "processing"}, "thumbnails": {"data": []}},
                {"thumbnails": {"data": [{"uri": "https://t/ok.jpg"}]}},
            ]
            assert publisher._video_thumbnail_url("vid_1") == "https://t/ok.jpg"

    def test_raises_without_form_id(self, base_config):
        import copy
        cfg = copy.deepcopy(base_config)
        cfg["meta"].pop("lead_gen_form_id", None)
        pub = MetaPublisher(cfg)
        with pytest.raises(RuntimeError, match="lead_gen_form_id"):
            pub.create_ad_creative("v", "p", "h", "d")


class TestPreviewCreative:
    def test_preview_ok(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": [{"body": "<html>ad preview</html>"}]}
            result = publisher.preview_creative("creative_x")
            assert result["ok"] is True
            assert "ad preview" in result["body"]

    def test_preview_empty_data_returns_rejected(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": []}
            result = publisher.preview_creative("creative_x")
            assert result["ok"] is False

    def test_preview_http_error_extracts_message(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            r = make_response(status_code=400)
            r.json.return_value = {"error": {"message": "creative invalide"}}
            mreq.side_effect = requests.HTTPError("400", response=r)
            result = publisher.preview_creative("creative_x")
            assert result["ok"] is False
            assert "invalide" in result["body"]

    def test_preview_generic_exception(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.side_effect = RuntimeError("totally unexpected")
            result = publisher.preview_creative("creative_x")
            assert result["ok"] is False


class TestGetActiveAds:
    def test_filters_active_only(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": [
                {"id": "a1", "status": "ACTIVE", "name": "AUTOPILOT_AD_x"},
                {"id": "a2", "status": "PAUSED", "name": "AUTOPILOT_AD_y"},
            ]}
            ads = publisher.get_active_ads()
            assert len(ads) == 1
            assert ads[0]["id"] == "a1"


class TestGetAdInsights:
    def test_parses_lead_actions(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": [{
                "spend": "25.50", "impressions": "1000", "reach": "700",
                "clicks": "30", "ctr": "3.0", "frequency": "1.4",
                "actions": [
                    {"action_type": "lead", "value": "5"},
                    {"action_type": "page_engagement", "value": "100"},
                    {"action_type": "onsite_conversion.lead_grouped", "value": "2"},
                ]
            }]}
            insights = publisher.get_ad_insights("ad_1")
            assert insights["spend"] == 25.50
            assert insights["leads"] == 7  # 5 + 2
            assert insights["reach"] == 700

    def test_no_data_returns_zeros(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": []}
            insights = publisher.get_ad_insights("ad_1")
            assert insights["spend"] == 0
            assert insights["leads"] == 0
            assert insights["reach"] == 0


class TestGetAdLeads:
    def test_returns_leads(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": [
                {"id": "L1", "field_data": [{"name": "email", "values": ["a@b.com"]}]},
            ]}
            leads = publisher.get_ad_leads("ad_1")
            assert len(leads) == 1
            assert leads[0]["id"] == "L1"

    def test_handles_permission_error_gracefully(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.side_effect = Exception("(#10) permissions error")
            leads = publisher.get_ad_leads("ad_1")
            assert leads == []  # fail-soft


class TestUpdateAdBudget:
    def test_finds_adset_then_updates(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.side_effect = [
                {"adset_id": "as_x"},  # fetch ad
                {"success": True},     # update budget
            ]
            publisher.update_ad_budget("ad_1", 25.50)
            # 2nd call cible adset_id, daily_budget en cents
            update_call = mreq.call_args_list[1]
            assert update_call.args == ("POST", "as_x")
            assert update_call.kwargs["data"]["daily_budget"] == 2550


class TestPauseAd:
    def test_sends_paused_status(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            publisher.pause_ad("ad_1")
            mreq.assert_called_with("POST", "ad_1", data={"status": "PAUSED"})


class TestGetOrCreateCampaign:
    def test_returns_existing(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.return_value = {"data": [
                {"id": "camp_1", "name": "AUTOPILOT_FIBRE", "status": "ACTIVE"},
            ]}
            cid = publisher._get_or_create_campaign("fibre")
            assert cid == "camp_1"

    def test_creates_when_absent(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.side_effect = [
                {"data": []},  # list
                {"id": "camp_new"},  # create
            ]
            cid = publisher._get_or_create_campaign("fibre")
            assert cid == "camp_new"

    def test_skips_deleted_campaigns(self, publisher):
        with patch.object(publisher, "_request") as mreq:
            mreq.side_effect = [
                {"data": [{"id": "camp_old", "name": "AUTOPILOT_FIBRE", "status": "DELETED"}]},
                {"id": "camp_new"},
            ]
            cid = publisher._get_or_create_campaign("fibre")
            assert cid == "camp_new"


class TestPublishAdFlow:
    def test_full_flow(self, publisher):
        with patch.object(publisher, "_request") as mreq, \
             patch("meta_publisher.time.sleep"):
            mreq.side_effect = [
                {"data": [{"id": "camp_1", "name": "AUTOPILOT_FIBRE", "status": "ACTIVE"}]},
                {"id": "video_1"},      # upload_video
                {"thumbnails": {"data": [{"uri": "https://t/x.jpg"}]}},  # thumbnail poll
                {"id": "creative_1"},   # create_ad_creative
                {"id": "adset_1"},      # _create_adset
                {"id": "ad_1"},         # _create_ad
            ]
            result = publisher.publish_ad(
                vertical="fibre",
                video_url="https://x/y.mp4",
                primary_text="primary",
                headline="head",
                description="desc",
                daily_budget=10.0,
            )
            assert result["ad_id"] == "ad_1"
            assert result["campaign_id"] == "camp_1"
