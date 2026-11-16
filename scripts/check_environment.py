"""설치 여부만 확인하며 환경 설정이나 보안 정책을 변경하지 않습니다."""

import shutil
import sys
from pathlib import Path


def main() -> int:
    required = ("git", "go", "python", "node", "npm")
    missing = []
    for name in required:
        found = shutil.which(name) is not None
        if name == "npm" and not found:
            found = (Path(__file__).resolve().parents[1] / ".local/npm/package/bin/npm-cli.js").is_file() and shutil.which("node") is not None
        print(f"{'FOUND' if found else 'MISSING'}: {name}")
        if not found:
            missing.append(name)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
