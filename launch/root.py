# Entry point for the root service, run as: python -I root.py
# If a freshly auto-installed version can't start, switch back to the previous one;
# launchd then restarts the service on the old code.
import os
import shutil
import sys
import traceback

APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREV = APP + ".prev"
sys.path.insert(0, APP)


def roll_back():
    traceback.print_exc()
    if os.path.isdir(PREV):
        bad = APP + ".bad"
        shutil.rmtree(bad, ignore_errors=True)
        os.rename(APP, bad)
        os.rename(PREV, APP)
        print("FocusGuard: new version failed to start; went back to the previous one",
              file=sys.stderr)
    sys.exit(1)


try:
    from focusguard.rootd import main
except Exception:
    roll_back()

try:
    main()
except SystemExit:
    raise
except Exception:
    roll_back()
