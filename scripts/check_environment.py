"""설치 여부만 확인하며 환경 설정이나 보안 정책을 변경하지 않습니다."""

import shutil
import sys


def main() -> int:
    required = ("git", "go", "python", "node", "npm")
    missing = []
    for name in required:
        found = shutil.which(name) is not None
        print(f"{'FOUND' if found else 'MISSING'}: {name}")
        if not found:
            missing.append(name)
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
