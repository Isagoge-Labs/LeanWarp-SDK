import pytest
from leanwarp_cloud.amounts import usd_spending_cap


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", 0),
        ("2", 2_000_000),
        ("0.000001", 1),
        ("2.000001", 2_000_001),
        ("1000000.000000", 10**12),
    ],
)
def test_exact_usd_cap(value: str, expected: int) -> None:
    assert usd_spending_cap(value) == expected


@pytest.mark.parametrize(
    "value", ["-1", "nan", "inf", "1e2", "00", "1.0000001", "1000000.000001", ""]
)
def test_cap_never_rounds_or_accepts_non_decimal_input(value: str) -> None:
    with pytest.raises(ValueError):
        usd_spending_cap(value)
