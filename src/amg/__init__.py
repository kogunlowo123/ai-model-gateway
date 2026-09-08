"""A model gateway that treats its routing policy as a fitted model.

The public surface is small on purpose:

* :mod:`amg.gateway` -- the routing core, shared by the HTTP server and the
  replay harness, so a published counterfactual describes the gateway that is
  actually running.
* :mod:`amg.routing` -- the policies, the fitted estimator behind one of them,
  and the threshold calibration that puts every policy at a matched price.
* :mod:`amg.replay` -- running a whole workload through a policy, and proving
  the run reproduces.
* :mod:`amg.evaluate` -- what the numbers in the README are computed by.

Everything on the measurement path is standard library, so the figures this
project publishes do not move when a dependency releases.
"""

from amg.errors import (
    EXIT_GATE_FAILED,
    EXIT_OK,
    EXIT_UNAVAILABLE,
    EXIT_USAGE,
    ConfigError,
    GateError,
    GatewayError,
    RefusalError,
)

__all__ = [
    "EXIT_GATE_FAILED",
    "EXIT_OK",
    "EXIT_UNAVAILABLE",
    "EXIT_USAGE",
    "ConfigError",
    "GateError",
    "GatewayError",
    "RefusalError",
]
