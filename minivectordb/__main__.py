"""Allow ``python -m minivectordb`` to start the server."""

import sys

from .server import main

if __name__ == "__main__":
    sys.exit(main())
