# Test the local AI with FocusGuard's real prompts:  python -I selftest.py
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from focusguard.selftest import main  # noqa: E402

main()
