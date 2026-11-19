"""외부 업로드 없이 ECR 대상 검증·인증정보 취급·동일 이미지 전달을 확인한다."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("container_tools", Path(__file__).with_name("container.py"))
container = importlib.util.module_from_spec(spec)
spec.loader.exec_module(container)


class ECRTests(unittest.TestCase):
    def args(self):
        return container.parser().parse_args(["--context", "test", "push", "--image", "dangubi:checked",
            "--account", "123456789012", "--region", "ap-northeast-2", "--repository", "dangubi/simulator", "--tag", "git-abc123"])

    def test_upload_uses_checked_image_and_stdin_token(self):
        args = self.args()
        registry, repo = container.target(args)
        responses = ["sha256:local-image", json.dumps({"repositories":[{"repositoryUri":repo}]}),
                     "private-token", json.dumps({"imageDetails":[{"imageDigest":"sha256:uploaded"}]})]
        with patch.object(container, "output", side_effect=responses), patch.object(container, "run") as run, contextlib.redirect_stdout(io.StringIO()) as console:
            container.push(args)
        calls = run.call_args_list
        assert calls[0].kwargs["input"] == "private-token\n"
        assert calls[1].args[0][-3:] == ["tag", "sha256:local-image", f"{repo}:git-abc123"]
        assert calls[2].args[0][-2:] == ["push", f"{repo}:git-abc123"]
        assert "private-token" not in str([c.args for c in calls]) + console.getvalue()
        assert f"{repo}@sha256:uploaded" in console.getvalue()

    def test_different_repository_stops_before_login(self):
        with patch.object(container, "output", side_effect=["sha256:local", '{"repositories":[{"repositoryUri":"other"}]}']), patch.object(container, "run") as run:
            with self.assertRaises(ValueError):container.push(self.args())
        run.assert_not_called()

    def test_invalid_target_and_floating_tag_rejected(self):
        for key,value in [("account","123"),("repository","../private"),("region",";command"),("tag","latest")]:
            args=self.args()
            setattr(args,key,value)
            with self.assertRaises(ValueError):container.target(args)

    def test_command_failure_does_not_print_captured_secret(self):
        with patch.object(container.shutil,"which",return_value="tool"), patch.object(container,"push",side_effect=subprocess.CalledProcessError(1,["aws"],output="private-token",stderr="private-token")), contextlib.redirect_stderr(io.StringIO()) as console:
            result=container.main(["push","--image","local","--account","123456789012","--region","ap-northeast-2","--repository","simulator","--tag","git-test"])
        self.assertEqual(result,1)
        self.assertNotIn("private-token",console.getvalue())


if __name__ == "__main__":
    unittest.main()
