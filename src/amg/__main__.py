"""Entry point for `python -m amg`, so the CLI works without the console script.

Useful in a container before the package's bin directory is on PATH, and in CI
where a wheel has been installed but the shim has not been located yet.
"""

from amg.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
