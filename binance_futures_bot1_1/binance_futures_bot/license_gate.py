"""
license_gate.py  v2.0 — ECDSA 비대칭 라이선스 검증
────────────────────────────────────────────────────
보안 구조:
  개인키(private key) → 개발자 PC에만 존재  → 키 서명(발급)
  공개키(public key)  → 클라이언트에 내장   → 서명 검증만 가능

클라이언트가 소스코드를 전부 열람해도
공개키로는 서명 위조가 수학적으로 불가능합니다.
(ECDSA P-256 기준: 개인키 없이 위조 = 사실상 불가능)

키 형식:
  BOT1-{base64url(payload)}.{base64url(signature)}

payload (JSON):
  { "feature": "NEURAL", "expiry": 1234567890, "uid": "random8hex" }

발급 (개발자 PC에서만):
  python license_gate.py --gen-key          # 키쌍 1회 생성 (처음 한 번만)
  python license_gate.py --issue 365        # 365일짜리 라이선스 발급

검증 (클라이언트):
  from license_gate import validate_key
  ok, msg = validate_key("BOT1-...", "NEURAL")
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
from datetime import datetime, timezone
from typing import Tuple

# ── 공개키 (클라이언트 내장 — 개인키 없이는 위조 불가) ─────────────────────
# 처음 실행 시 --gen-key로 생성된 값이 여기에 자동 기록됩니다.
# 이 값은 공개되어도 안전합니다.
_PUBLIC_KEY_PEM = """
-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAESEL7t0J8lFqwg45G2PGJ88m3i18n
YxiQxPEA/sR0IT2b6Pk0J1Gz0NcXuArczs5r1M5mXQBAhpYboO5YbTKRSw==
-----END PUBLIC KEY-----
"""

# 개인키 경로 (배포 파일에 절대 포함 금지)
_PRIVATE_KEY_PATH = os.path.expanduser("~/.botkeys/neural_scorer_private.pem")
_PUBLIC_KEY_PATH  = os.path.expanduser("~/.botkeys/neural_scorer_public.pem")

FEATURE = "NEURAL"


# ─────────────────────────────────────────────────────────────────────────────
# 내부 유틸
# ─────────────────────────────────────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def _b64url_decode(s: str) -> bytes:
    pad = 4 - len(s) % 4
    return base64.urlsafe_b64decode(s + "=" * (pad % 4))

def _load_public_key():
    from cryptography.hazmat.primitives.serialization import load_pem_public_key
    pem = _PUBLIC_KEY_PEM.strip().encode()
    if b"PLACEHOLDER" in pem:
        raise RuntimeError(
            "공개키가 설정되지 않았습니다.\n"
            "개발자 PC에서 'python license_gate.py --gen-key' 를 실행한 후\n"
            "_PUBLIC_KEY_PEM 값을 이 파일에 붙여넣으세요."
        )
    return load_pem_public_key(pem)

def _load_private_key():
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    if not os.path.exists(_PRIVATE_KEY_PATH):
        raise FileNotFoundError(
            f"개인키 파일 없음: {_PRIVATE_KEY_PATH}\n"
            f"'python license_gate.py --gen-key' 로 키를 먼저 생성하세요."
        )
    with open(_PRIVATE_KEY_PATH, "rb") as f:
        return load_pem_private_key(f.read(), password=None)


# ─────────────────────────────────────────────────────────────────────────────
# 공개 API
# ─────────────────────────────────────────────────────────────────────────────
def generate_key(feature: str = "NEURAL", days: int = 365) -> str:
    """
    라이선스 키 발급 (개인키 필요 — 개발자 PC에서만 실행 가능).
    """
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.hashes import SHA256

    private_key = _load_private_key()
    payload_dict = {
        "feature": feature.upper(),
        "expiry":  int(time.time()) + days * 86400,
        "uid":     os.urandom(6).hex(),
    }
    payload_bytes = json.dumps(payload_dict, separators=(",", ":")).encode()
    signature     = private_key.sign(payload_bytes, ECDSA(SHA256()))

    key = f"BOT1-{_b64url_encode(payload_bytes)}.{_b64url_encode(signature)}"
    return key


def validate_key(key: str, feature: str = "NEURAL", lang: str = "ko") -> Tuple[bool, str]:
    """
    라이선스 키 검증 (공개키만 사용 — 클라이언트에서 안전).
    Returns (is_valid, message)
    lang: "ko" or "en"
    """
    from cryptography.hazmat.primitives.asymmetric.ec import ECDSA
    from cryptography.hazmat.primitives.hashes import SHA256
    from cryptography.exceptions import InvalidSignature

    _en = lang == "en"

    if not key or not isinstance(key, str):
        return False, ("Key is empty" if _en else "키가 비어있습니다")

    key = key.strip()
    if not key.startswith("BOT1-"):
        return False, ("Invalid key format (must start with BOT1-)" if _en
                       else "키 형식이 올바르지 않습니다 (BOT1-으로 시작해야 함)")

    parts = key[5:].split(".")
    if len(parts) != 2:
        return False, ("Invalid key format (BOT1-payload.signature)" if _en
                       else "키 형식이 올바르지 않습니다 (BOT1-payload.signature)")

    try:
        payload_bytes = _b64url_decode(parts[0])
        signature     = _b64url_decode(parts[1])
    except Exception:
        return False, ("Key decoding failed" if _en else "키 디코딩 실패")

    # 1. 서명 검증 (공개키)
    try:
        pub = _load_public_key()
        pub.verify(signature, payload_bytes, ECDSA(SHA256()))
    except InvalidSignature:
        return False, ("Invalid key signature (forged or tampered)" if _en
                       else "키 서명이 유효하지 않습니다 (위조 또는 변조)")
    except RuntimeError as e:
        return False, str(e)
    except Exception as e:
        return False, (f"Verification error: {e}" if _en else f"검증 오류: {e}")

    # 2. 페이로드 파싱
    try:
        payload = json.loads(payload_bytes)
    except Exception:
        return False, ("Key data parsing failed" if _en else "키 데이터 파싱 실패")

    # 3. 기능 확인
    if payload.get("feature", "").upper() != feature.upper():
        feat = payload.get('feature')
        return False, (f"This key is for {feat} (not {feature})" if _en
                       else f"이 키는 {feat} 기능용입니다 ({feature} 아님)")

    # 4. 만료일 확인
    expiry = int(payload.get("expiry", 0))
    if time.time() > expiry:
        exp_dt = datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%d")
        return False, (f"Expired key (expired: {exp_dt} UTC)" if _en
                       else f"만료된 키입니다 (만료일: {exp_dt} UTC)")

    exp_dt = datetime.fromtimestamp(expiry, tz=timezone.utc).strftime("%Y-%m-%d")
    return True, (f"Valid (expires: {exp_dt} UTC)" if _en
                  else f"유효 (만료: {exp_dt} UTC)")


def days_remaining(key: str) -> int:
    """남은 유효 기간(일). 만료/무효 시 -1."""
    try:
        parts = key.strip()[5:].split(".")
        payload = json.loads(_b64url_decode(parts[0]))
        remaining = int(payload["expiry"]) - time.time()
        return max(-1, int(remaining / 86400))
    except Exception:
        return -1


# ─────────────────────────────────────────────────────────────────────────────
# CLI  (개발자 전용)
# ─────────────────────────────────────────────────────────────────────────────
def _cmd_gen_key():
    """ECDSA P-256 키쌍 생성 (최초 1회만 실행)."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import (
        Encoding, PrivateFormat, PublicFormat,
        NoEncryption, BestAvailableEncryption
    )

    if os.path.exists(_PRIVATE_KEY_PATH):
        print(f"⚠️  개인키가 이미 존재합니다: {_PRIVATE_KEY_PATH}")
        ans = input("덮어쓰면 기존 발급 키가 모두 무효화됩니다. 계속하시겠습니까? [y/N] ")
        if ans.strip().lower() != "y":
            print("취소됨.")
            return

    os.makedirs(os.path.dirname(_PRIVATE_KEY_PATH), exist_ok=True)
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key  = private_key.public_key()

    priv_pem = private_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_pem  = public_key.public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)

    with open(_PRIVATE_KEY_PATH, "wb") as f:
        f.write(priv_pem)
    os.chmod(_PRIVATE_KEY_PATH, 0o600)   # 소유자만 읽기

    with open(_PUBLIC_KEY_PATH, "wb") as f:
        f.write(pub_pem)

    print(f"✅ 개인키 저장: {_PRIVATE_KEY_PATH}  (배포 금지!)")
    print(f"✅ 공개키 저장: {_PUBLIC_KEY_PATH}")
    print()
    print("━" * 60)
    print("아래 공개키를 license_gate.py의 _PUBLIC_KEY_PEM 에 붙여넣으세요:")
    print("━" * 60)
    print(pub_pem.decode())


def _cmd_issue(days: int):
    """라이선스 키 발급."""
    try:
        key = generate_key("NEURAL", days=days)
        ok, msg = validate_key(key, "NEURAL")
        print(f"✅ 발급 완료 ({days}일)")
        print(f"키: {key}")
        print(f"검증: {msg}")
    except FileNotFoundError as e:
        print(f"❌ {e}")
    except Exception as e:
        print(f"❌ 발급 실패: {e}")


def _cmd_verify(key: str):
    """키 검증."""
    ok, msg = validate_key(key, "NEURAL")
    print(f"{'✅' if ok else '❌'} {msg}")
    if ok:
        print(f"남은 일수: {days_remaining(key)}일")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="라이선스 키 관리 (개발자 전용)")
    parser.add_argument("--gen-key",        action="store_true", help="ECDSA 키쌍 생성 (최초 1회)")
    parser.add_argument("--issue",  type=int, metavar="DAYS",    help="N일짜리 라이선스 발급")
    parser.add_argument("--verify", type=str, metavar="KEY",     help="키 유효성 검증")
    args = parser.parse_args()

    if args.gen_key:
        _cmd_gen_key()
    elif args.issue:
        _cmd_issue(args.issue)
    elif args.verify:
        _cmd_verify(args.verify)
    else:
        parser.print_help()
