import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    def test_astrbot_config_schema_uses_supported_types(self):
        schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
        supported = {
            "int",
            "float",
            "bool",
            "string",
            "text",
            "list",
            "file",
            "object",
            "template_list",
        }
        self.assertTrue(schema)
        self.assertEqual(
            {
                key: value["type"]
                for key, value in schema.items()
                if value["type"] not in supported
            },
            {},
        )
        self.assertEqual(schema["relation_intent_keywords"]["type"], "text")
        json.loads(schema["relation_intent_keywords"]["default"])
        self.assertFalse(schema["enable_passive_group_capture"]["default"])
        self.assertEqual(schema["passive_group_filter_mode"]["default"], "whitelist")
        self.assertEqual(schema["passive_group_ids"]["default"], [])

    def test_example_env_contains_no_real_secret(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DEEPSEEK_MODEL=deepseek-v4-flash", example)
        self.assertIn("TIERMEM_RUN_LIVE_TESTS=0", example)
        self.assertNotIn("sk-", example)

    def test_public_version_and_legacy_branch_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
        self.assertIn("# TierMem v2", readme)
        self.assertIn("v1-legacy", readme)
        self.assertIn("version: v2.0.0", metadata)

    def test_plugin_page_assets_are_complete(self):
        page = ROOT / "pages" / "tiermem-console"
        for filename in ("index.html", "style.css", "app.js"):
            self.assertGreater((page / filename).stat().st_size, 100)


if __name__ == "__main__":
    unittest.main()
