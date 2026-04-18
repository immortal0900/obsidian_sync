"""Version Vector (HLC) - Syncthing BEP lib/protocol/vector.go 포팅.

Hybrid Logical Clock 기반 버전 벡터로, 기기 간 인과관계를 추적한다.
각 기기의 counter는 max(existing+1, unix_ms)로 갱신되어
시계 편차를 방어하면서 total order를 강제한다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any


class VectorOrdering(Enum):
    """두 VersionVector 간 비교 결과."""

    Equal = "equal"
    Greater = "greater"
    Lesser = "lesser"
    ConcurrentGreater = "concurrent_greater"
    ConcurrentLesser = "concurrent_lesser"


@dataclass(frozen=True)
class VersionVector:
    """Syncthing BEP의 Vector 구조체 포팅. HLC 기반 immutable 버전 벡터.

    counters: device_id_prefix(8자 hex) -> HLC ms 값.
    frozen=True이므로 모든 변경 메서드는 새 인스턴스를 반환한다.
    """

    counters: dict[str, int]

    def __bool__(self) -> bool:
        """Empty vector is falsy, non-empty is truthy."""
        return bool(self.counters)

    @staticmethod
    def empty() -> VersionVector:
        """빈 버전 벡터를 생성한다."""
        return VersionVector({})

    def update(self, device_id: str, now: float | None = None) -> VersionVector:
        """HLC 갱신: max(existing+1, unix_ms).

        Syncthing lib/protocol/vector.go의 Update 로직 포팅.
        device_id의 앞 8자를 prefix로 사용한다.

        Args:
            device_id: 기기 식별자. 앞 8자가 prefix로 쓰인다.
            now: 현재 시각(초). None이면 time.time() 사용.

        Returns:
            counter가 갱신된 새 VersionVector.
        """
        ts_ms = int((now if now is not None else time.time()) * 1000)
        prefix = device_id[:8]
        existing = self.counters.get(prefix, 0)
        new_value = max(existing + 1, ts_ms)
        return VersionVector({**self.counters, prefix: new_value})

    def compare(self, other: VersionVector) -> VectorOrdering:
        """두 벡터의 인과관계를 비교한다.

        Syncthing vector.go의 Compare 로직:
        - 모든 키에서 self >= other이고 other >= self이면 Equal
        - 모든 키에서 self >= other이면 Greater
        - 모든 키에서 other >= self이면 Lesser
        - 그 외 Concurrent (sum 기반으로 Greater/Lesser 구분)
        """
        keys = set(self.counters) | set(other.counters)
        if not keys:
            return VectorOrdering.Equal

        a_ge = all(
            self.counters.get(k, 0) >= other.counters.get(k, 0) for k in keys
        )
        b_ge = all(
            other.counters.get(k, 0) >= self.counters.get(k, 0) for k in keys
        )

        if a_ge and b_ge:
            return VectorOrdering.Equal
        if a_ge:
            return VectorOrdering.Greater
        if b_ge:
            return VectorOrdering.Lesser

        # Concurrent — sum으로 Greater/Lesser 구분
        sa = sum(self.counters.values())
        sb = sum(other.counters.values())
        if sa >= sb:
            return VectorOrdering.ConcurrentGreater
        return VectorOrdering.ConcurrentLesser

    def merge(self, other: VersionVector) -> VersionVector:
        """두 벡터의 element-wise max를 반환한다 (union of max)."""
        keys = set(self.counters) | set(other.counters)
        return VersionVector(
            {
                k: max(self.counters.get(k, 0), other.counters.get(k, 0))
                for k in keys
            }
        )

    def trim(self, max_devices: int = 28) -> VersionVector:
        """appProperties 30-key 제한 대응. 예비 2슬롯(schema, deleted) 확보.

        기기 수가 max_devices를 초과하면 value가 큰 상위 N개만 유지한다.
        """
        if len(self.counters) <= max_devices:
            return self
        kept = sorted(
            self.counters.items(), key=lambda kv: kv[1], reverse=True
        )[:max_devices]
        return VersionVector(dict(kept))

    def to_dict(self) -> dict[str, int]:
        """JSON 직렬화용 딕셔너리를 반환한다."""
        return dict(self.counters)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> VersionVector:
        """딕셔너리에서 VersionVector를 생성한다.

        None이나 빈 dict이면 empty()를 반환한다.
        """
        if not data:
            return cls.empty()
        return cls({str(k): int(v) for k, v in data.items()})
