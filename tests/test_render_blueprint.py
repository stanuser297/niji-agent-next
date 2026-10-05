import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


class RenderBlueprintTests(unittest.TestCase):
    def test_blueprint_is_valid_and_deploys_the_authenticated_api(self):
        blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
        self.assertIsInstance(blueprint, dict)
        self.assertEqual(len(blueprint["services"]), 2)
        service = next(item for item in blueprint["services"] if item["name"] == "niji-cloud-api")
        worker = next(item for item in blueprint["services"] if item["name"] == "niji-cloud-worker")
        self.assertEqual(service["type"], "web")
        self.assertEqual(service["runtime"], "python")
        self.assertEqual(service["region"], "singapore")
        self.assertEqual(service["numInstances"], 1)
        self.assertIn(".[cloud]", service["buildCommand"])
        self.assertEqual(service["startCommand"], "python -m niji.cloud_api")
        self.assertEqual(service["healthCheckPath"], "/healthz")
        self.assertEqual(worker["type"], "worker")
        self.assertEqual(worker["runtime"], "python")
        self.assertEqual(worker["startCommand"], "python -m niji.cloud_worker")
        self.assertEqual(worker["region"], service["region"])

    def test_api_uses_private_render_postgres_not_a_single_service_disk(self):
        blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
        service = blueprint["services"][0]
        database = blueprint["databases"][0]
        database_env = next(item for item in service["envVars"]
                            if item["key"] == "NIJI_CLOUD_DATABASE_URL")
        self.assertEqual(database["name"], "niji-cloud-db")
        self.assertEqual(database["ipAllowList"], [])
        self.assertEqual(database_env["fromDatabase"]["name"], database["name"])
        self.assertEqual(database_env["fromDatabase"]["property"], "connectionString")
        self.assertNotIn("disk", service)

    def test_api_and_worker_share_explicit_usage_quotas(self):
        blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
        services = {item["name"]: item for item in blueprint["services"]}
        api_env = {item["key"]: item for item in services["niji-cloud-api"]["envVars"]}
        worker_env = {item["key"]: item for item in services["niji-cloud-worker"]["envVars"]}
        expected = {
            "NIJI_CLOUD_MAX_MONTHLY_PROMPT_TOKENS": "2000000",
            "NIJI_CLOUD_MAX_MONTHLY_COMPLETION_TOKENS": "160000",
            "NIJI_CLOUD_RUN_PROMPT_TOKEN_CAP": "200000",
            "NIJI_CLOUD_RUN_COMPLETION_TOKEN_CAP": "16384",
        }
        for key, value in expected.items():
            self.assertEqual(api_env[key]["value"], value)
            self.assertEqual(worker_env[key]["value"], value)

    def test_blueprint_fails_closed_on_missing_oidc_settings(self):
        blueprint = yaml.safe_load((ROOT / "render.yaml").read_text(encoding="utf-8"))
        service = next(item for item in blueprint["services"] if item["name"] == "niji-cloud-api")
        worker = next(item for item in blueprint["services"] if item["name"] == "niji-cloud-worker")
        env = {item["key"]: item for item in service["envVars"]}
        self.assertEqual(env["NIJI_CLOUD_AUTH_MODE"]["value"], "oidc")
        for key in ("NIJI_CLOUD_OIDC_ISSUER", "NIJI_CLOUD_OIDC_AUDIENCE", "NIJI_CLOUD_OIDC_JWKS_URL"):
            self.assertIs(env[key]["sync"], False)
            self.assertNotIn("value", env[key])
        self.assertNotIn("NIJI_CLOUD_API_TOKEN", env)
        self.assertNotIn("NIJI_CLOUD_TENANT_ID", env)
        worker_env = {item["key"]: item for item in worker["envVars"]}
        self.assertEqual(worker_env["NIJI_CLOUD_WORKER_TENANT_MODE"]["value"], "all")
        self.assertEqual(service["autoDeployTrigger"], "checksPass")


if __name__ == "__main__":
    unittest.main()
