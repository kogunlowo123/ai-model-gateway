"""The error taxonomy, and the exit codes it maps onto.

Exit codes are part of this tool's interface, because the thing that reads them
is a CI pipeline rather than a person:

===== ==========================================================================
Code  Meaning
===== ==========================================================================
0     The gate held
1     Usage error -- bad arguments, a missing file, a malformed config
2     A gate failed, **or** the run refused to report a number
3     Could not run at all
===== ==========================================================================

Two of those need justifying.

**A refusal exits 2, the same as a failure.** ``RefusalError`` is raised when the
measurement cannot be trusted -- a replay that did not reproduce, a fit and a
measurement workload that overlap, a simulation that did not drain. "Not
measured" is not "passed", and a pipeline that treats a refusal as success is a
pipeline that goes green on the days the evidence is missing.

**A usage error exits 1, not 2.** ``argparse`` exits 2 by default, which would
be indistinguishable from a failed gate: a misspelt flag would look exactly like
a routing regression. ``_Parser`` in :mod:`amg.cli` overrides that, and an
end-to-end test pins it, because nothing running in-process can see the code the
operating system actually receives.
"""

from __future__ import annotations

from typing import Final

EXIT_OK: Final[int] = 0
EXIT_USAGE: Final[int] = 1
EXIT_GATE_FAILED: Final[int] = 2
EXIT_UNAVAILABLE: Final[int] = 3


class GatewayError(Exception):
    """Base class for every error this package raises deliberately.

    Carries a *remedy* because an error message that does not say what to do
    next makes the reader search the source for the answer. Every subclass
    raised at a command-line boundary sets one.
    """

    exit_code: int = EXIT_UNAVAILABLE

    def __init__(self, message: str, *, remedy: str | None = None) -> None:
        super().__init__(message)
        self.remedy = remedy


class ConfigError(GatewayError):
    """Something the caller passed in is wrong, and only the caller can fix it."""

    exit_code = EXIT_USAGE


class GateError(GatewayError):
    """A measured quantity moved past what the committed baseline permits."""

    exit_code = EXIT_GATE_FAILED


class RefusalError(GatewayError):
    """The run declines to report a number, because the number would be wrong.

    Distinct from :class:`GateError` in meaning and identical in exit code. A
    gate failure says the system got worse; a refusal says the measurement is
    not admissible. Both must stop a pipeline, and conflating them at the exit
    code while separating them in the type is the honest arrangement: the shell
    needs one bit, the reader needs the distinction.
    """

    exit_code = EXIT_GATE_FAILED


class UpstreamError(GatewayError):
    """An upstream failed to answer.

    Not a bug: this is the condition the resilience layer exists to handle, and
    the simulator injects it on purpose. It reaches the command line only when
    every attempt has been exhausted.
    """

    exit_code = EXIT_UNAVAILABLE
