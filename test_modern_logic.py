import pytest

from modern_logic import run_modern_logic, MAX_LOAN_AMOUNT, MAX_INTEREST_RATE


class TestHappyPath:
    def test_basic_five_percent(self):
        assert run_modern_logic(1000.00, 5.00) == 50.00

    def test_typical_mortgage_like_values(self):
        assert run_modern_logic(250000.00, 3.75) == pytest.approx(9375.00)

    def test_small_loan_small_rate(self):
        assert run_modern_logic(100.00, 1.00) == 1.00

    def test_result_rounds_half_up(self):
        # 33.335 -> rounds to 33.34 under ROUND_HALF_UP
        assert run_modern_logic(1000.00, 3.3335) == 33.34


class TestZeroValues:
    def test_zero_loan(self):
        assert run_modern_logic(0.00, 5.00) == 0.00

    def test_zero_rate(self):
        assert run_modern_logic(1000.00, 0.00) == 0.00

    def test_zero_loan_and_rate(self):
        assert run_modern_logic(0.00, 0.00) == 0.00


class TestEdgeCases:
    def test_max_loan_amount(self):
        result = run_modern_logic(float(MAX_LOAN_AMOUNT), 1.00)
        assert result == pytest.approx(99999.9999, abs=0.01)

    def test_max_interest_rate(self):
        result = run_modern_logic(1000.00, float(MAX_INTEREST_RATE))
        assert result == pytest.approx(999.90, abs=0.01)

    def test_max_loan_and_max_rate(self):
        result = run_modern_logic(float(MAX_LOAN_AMOUNT), float(MAX_INTEREST_RATE))
        assert result == pytest.approx(9998999.99, abs=0.01)

    def test_fractional_cent_rounds_correctly(self):
        # 100 * (0.005) = 0.5 exactly -> 0.50, no drift
        assert run_modern_logic(100.00, 0.50) == 0.50

    def test_no_floating_point_drift(self):
        # Classic float trap: 0.1 + 0.2 style drift must not appear
        result = run_modern_logic(1010.10, 10.10)
        assert result == pytest.approx(102.0201, abs=0.01)

    def test_negative_loan_raises(self):
        with pytest.raises(ValueError):
            run_modern_logic(-100.00, 5.00)

    def test_negative_rate_raises(self):
        with pytest.raises(ValueError):
            run_modern_logic(100.00, -5.00)

    def test_loan_over_capacity_raises(self):
        with pytest.raises(ValueError):
            run_modern_logic(10000000.00, 5.00)

    def test_rate_over_capacity_raises(self):
        with pytest.raises(ValueError):
            run_modern_logic(1000.00, 100.00)

    def test_return_type_is_float(self):
        assert isinstance(run_modern_logic(1000.00, 5.00), float)
