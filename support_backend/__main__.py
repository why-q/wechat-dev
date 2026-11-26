"""Module CLI entrypoint (`python -m support_backend ...`) without runpy import warnings."""

from .knowledge import main

if __name__ == "__main__":
    raise SystemExit(main())
