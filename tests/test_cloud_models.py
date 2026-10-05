import json
import os
import unittest
from unittest.mock import patch
from urllib.error import URLError

from niji import cloud_models


class _FakeResponse:
    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, limit):
        return self.body[:limit]


class CloudModelsTests(unittest.TestCase):
    def setUp(self):
        cloud_models._discover_provider_models.cache_clear()

    def tearDown(self):
        cloud_models._discover_provider_models.cache_clear()

    def test_legacy_safe_choices_remain_when_provider_discovery_is_not_configured(self):
        with patch.dict(os.environ, {
            "NIJI_CLOUD_PROVIDER": "nvidia",
            "NIJI_CLOUD_BASE_URL": "",
            "NIJI_CLOUD_PROVIDER_API_KEY": "",
        }, clear=False):
            catalog = cloud_models.public_model_catalog()
        self.assertEqual([item["id"] for item in catalog], [
            "nemotron-3.5-lightning", "nemotron-3-super-120b",
        ])
        self.assertNotIn("provider", json.dumps(catalog).lower())
        self.assertNotIn("api_key", json.dumps(catalog).lower())

    def test_catalog_uses_live_nvidia_models_and_keeps_provider_details_private(self):
        payload = {"data": [
            {"id": "nvidia/nemotron-3.5-lightning-30b-a3b", "owned_by": "nvidia"},
            {"id": "deepseek-ai/deepseek-v3.2", "owned_by": "deepseek-ai"},
            {"id": "not a valid ID", "owned_by": "secret-company"},
        ]}
        with patch.dict(os.environ, {
            "NIJI_CLOUD_PROVIDER": "nvidia",
            "NIJI_CLOUD_BASE_URL": "https://provider.example/v1",
            "NIJI_CLOUD_PROVIDER_API_KEY": "test-secret",
            "NIJI_CLOUD_MODEL": "nvidia/nemotron-3.5-lightning-30b-a3b",
        }, clear=False), patch("niji.cloud_models.urlopen", return_value=_FakeResponse(payload)) as open_url:
            catalog = cloud_models.public_model_catalog()
            self.assertTrue(cloud_models.is_known_cloud_model("deepseek-ai/deepseek-v3.2"))
            resolved = cloud_models.resolve_cloud_model("deepseek-ai/deepseek-v3.2", "nvidia")

        self.assertEqual(resolved, "deepseek-ai/deepseek-v3.2")
        self.assertEqual(len(catalog), 2)
        self.assertTrue(next(item for item in catalog if item["id"].startswith("nvidia/"))["default"])
        self.assertEqual(next(item for item in catalog if item["id"].startswith("deepseek"))["name"], "deepseek v3.2")
        names = " ".join(item["name"] for item in catalog).lower()
        self.assertNotIn("nvidia", names)
        self.assertNotIn("deepseek-ai", names)
        self.assertNotIn("test-secret", names)
        request = open_url.call_args.args[0]
        self.assertEqual(request.full_url, "https://provider.example/v1/models")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-secret")

    def test_invalid_or_unavailable_catalog_falls_back_without_exposing_provider_error(self):
        with patch.dict(os.environ, {
            "NIJI_CLOUD_PROVIDER": "nvidia",
            "NIJI_CLOUD_BASE_URL": "http://unsafe.example/v1",
            "NIJI_CLOUD_PROVIDER_API_KEY": "test-secret",
        }, clear=False), patch("niji.cloud_models.urlopen") as open_url:
            catalog = cloud_models.public_model_catalog()
        open_url.assert_not_called()
        self.assertEqual(len(catalog), 2)
        self.assertNotIn("unsafe.example", json.dumps(catalog))

    def test_provider_model_discovery_failure_keeps_fallback_and_does_not_echo_error(self):
        with patch.dict(os.environ, {
            "NIJI_CLOUD_PROVIDER": "nvidia",
            "NIJI_CLOUD_BASE_URL": "https://provider.example/v1",
            "NIJI_CLOUD_PROVIDER_API_KEY": "test-secret",
        }, clear=False), patch("niji.cloud_models.urlopen", side_effect=URLError("secret header leaked")):
            catalog = cloud_models.public_model_catalog()
        self.assertEqual(len(catalog), 2)
        self.assertNotIn("secret", json.dumps(catalog).lower())


if __name__ == "__main__":
    unittest.main()
