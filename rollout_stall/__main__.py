"""Allow ``python -m rollout_stall``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
