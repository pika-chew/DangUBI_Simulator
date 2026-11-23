"""상시 운영 차량의 가상 VIN.

형식 근거: 49 CFR 565.15(17자리, I·O·Q 제외, 9번째 검사 숫자, 10번째 연식,
승용차 7번째 영문자, 13~17번째 숫자). 실제 차량과 겹치지 않도록 지역 코드 표에
없는 첫 글자 '0'의 가상 WMI를 쓴다. 미배정을 완전히 보증하지 않으며 근거·한계는
문서에 기록한다. 원본 VIN은 내부 명부에만 두고 외부 전송에는 가명 VIN을 쓴다.
"""
import hashlib
import random

ALPHABET = "0123456789ABCDEFGHJKLMNPRSTUVWXYZ"
LETTERS = "ABCDEFGHJKLMNPRSTUVWXYZ"
TRANSLITERATION = {**{str(n): n for n in range(10)},
                   "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
                   "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
                   "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9}
WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
MODEL_YEARS = {2016: "G", 2017: "H", 2018: "J", 2019: "K", 2020: "L", 2021: "M",
               2022: "N", 2023: "P", 2024: "R", 2025: "S", 2026: "T", 2027: "V"}
VIRTUAL_WMI = "0DS"
PLANT = "S"          # 상시 운영
TEST_PLANT = "T"     # 시험 실행: 공장 코드가 달라 운영 VIN과 절대 같지 않다.
SERIAL_MAX = 999_999


def check_digit(vin: str) -> str:
    """9번째 자리를 제외한 가중합을 11로 나눈 나머지. 10이면 X."""
    if len(vin) != 17 or any(c not in ALPHABET for c in vin.upper()):
        raise ValueError("VIN은 I·O·Q를 제외한 영문자·숫자 17자리여야 합니다.")
    total = sum(TRANSLITERATION[c] * w for c, w in zip(vin.upper(), WEIGHTS))
    remainder = total % 11
    return "X" if remainder == 10 else str(remainder)


def is_valid(vin: str) -> bool:
    try:
        return vin == vin.upper() and vin[8] == check_digit(vin)
    except ValueError:
        return False


def build(serial: int, model_year: int, chooser: random.Random, wmi: str = VIRTUAL_WMI, plant: str = PLANT) -> str:
    """명부 일련번호로 VIN을 만든다. 일련번호가 다르면 VIN도 반드시 다르다."""
    if not 1 <= serial <= SERIAL_MAX:
        raise ValueError(f"VIN 일련번호는 1~{SERIAL_MAX:,} 범위여야 합니다.")
    if model_year not in MODEL_YEARS:
        raise ValueError("지원하지 않는 연식입니다.")
    if len(wmi) != 3 or len(plant) != 1 or any(c not in ALPHABET for c in wmi + plant):
        raise ValueError("WMI와 공장 코드는 VIN 허용 문자여야 합니다.")
    # 4~8자리 차량 특성: 7번째는 연식 2010~2039 구간을 뜻하는 영문자.
    descriptor = "".join(chooser.choice(ALPHABET) for _ in range(3)) + chooser.choice(LETTERS) + chooser.choice(ALPHABET)
    body = f"{wmi}{descriptor}0{MODEL_YEARS[model_year]}{plant}{serial:06d}"
    return body[:8] + check_digit(body) + body[9:]


def mask(vin: str) -> str:
    """화면·응답 기본 표시용. 원본 VIN 대부분을 가린다."""
    return "*" * 13 + vin[-4:]


def trial_vin(index: int) -> str:
    """시험 실행 차량의 가상 VIN. 가명처리하지 않고 그대로 전송한다(시험용)."""
    chooser = random.Random(int.from_bytes(hashlib.sha256(f"test-vin|{index}".encode()).digest(), "big"))
    return build(index, chooser.randint(2016, 2026), chooser, plant=TEST_PLANT)


def trial_vins(vehicle_ids) -> dict:
    """내부 차량 ID(정렬 순서) → 시험 VIN. 생성 키는 내부 ID를 그대로 써 기존 재현성을 유지한다."""
    return {v: trial_vin(i + 1) for i, v in enumerate(sorted(vehicle_ids))}

