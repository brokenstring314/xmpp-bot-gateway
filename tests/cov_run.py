"""Run an e2e script under coverage. The e2e scripts end with os._exit (to skip slow slixmpp teardown),
which would drop coverage data, so save it first.  Usage: python3 tests/cov_run.py tests/e2e_p0.py"""
import os, runpy, sys

import coverage

cov = coverage.Coverage(source=["src/xmpp_gateway"], data_suffix=True)
cov.start()
_exit = os._exit


def _save_and_exit(code):
    cov.stop()
    cov.save()
    _exit(code)


os._exit = _save_and_exit
sys.argv = sys.argv[1:]
try:
    runpy.run_path(sys.argv[0], run_name="__main__")
finally:
    cov.stop()
    cov.save()
