import pytest

from brewski.thermal import (
    ThermalModel,
    coast_rise_for,
    fit_startup_quad,
    predicted_crest,
)


def test_cold_start_is_a_parabola_and_a_shorter_coast_crests_lower():
    samples = [(float(t), 70.0 + 0.01 * t * t) for t in range(21)]
    assert fit_startup_quad(samples) == pytest.approx(0.01)

    model = ThermalModel(
        cold_start_seconds=20,
        cold_start_quad=0.01,
        linear_rate_f_per_sec=0.4,
        coast_rise_f=8,
        coast_seconds=20,
        coast_quad=-8 / 400,
    )
    assert coast_rise_for(model, 40) == pytest.approx(8)
    assert predicted_crest(100, 40, 0, model) == pytest.approx(108)
    # Half the shutoff slope stores a quarter of the coast rise.
    assert coast_rise_for(model, 10) == pytest.approx(2)
