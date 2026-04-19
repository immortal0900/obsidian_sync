"""VersionVector 단위 테스트.

HLC strict increase, 시간 역행 방어, 5가지 VectorOrdering,
merge, trim(28) 동작을 100% 커버리지로 검증한다.
"""
import time

from src.version_vector import VectorOrdering, VersionVector


class TestEmpty:
    """empty() 팩토리 메서드."""

    def test_empty_has_no_counters(self):
        v = VersionVector.empty()
        assert v.counters == {}

    def test_empty_is_falsy(self):
        assert not VersionVector.empty()

    def test_non_empty_is_truthy(self):
        v = VersionVector({"dev12345": 100})
        assert v


class TestUpdate:
    """update() — HLC 갱신 로직."""

    def test_first_update_uses_timestamp(self):
        v = VersionVector.empty()
        now = 1700000000.0  # 고정 시각
        result = v.update("device_abc", now=now)
        prefix = "device_a"
        assert result.counters[prefix] == 1700000000000

    def test_update_strict_increase(self):
        """update() 2회 연속 호출 시 value strict increase."""
        v = VersionVector.empty()
        now = 1700000000.0
        v1 = v.update("dev12345", now=now)
        # 같은 시각에 다시 호출 — max(existing+1, ts_ms)로 증가 보장
        v2 = v1.update("dev12345", now=now)
        assert v2.counters["dev12345"] > v1.counters["dev12345"]

    def test_update_clock_skew_backward(self):
        """시간 역행 시에도 counter 증가."""
        v = VersionVector.empty()
        v1 = v.update("dev12345", now=1700000001.0)
        # 시간이 과거로 돌아감
        v2 = v1.update("dev12345", now=1700000000.0)
        assert v2.counters["dev12345"] > v1.counters["dev12345"]

    def test_update_clock_skew_forward(self):
        """시간이 미래로 점프하면 미래 시각 사용."""
        v = VersionVector.empty()
        v1 = v.update("dev12345", now=1700000000.0)
        v2 = v1.update("dev12345", now=1800000000.0)
        assert v2.counters["dev12345"] == 1800000000000

    def test_update_preserves_other_devices(self):
        """다른 기기의 counter는 보존된다."""
        v = VersionVector({"aaaaaaaa": 100, "bbbbbbbb": 200})
        result = v.update("aaaaaaaa_full_id", now=1.0)
        assert result.counters["bbbbbbbb"] == 200
        assert result.counters["aaaaaaaa"] > 100

    def test_update_uses_prefix_8chars(self):
        """device_id의 앞 8자만 prefix로 사용."""
        v = VersionVector.empty()
        result = v.update("abcdefghijklmnop", now=1.0)
        assert "abcdefgh" in result.counters
        assert len(result.counters) == 1

    def test_update_without_now_uses_real_time(self):
        """now=None이면 실제 time.time() 사용."""
        v = VersionVector.empty()
        before = int(time.time() * 1000)
        result = v.update("dev12345")
        after = int(time.time() * 1000)
        val = result.counters["dev12345"]
        assert before <= val <= after + 1

    def test_immutability(self):
        """update()는 원본을 변경하지 않는다."""
        v = VersionVector.empty()
        v.update("dev12345", now=1.0)
        assert v.counters == {}


class TestCompare:
    """compare() — 5가지 VectorOrdering."""

    def test_equal_both_empty(self):
        a = VersionVector.empty()
        b = VersionVector.empty()
        assert a.compare(b) == VectorOrdering.Equal

    def test_equal_same_counters(self):
        a = VersionVector({"dev1": 100, "dev2": 200})
        b = VersionVector({"dev1": 100, "dev2": 200})
        assert a.compare(b) == VectorOrdering.Equal

    def test_greater(self):
        a = VersionVector({"dev1": 200, "dev2": 200})
        b = VersionVector({"dev1": 100, "dev2": 200})
        assert a.compare(b) == VectorOrdering.Greater

    def test_greater_with_superset_keys(self):
        """a가 b에 없는 키를 갖고, b의 모든 키에서 >=이면 Greater."""
        a = VersionVector({"dev1": 100, "dev2": 200})
        b = VersionVector({"dev1": 100})
        assert a.compare(b) == VectorOrdering.Greater

    def test_lesser(self):
        a = VersionVector({"dev1": 100, "dev2": 200})
        b = VersionVector({"dev1": 200, "dev2": 200})
        assert a.compare(b) == VectorOrdering.Lesser

    def test_lesser_with_subset_keys(self):
        """a가 b의 부분집합 키를 갖고, 값이 같으면 Lesser."""
        a = VersionVector({"dev1": 100})
        b = VersionVector({"dev1": 100, "dev2": 200})
        assert a.compare(b) == VectorOrdering.Lesser

    def test_concurrent_greater(self):
        """양쪽 모두 상대보다 큰 키가 있고, sum이 a >= b."""
        a = VersionVector({"dev1": 300, "dev2": 100})
        b = VersionVector({"dev1": 100, "dev2": 200})
        # a: dev1 > b.dev1, b: dev2 > a.dev2 → concurrent
        # sum(a)=400, sum(b)=300 → ConcurrentGreater
        assert a.compare(b) == VectorOrdering.ConcurrentGreater

    def test_concurrent_lesser(self):
        """양쪽 모두 상대보다 큰 키가 있고, sum이 a < b."""
        a = VersionVector({"dev1": 200, "dev2": 100})
        b = VersionVector({"dev1": 100, "dev2": 300})
        # concurrent, sum(a)=300 < sum(b)=400
        assert a.compare(b) == VectorOrdering.ConcurrentLesser

    def test_concurrent_equal_sum_is_greater(self):
        """sum이 동일하면 ConcurrentGreater."""
        a = VersionVector({"dev1": 200, "dev2": 100})
        b = VersionVector({"dev1": 100, "dev2": 200})
        assert a.compare(b) == VectorOrdering.ConcurrentGreater


class TestMerge:
    """merge() — element-wise max."""

    def test_merge_empty_with_empty(self):
        a = VersionVector.empty()
        b = VersionVector.empty()
        assert a.merge(b).counters == {}

    def test_merge_takes_max(self):
        a = VersionVector({"dev1": 100, "dev2": 300})
        b = VersionVector({"dev1": 200, "dev2": 200})
        merged = a.merge(b)
        assert merged.counters == {"dev1": 200, "dev2": 300}

    def test_merge_union_of_keys(self):
        a = VersionVector({"dev1": 100})
        b = VersionVector({"dev2": 200})
        merged = a.merge(b)
        assert merged.counters == {"dev1": 100, "dev2": 200}

    def test_merge_is_commutative(self):
        a = VersionVector({"dev1": 100, "dev2": 300})
        b = VersionVector({"dev1": 200, "dev3": 400})
        assert a.merge(b).counters == b.merge(a).counters

    def test_merge_immutability(self):
        a = VersionVector({"dev1": 100})
        b = VersionVector({"dev1": 200})
        a.merge(b)
        assert a.counters == {"dev1": 100}


class TestTrim:
    """trim() — appProperties 30-key 제한 대응."""

    def test_trim_no_change_when_under_limit(self):
        v = VersionVector({"dev1": 100, "dev2": 200})
        trimmed = v.trim(28)
        assert trimmed is v  # 동일 인스턴스 반환

    def test_trim_exact_limit(self):
        counters = {f"dev{i:05d}": i for i in range(28)}
        v = VersionVector(counters)
        trimmed = v.trim(28)
        assert trimmed is v

    def test_trim_over_limit_keeps_top_values(self):
        counters = {f"dev{i:05d}": i * 100 for i in range(30)}
        v = VersionVector(counters)
        trimmed = v.trim(28)
        assert len(trimmed.counters) == 28
        # 가장 작은 값 2개(dev00000=0, dev00001=100)가 제거되어야 함
        assert "dev00000" not in trimmed.counters
        assert "dev00001" not in trimmed.counters
        # 가장 큰 값은 유지
        assert "dev00029" in trimmed.counters

    def test_trim_custom_max(self):
        counters = {f"d{i}": i for i in range(10)}
        v = VersionVector(counters)
        trimmed = v.trim(5)
        assert len(trimmed.counters) == 5
        # 상위 5개 유지: d5~d9
        for i in range(5, 10):
            assert f"d{i}" in trimmed.counters


class TestSerialization:
    """to_dict / from_dict — JSON 왕복."""

    def test_roundtrip(self):
        original = VersionVector({"dev12345": 1700000000000, "abcdefgh": 42})
        restored = VersionVector.from_dict(original.to_dict())
        assert restored.counters == original.counters

    def test_from_dict_none(self):
        assert VersionVector.from_dict(None) == VersionVector.empty()

    def test_from_dict_empty(self):
        assert VersionVector.from_dict({}) == VersionVector.empty()

    def test_from_dict_coerces_types(self):
        """문자열 key/value도 변환."""
        v = VersionVector.from_dict({"dev1": "100"})
        assert v.counters == {"dev1": 100}
