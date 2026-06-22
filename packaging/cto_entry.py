"""PyInstaller entry script.

Importing rag.api.cli runs it as a proper module (rag.api.cli),
not __main__, so its `from ..cli.X import ...` relative imports
resolve correctly. Without this wrapper, PyInstaller invokes
cli.py as __main__ and every relative import inside the function
bodies fails with ImportError.
"""

from rag.api.cli import main

if __name__ == "__main__":
    main()
