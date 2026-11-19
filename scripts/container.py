"""컨테이너 빌드와 기존 ECR 저장소 업로드. AWS 리소스는 생성하지 않는다."""
import argparse
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def run(command, **kwargs):
    return subprocess.run(command, cwd=ROOT, check=True, **kwargs)


def output(command):
    return run(command, capture_output=True, text=True, encoding="utf-8").stdout.strip()


def docker(args):
    return ["docker"] + (["--context", args.context] if args.context else [])


def build(args):
    revision = args.revision or output(["git", "rev-parse", "HEAD"])
    run(docker(args) + ["build", "--platform", args.platform, "--build-arg", f"REVISION={revision}",
                       "--tag", args.image, "--file", "Dockerfile", "."])


def target(args):
    if not re.fullmatch(r"\d{12}", args.account):
        raise ValueError("AWS 계정은 12자리 숫자여야 합니다.")
    if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d+", args.region):
        raise ValueError("AWS 리전을 확인하세요.")
    if not re.fullmatch(r"[a-z0-9]+(?:[._/-][a-z0-9]+)*", args.repository):
        raise ValueError("ECR 저장소 이름을 확인하세요.")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", args.tag) or args.tag == "latest":
        raise ValueError("latest 대신 버전 또는 커밋을 나타내는 명시적인 태그를 지정하세요.")
    suffix = "amazonaws.com.cn" if args.region.startswith("cn-") else "amazonaws.com"
    registry = f"{args.account}.dkr.ecr.{args.region}.{suffix}"
    return registry, f"{registry}/{args.repository}"


def push(args):
    registry, repository = target(args)
    aws = ["aws"] + (["--profile", args.profile] if args.profile else []) + ["--region", args.region, "--no-cli-pager"]
    # 인증 전에 로컬 이미지와 대상 저장소를 확인한다. 이름이 바뀌어도 같은 이미지 ID를 올린다.
    image_id = output(docker(args) + ["image", "inspect", args.image, "--format", "{{.Id}}"])
    detail = json.loads(output(aws + ["ecr", "describe-repositories", "--registry-id", args.account,
                                     "--repository-names", args.repository, "--output", "json"]))
    if len(detail.get("repositories", [])) != 1 or detail["repositories"][0].get("repositoryUri") != repository:
        raise ValueError("조회한 ECR 저장소가 지정한 업로드 대상과 다릅니다.")
    token = output(aws + ["ecr", "get-login-password"])
    if not token:
        raise ValueError("ECR 인증 토큰을 받지 못했습니다.")
    # 토큰은 인수·콘솔·프로젝트 파일에 기록하지 않는다. Docker의 자격증명 보관 설정을 사용한다.
    run(docker(args) + ["login", "--username", "AWS", "--password-stdin", registry],
        input=token + "\n", text=True, encoding="utf-8")
    del token
    tagged = f"{repository}:{args.tag}"
    run(docker(args) + ["tag", image_id, tagged])
    run(docker(args) + ["push", tagged])
    digest = json.loads(output(aws + ["ecr", "describe-images", "--registry-id", args.account,
                                    "--repository-name", args.repository, "--image-ids", f"imageTag={args.tag}",
                                    "--output", "json"]))["imageDetails"][0]["imageDigest"]
    print(json.dumps({"image": tagged, "digest_reference": f"{repository}@{digest}"}, ensure_ascii=False))


def parser():
    result = argparse.ArgumentParser(description="DangUBI 컨테이너 빌드·ECR 업로드")
    result.add_argument("--context", help="Docker context 이름. 기본 context 설정은 변경하지 않습니다.")
    sub = result.add_subparsers(dest="action", required=True)
    b = sub.add_parser("build", help="Linux 컨테이너 이미지 빌드")
    b.add_argument("--image", required=True)
    b.add_argument("--platform", choices=["linux/amd64", "linux/arm64"], default="linux/amd64")
    b.add_argument("--revision", help="이미지에 기록할 코드 revision. 기본은 현재 Git HEAD")
    p = sub.add_parser("push", help="검증한 이미지를 기존 private ECR 저장소에 업로드")
    p.add_argument("--image", required=True)
    p.add_argument("--account", required=True)
    p.add_argument("--region", required=True)
    p.add_argument("--repository", required=True)
    p.add_argument("--tag", required=True)
    p.add_argument("--profile", help="AWS CLI 프로필. 자격증명 값은 인수로 받지 않습니다.")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.action == "push":
            target(args)
        for name in (["docker", "git"] if args.action == "build" and not args.revision
                     else ["docker", "aws"] if args.action == "push" else ["docker"]):
            if not shutil.which(name):
                raise ValueError(f"{name} 명령이 필요합니다.")
        (build if args.action == "build" else push)(args)
    except subprocess.CalledProcessError as error:
        # get-login-password 실패 출력에도 토큰이 섞일 가능성을 피한다.
        print(f"명령 실행 실패(종료 코드 {error.returncode}). 실행 환경·권한·대상 설정을 확인하세요.", file=sys.stderr)
        return 1
    except (ValueError, OSError, KeyError, IndexError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
