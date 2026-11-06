"""Give supervised Windows children a graceful Ctrl+Break shutdown."""

import runpy
import signal
import sys


def main():
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    module = sys.argv.pop(1)
    sys.argv[0] = module
    try:
        runpy.run_module(module, run_name="__main__")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
