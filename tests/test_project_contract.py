from __future__ import annotations

import importlib
import io
import json
import pathlib
import sys
import tomllib
import unittest
from contextlib import redirect_stdout

from langgraph_demo.cli import main


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    def test_cli_list_works_without_model_configuration(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            main(["list"])
        text = output.getvalue()
        self.assertIn("basic", text)
        self.assertIn("react", text)
        self.assertIn("真实模型", text)

    def test_langgraph_json_targets_are_importable_graphs(self) -> None:
        config = json.loads((PROJECT_ROOT / "langgraph.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(config["graphs"]), 10)
        for name, target in config["graphs"].items():
            path_text, attribute = target.split(":", maxsplit=1)
            module_name = (
                path_text.removeprefix("./src/")
                .removesuffix(".py")
                .replace("/", ".")
            )
            module = importlib.import_module(module_name)
            graph = getattr(module, attribute)
            with self.subTest(graph=name):
                self.assertTrue(callable(graph.invoke))
                self.assertTrue(callable(graph.stream))

    def test_pyproject_pins_reference_versions(self) -> None:
        with (PROJECT_ROOT / "pyproject.toml").open("rb") as file:
            data = tomllib.load(file)
        dependencies = {
            item.split("==", maxsplit=1)[0]: item.split("==", maxsplit=1)[1]
            for item in data["project"]["dependencies"]
            if "==" in item
        }
        expected = {
            "langgraph": "0.3.34",
            "langgraph-checkpoint": "2.1.0",
            "langgraph-prebuilt": "0.1.8",
            "langchain-core": "0.3.66",
            "langchain-openai": "0.3.25",
            "langsmith": "0.4.2",
            "python-dotenv": "1.1.1",
        }
        for package, version in expected.items():
            with self.subTest(package=package):
                self.assertEqual(dependencies[package], version)

    def test_supported_python_version(self) -> None:
        self.assertGreaterEqual(sys.version_info[:2], (3, 11))
        self.assertLess(sys.version_info[:2], (3, 14))


if __name__ == "__main__":
    unittest.main()
