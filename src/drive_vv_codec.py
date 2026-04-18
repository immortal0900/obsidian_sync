"""Drive appProperties ↔ VersionVector 변환 코덱.

Google Drive 파일의 appProperties에 Version Vector를 저장/로드한다.
appProperties는 key+value 합계 124바이트, 최대 30개 key 제한이 있다.

스키마:
    ot_sync_schema:       "v2"
    ot_sync_deleted:      "0" | "1"
    ot_sync_md5:          "<hex>"  (None이면 key 자체를 생략)
    ot_sync_vv_<prefix>:  "<HLC_ms>"   (최대 28개)
"""
from __future__ import annotations

from src.version_vector import VersionVector

# appProperties key 접두사
KEY_SCHEMA = "ot_sync_schema"
KEY_DELETED = "ot_sync_deleted"
KEY_MD5 = "ot_sync_md5"
KEY_VV_PREFIX = "ot_sync_vv_"

# 현재 스키마 버전
SCHEMA_VERSION = "v2"

# 예약 슬롯: schema, deleted, md5 → 기기용 최대 27개 (안전 마진)
# spec.md는 28로 명시하나 md5 key 추가로 27이 실 한도
MAX_DEVICE_KEYS = 28


def encode(
    vv: VersionVector,
    deleted: bool = False,
    md5: str | None = None,
) -> dict[str, str]:
    """VersionVector + 메타데이터를 appProperties dict로 인코딩한다.

    - vv가 MAX_DEVICE_KEYS를 초과하면 자동 trim.
    - md5가 None이면 ot_sync_md5 key 생략.

    Returns:
        Drive files.create/update의 body.appProperties에 넣을 dict.
    """
    trimmed = vv.trim(MAX_DEVICE_KEYS)

    props: dict[str, str] = {
        KEY_SCHEMA: SCHEMA_VERSION,
        KEY_DELETED: "1" if deleted else "0",
    }

    if md5 is not None:
        props[KEY_MD5] = md5

    for prefix, hlc_ms in trimmed.counters.items():
        props[f"{KEY_VV_PREFIX}{prefix}"] = str(hlc_ms)

    return props


def decode(
    app_properties: dict[str, str] | None,
) -> tuple[VersionVector, bool, str | None]:
    """appProperties dict에서 VersionVector + 메타데이터를 디코딩한다.

    - ot_sync_schema가 없거나 "v2"가 아니면 legacy 취급 (empty vector).
    - 파싱 실패 시 silent fallback: (empty, False, None).

    Returns:
        (VersionVector, deleted, md5)
    """
    if not app_properties:
        return VersionVector.empty(), False, None

    schema = app_properties.get(KEY_SCHEMA)
    if schema != SCHEMA_VERSION:
        # legacy 파일 또는 스키마 미지원 → empty
        return VersionVector.empty(), False, None

    # deleted
    deleted = app_properties.get(KEY_DELETED, "0") == "1"

    # md5
    md5 = app_properties.get(KEY_MD5)

    # version vector counters
    counters: dict[str, int] = {}
    for key, value in app_properties.items():
        if key.startswith(KEY_VV_PREFIX):
            prefix = key[len(KEY_VV_PREFIX):]
            try:
                counters[prefix] = int(value)
            except (ValueError, TypeError):
                # 손상된 값 → 해당 기기 카운터 무시
                continue

    return VersionVector(counters), deleted, md5
