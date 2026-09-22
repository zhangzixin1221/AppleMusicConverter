"""PyInstaller / packaging entry point.

Deliberately separate from ``src/amc/__main__.py``: that module uses relative
imports, so it has to be imported as part of the ``amc`` package rather than
executed as a standalone script. Pointing PyInstaller at this file (with
``--paths src``) keeps the package context intact.
"""

import multiprocessing
import sys

from amc.__main__ import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
