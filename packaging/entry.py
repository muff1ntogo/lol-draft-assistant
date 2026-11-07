"""PyInstaller entry point: the same launcher as `draftassist` (src/draftassist/launcher.py)."""
from draftassist.launcher import main

if __name__ == "__main__":
    raise SystemExit(main())
