"""제품 저장소 공개 범위 검사. 설치 파일·접속 정보·로컬 기록 제외."""
from pathlib import PurePosixPath
import subprocess
import sys

ALLOWED_ROOTS = {"apps", "services", "contracts", "scripts", "configs", "tests", ".github", ".vscode"}
ALLOWED_FILES = {"README.md", ".gitignore", ".editorconfig", ".gitattributes", "DangUBI.code-workspace", "docs/USAGE.md", "Dockerfile", ".dockerignore"}
FORBIDDEN_PARTS = {"node_modules", ".venv", ".local", "__pycache__", "certs", "secrets", "dist", ".codex", ".agents"}


def allowed(name: str) -> bool:
    path = PurePosixPath(name)
    if set(path.parts) & FORBIDDEN_PARTS or path.name == "AGENTS.md":
        return False
    if path.suffix.lower() in {".pem", ".key", ".crt", ".pfx", ".p12", ".sqlite3"}:
        return False
    if path.name.startswith(".env") and path.name != ".env.example":
        return False
    return name in ALLOWED_FILES or path.parts[0] in ALLOWED_ROOTS


def main():
    files = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    invalid = [name for name in files if name and not allowed(name)]
    if invalid:
        print("공개 범위에서 제외해야 하는 파일:\n" + "\n".join(invalid))
        return 1
    print("제품 저장소 공개 범위 검사 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
