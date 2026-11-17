"""프로젝트 내부 도구를 사용하는 로컬 준비·실행 명령."""
import argparse
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import json

ROOT = Path(__file__).resolve().parents[1]
LOCAL = ROOT / ".local"
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
WORKER = LOCAL / ("worker.exe" if os.name == "nt" else "worker")


def run(args, cwd=ROOT, **kwargs):
    subprocess.run([str(x) for x in args], cwd=cwd, check=True, **kwargs)


def node():
    result = shutil.which("node")
    if not result:
        raise SystemExit("Node.js가 필요합니다. Node.js 공식 설치 후 다시 실행하세요.")
    return result


def npm_args():
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm:
        return [npm]
    fallback = LOCAL / "npm/package/bin/npm-cli.js"
    if not fallback.is_file():
        raise SystemExit("npm이 없습니다. 먼저 prepare를 실행하세요.")
    return [node(), str(fallback)]


def prepare():
    LOCAL.mkdir(exist_ok=True)
    node()
    if not shutil.which("go"):
        raise SystemExit("Go 설치가 필요합니다.")
    if not PYTHON.is_file():
        run([sys.executable, "-m", "venv", ROOT / ".venv"])
    run([PYTHON, "-m", "pip", "install", "-r", ROOT / "services/control/requirements.lock.txt"])
    if not shutil.which("npm.cmd" if os.name == "nt" else "npm") and not (LOCAL / "npm/package/bin/npm-cli.js").is_file():
        with urllib.request.urlopen("https://registry.npmjs.org/npm/12.2.2") as response:
            info = json.load(response)
        urllib.request.urlretrieve(info["dist"]["tarball"], LOCAL / "npm.tgz")
        with tarfile.open(LOCAL / "npm.tgz") as archive:
            archive.extractall(LOCAL / "npm", filter="data")
    run(npm_args() + ["ci"], cwd=ROOT / "apps/web")
    build()


def build():
    LOCAL.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["GOCACHE"] = str(LOCAL / "go-cache")
    env["GOMODCACHE"] = str(LOCAL / "go-mod")
    run(["go", "build", "-o", WORKER, "."], cwd=ROOT / "services/worker", env=env)
    run(npm_args() + ["run", "build"], cwd=ROOT / "apps/web")


def main():
    parser = argparse.ArgumentParser(description="DangUBI 로컬 첫 실행본")
    parser.add_argument("action", choices=["prepare", "build", "run", "test"])
    args = parser.parse_args()
    if args.action == "prepare":
        prepare()
    elif args.action == "build":
        build()
    elif args.action == "test":
        env = os.environ.copy()
        env["GOCACHE"] = str(LOCAL / "go-cache")
        env["GOMODCACHE"] = str(LOCAL / "go-mod")
        run(["go", "test", "./..."], cwd=ROOT / "services/worker", env=env)
        run([PYTHON, "-m", "pytest", "services/control/test_app.py", "-q"])
        run([PYTHON, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_measure_mqtt.py"])
    else:
        if not WORKER.is_file() or not (ROOT / "apps/web/dist/index.html").is_file():
            raise SystemExit("먼저 python scripts/dev.py prepare를 실행하세요.")
        run([PYTHON, "-m", "uvicorn", "services.control.app:app", "--host", "127.0.0.1", "--port", "8080"])


if __name__ == "__main__":
    main()
