import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import mineru_runner


class MinerURunnerTests(unittest.TestCase):
    def test_discovers_registered_conda_environment_outside_path(self):
        with tempfile.TemporaryDirectory() as td, patch("mineru_runner.shutil.which", return_value=None), patch("mineru_runner.Path.home", return_value=Path(td)):
            root = Path(td)
            exe = root / "custom installation" / "env" / "Scripts" / "mineru.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            (root / ".conda").mkdir()
            (root / ".conda" / "environments.txt").write_text(str(exe.parent.parent) + "\n", encoding="utf-8")
            self.assertEqual(mineru_runner._resolve_executable()[0], str(exe))
            # A typo in an explicit selection must not silently select another install.
            self.assertIsNone(mineru_runner._resolve_executable(str(root / "missing.exe"))[0])

    def test_conda_model_settings_reach_detection_and_parser(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            exe = root / "env" / "Scripts" / "mineru.exe"
            exe.parent.mkdir(parents=True)
            exe.touch()
            metadata = exe.parent.parent / "conda-meta"
            metadata.mkdir()
            variables = {"MINERU_MODEL_SOURCE": "local", "MINERU_TOOLS_CONFIG_JSON": str(root / "mineru.json")}
            (metadata / "state").write_text(json.dumps({"env_vars": variables}), encoding="utf-8")
            source = root / "paper.pdf"
            source.write_bytes(b"%PDF")
            original = dict(os.environ)
            def fake_run(cmd, **kwargs):
                for key, value in variables.items():
                    self.assertEqual(kwargs["env"][key], value)
                self.assertEqual(kwargs["env"]["PYTHONIOENCODING"], "utf-8")
                self.assertIn(str(exe.parent), kwargs["env"]["PATH"].split(os.pathsep))
                if "-o" in cmd:
                    (Path(cmd[cmd.index("-o") + 1]) / "full.md").write_text("# Parsed", encoding="utf-8")
                return mineru_runner.subprocess.CompletedProcess(cmd, 0, "mineru 3.4.5", "")
            def observed(cmd, directory, env, *args):
                return fake_run(cmd, env=env)
            with patch("mineru_runner.subprocess.run", side_effect=fake_run), patch("mineru_runner.run_observed", side_effect=observed):
                self.assertTrue(mineru_runner.detect_mineru_cli(str(exe)).found)
                folder = mineru_runner.parse_with_local_cli(source, output_root=root / "out", executable=str(exe))
                self.assertTrue((folder / "full.md").is_file())
            self.assertEqual(dict(os.environ), original)

    def test_locate_output_prefers_full_md(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "nested").mkdir()
            (root / "nested" / "other.md").write_text("other", encoding="utf-8")
            (root / "deep").mkdir()
            (root / "deep" / "full.md").write_text("full", encoding="utf-8")
            self.assertEqual(mineru_runner.locate_mineru_output_folder(root), root / "deep")

    def test_local_cli_uses_pipeline_backend_when_requested(self):
        calls = []

        def fake_run(cmd, *args, **kwargs):
            calls.append(cmd)
            out_dir = Path(cmd[cmd.index("-o") + 1])
            (out_dir / "result").mkdir(parents=True)
            (out_dir / "result" / "full.md").write_text("# Parsed", encoding="utf-8")

            class Result:
                returncode = 0
                stdout = "ok"
                stderr = ""

            return Result()

        with tempfile.TemporaryDirectory() as td, patch("mineru_runner._resolve_executable", return_value=("mineru", "mineru")), patch(
            "mineru_runner.run_observed", fake_run
        ), patch("mineru_runner.parser_signature", return_value=({"engine": "test"}, True)):
            source = Path(td) / "paper.pdf"
            source.write_bytes(b"%PDF")
            folder = mineru_runner.parse_with_local_cli(source, output_root=Path(td) / "out", backend="pipeline")
            self.assertEqual((folder / "full.md").read_text(encoding="utf-8"), "# Parsed")
            self.assertIn("-b", calls[0])
            self.assertIn("pipeline", calls[0])

    def test_local_cli_missing_executable_has_clear_error(self):
        with tempfile.TemporaryDirectory() as td, patch("mineru_runner._resolve_executable", return_value=(None, "")):
            source = Path(td) / "paper.pdf"
            source.write_bytes(b"%PDF")
            with self.assertRaisesRegex(mineru_runner.MinerURunnerError, "not found|was not found"):
                mineru_runner.parse_with_local_cli(source)

    def test_api_file_parse_json_markdown(self):
        class Response:
            headers = {"content-type": "application/json"}
            content = b'{"markdown":"# Parsed"}'
            text = content.decode("utf-8")
            status_code = 200

            def raise_for_status(self):
                return None

            def json(self):
                return json.loads(self.text)

        class Session:
            def post(self, endpoint, headers=None, files=None, timeout=None):
                self.endpoint = endpoint
                self.headers = headers
                return Response()

        with tempfile.TemporaryDirectory() as td, patch("requests.Session", return_value=Session()):
            source = Path(td) / "paper.pdf"
            source.write_bytes(b"%PDF")
            folder = mineru_runner.parse_with_api(source, "https://mineru.example/api", api_key="secret", output_root=Path(td) / "out")
            self.assertEqual((folder / "full.md").read_text(encoding="utf-8"), "# Parsed")


if __name__ == "__main__":
    unittest.main()
