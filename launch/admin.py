# Entry point, run as: python -I admin.py   (-I ignores PYTHON* env vars and user packages)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from focusguard.admin import main  # noqa: E402

sys.exit(main(sys.argv))
