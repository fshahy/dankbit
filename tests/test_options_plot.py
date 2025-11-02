import pytest

try:
    import matplotlib
    matplotlib.use('Agg')
    import numpy as np
    from my_addons.dankbit.controllers.options import OptionStrat
    ODOO_AVAILABLE = True
except Exception:
    ODOO_AVAILABLE = False


def test_optionstrat_plot_smoke():
    if not ODOO_AVAILABLE:
        pytest.skip("Odoo not available in test environment; skipping smoke test")

    S0 = 100000
    obj = OptionStrat("TEST", S0, 90000, 110000, 1000)
    # add a simple position
    obj.long_call(100000, 0.01, Q=1)

    STs = obj.STs
    market_deltas = np.zeros_like(STs, dtype=float)
    market_gammas = np.zeros_like(STs, dtype=float)

    # call plot (should not raise)
    fig = obj.plot(S0, market_deltas, market_gammas, "taker", show_red_line=False, timeframe="TEST", width=6, height=3)
    assert fig is not None
    # close
    import matplotlib.pyplot as plt
    plt.close(fig)
