"""
Tests for insights/gemini.py - the HTTP layer, with `requests.post` mocked.

The one thing in this file that is a security property rather than plumbing:
the key goes in a header and NOT in the URL. `requests` puts the URL into the
text of every exception it raises, so a key in the URL is a key in the logs.
"""

from unittest import mock

import pytest
import requests

from insights import gemini

pytestmark = pytest.mark.django_db


def fake_response(status=200, payload=None, text=None):
    """A requests.Response stand-in with just what `generate` reads."""
    response = mock.Mock(spec=requests.Response)
    response.status_code = status
    response.ok = 200 <= status < 300
    if text is not None:
        response.json.side_effect = ValueError("not json")
    else:
        response.json.return_value = payload if payload is not None else {}
    return response


def candidate(text: str, finish="STOP") -> dict:
    return {
        "candidates": [
            {"content": {"parts": [{"text": text}]}, "finishReason": finish}
        ]
    }


@pytest.fixture
def post():
    with mock.patch("insights.gemini.requests.post") as patched:
        yield patched


class TestRequestShape:
    def test_key_travels_in_the_header_not_the_url(self, post, gemini_settings):
        post.return_value = fake_response(payload=candidate('{"insights": []}'))

        gemini.generate("hello")

        url = post.call_args.args[0]
        kwargs = post.call_args.kwargs
        assert gemini_settings.GEMINI_API_KEY not in url
        assert "key=" not in url
        assert kwargs["headers"]["x-goog-api-key"] == gemini_settings.GEMINI_API_KEY
        assert url.endswith(f"/{gemini_settings.GEMINI_MODEL}:generateContent")

    def test_asks_for_json_pinned_to_the_schema(self, post):
        post.return_value = fake_response(payload=candidate('{"insights": []}'))

        gemini.generate("hello")

        config = post.call_args.kwargs["json"]["generationConfig"]
        assert config["responseMimeType"] == "application/json"
        assert config["responseSchema"] == gemini.RESPONSE_SCHEMA
        enum = config["responseSchema"]["properties"]["insights"]["items"]["properties"]["category"]["enum"]
        assert set(enum) == set(gemini.CATEGORIES)

    def test_uses_the_configured_timeout(self, post, gemini_settings):
        post.return_value = fake_response(payload=candidate("{}"))

        gemini.generate("hello")

        assert post.call_args.kwargs["timeout"] == gemini_settings.GEMINI_TIMEOUT_SECONDS

    def test_returns_the_candidates_text(self, post):
        post.return_value = fake_response(payload=candidate('{"insights": [1]}'))
        assert gemini.generate("hello") == '{"insights": [1]}'

    def test_skips_thought_parts_and_a_prose_preamble(self, post):
        """
        What the newer thinking models actually send: a `thought` part, then
        sometimes a "Here is the JSON:" part, then the JSON. Only the JSON is
        the answer. Observed against gemini-3.6-flash, not hypothetical.
        """
        post.return_value = fake_response(
            payload={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Let me think about the numbers.", "thought": True},
                                {"text": "Here is the JSON requested:", "thoughtSignature": "abc"},
                                {"text": '{"insights": [{"category": "general", "text": "Hi."}]}'},
                            ]
                        },
                        "finishReason": "STOP",
                    }
                ]
            }
        )
        assert gemini.generate("hello") == '{"insights": [{"category": "general", "text": "Hi."}]}'

    def test_salvages_json_wrapped_in_prose_within_one_part(self, post):
        post.return_value = fake_response(
            payload=candidate(
                'Sure!\n{"insights": [{"category": "gain", "text": "Up."}]}\nHope that helps.'
            )
        )
        assert gemini.generate("hello") == '{"insights": [{"category": "gain", "text": "Up."}]}'

    def test_only_thought_parts_is_an_empty_answer(self, post):
        post.return_value = fake_response(
            payload={
                "candidates": [
                    {"content": {"parts": [{"text": "thinking...", "thought": True}]}, "finishReason": "STOP"}
                ]
            }
        )
        with pytest.raises(gemini.GeminiBadResponse):
            gemini.generate("hello")


class TestFailures:
    def test_no_key_raises_before_any_request(self, post, gemini_settings):
        gemini_settings.GEMINI_API_KEY = ""

        with pytest.raises(gemini.GeminiNotConfigured):
            gemini.generate("hello")
        assert post.call_count == 0

    def test_429_is_rate_limited(self, post):
        post.return_value = fake_response(status=429, payload={"error": {"message": "quota"}})
        with pytest.raises(gemini.GeminiRateLimited) as info:
            gemini.generate("hello")
        assert info.value.reason == "rate_limited"

    @pytest.mark.parametrize("status", [500, 502, 503])
    def test_5xx_is_unavailable(self, post, status):
        post.return_value = fake_response(status=status)
        with pytest.raises(gemini.GeminiUnavailable):
            gemini.generate("hello")

    @pytest.mark.parametrize("status", [400, 403, 404])
    def test_other_4xx_is_a_bad_response(self, post, status):
        post.return_value = fake_response(status=status, payload={"error": {}})
        with pytest.raises(gemini.GeminiBadResponse):
            gemini.generate("hello")

    def test_timeout_is_unavailable(self, post):
        post.side_effect = requests.Timeout()
        with pytest.raises(gemini.GeminiUnavailable):
            gemini.generate("hello")

    def test_connection_error_is_unavailable(self, post):
        post.side_effect = requests.ConnectionError()
        with pytest.raises(gemini.GeminiUnavailable):
            gemini.generate("hello")

    def test_no_candidates_is_a_bad_response(self, post):
        """A safety block: 200, no candidates, a promptFeedback.blockReason."""
        post.return_value = fake_response(
            payload={"candidates": [], "promptFeedback": {"blockReason": "SAFETY"}}
        )
        with pytest.raises(gemini.GeminiBadResponse):
            gemini.generate("hello")

    def test_empty_candidate_is_a_bad_response(self, post):
        post.return_value = fake_response(
            payload={"candidates": [{"content": {"parts": []}, "finishReason": "MAX_TOKENS"}]}
        )
        with pytest.raises(gemini.GeminiBadResponse):
            gemini.generate("hello")

    def test_non_json_body_is_a_bad_response(self, post):
        post.return_value = fake_response(text="<html>")
        with pytest.raises(gemini.GeminiBadResponse):
            gemini.generate("hello")

    def test_no_retry_on_rate_limit(self, post):
        """One request per call, whatever came back. The user's click is the retry."""
        post.return_value = fake_response(status=429, payload={})
        with pytest.raises(gemini.GeminiRateLimited):
            gemini.generate("hello")
        assert post.call_count == 1
