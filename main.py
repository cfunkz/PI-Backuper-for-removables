"""Entry point. Build the exe from this file (see README)."""

import sys

if sys.platform != "win32":
    sys.exit("PI-Backuper only runs on Windows.")

from pi_backuper.gui import main

if __name__ == "__main__":
    main()
