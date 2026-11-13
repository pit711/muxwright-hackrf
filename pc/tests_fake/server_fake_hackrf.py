"""Start pc/server.py with the fake libhackrf (tests only, never real hardware).

Every 0.5 s the fake writes a summary to $FAKE_HACKRF_OUT (JSON): calls, transfers, non-zero samples, peak, last gain.
    python server_fake_hackrf.py <server.py arguments>
"""
import json
import os
import runpy
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PC = os.path.dirname(HERE)
sys.path[:0] = [PC, HERE]
import numpy as np  # noqa: E402
import hackrf  # noqa: E402
from fake_hackrf import FakeLib  # noqa: E402

lib = FakeLib(keep_bytes=8 << 20)
hackrf._lib = lib                      # load_lib() returns this instead of hackrf.dll


def report():
    out = os.environ.get("FAKE_HACKRF_OUT")
    while out:
        tail = bytes(lib.tx[-(1 << 20):])
        x = np.frombuffer(tail, np.int8) if tail else np.zeros(2, np.int8)
        s = {"t": time.time(), "transfers": lib.transfers, "rate": lib.rate,
             "streaming": lib._th is not None and lib._th.is_alive(),
             "tail_peak": int(np.abs(x.astype(np.int16)).max()), "tail_nonzero": int(np.count_nonzero(x)),
             "amp": [c[1] for c in lib.calls if c[0] == "amp"], "txvga_last": next((c[1] for c in reversed(lib.calls) if c[0] == "txvga"), None),
             "freq_last": next((c[1] for c in reversed(lib.calls) if c[0] == "freq"), None)}
        with open(out + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(out + ".tmp", out)
        lib.tx = lib.tx[-(1 << 20):]
        time.sleep(0.5)


threading.Thread(target=report, daemon=True).start()
sys.argv = [os.path.join(PC, "server.py"), *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
