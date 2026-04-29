"""Entry point so `python -m src.podcast` keeps working post-split."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
