"""Stand-in for libhackrf (same function names and ctypes argument style) for hardware-free tests.

Simulates a HackRF One: open/board id/version, gain and rate setters (all calls recorded) and a TX streaming
thread that calls the libhackrf callback in real time (buffer of 262144 bytes, like libhackrf's TRANSFER_BUFFER_SIZE)
and keeps what it was given.
"""
import ctypes
import threading
import time

import hackrf as H

BUF = 262144


class FakeLib:
    def __init__(self, present=True, fw=b"2026.01.3", keep_bytes=64 << 20):
        self.present = present
        self.fw = fw
        self.calls = []
        self.rate = None
        self.tx = bytearray()
        self.keep = keep_bytes
        self.transfers = 0
        self._stop = threading.Event()
        self._th = None
        self.broken = False

    def _rec(self, *a):
        self.calls.append(a)
        return H.HACKRF_SUCCESS

    # ------------------------------------------------------------ device
    def hackrf_open(self, pdev):
        if not self.present:
            return H.HACKRF_ERROR_NOT_FOUND
        pdev._obj.value = 0x1234
        return self._rec("open")

    def hackrf_close(self, dev):
        return self._rec("close")

    def hackrf_error_name(self, rc):
        return f"FAKE_ERROR_{rc}".encode()

    def hackrf_library_version(self):
        return b"fake-0.10.0"

    def hackrf_board_id_read(self, dev, pval):
        pval._obj.value = 2
        return -1000 if self.broken else H.HACKRF_SUCCESS

    def hackrf_board_id_name(self, bid):
        return b"HackRF One"

    def hackrf_version_string_read(self, dev, buf, n):
        buf.value = self.fw
        return H.HACKRF_SUCCESS

    def hackrf_board_partid_serialno_read(self, dev, pps):
        pps._obj.serial_no[3] = 0xCAFE
        return H.HACKRF_SUCCESS

    # ------------------------------------------------------------ settings
    def hackrf_set_freq(self, dev, hz):
        return self._rec("freq", int(hz))

    def hackrf_set_sample_rate(self, dev, fs):
        self.rate = float(fs)
        return self._rec("rate", float(fs))

    def hackrf_compute_baseband_filter_bw(self, bw):
        return int(bw)

    def hackrf_set_baseband_filter_bandwidth(self, dev, bw):
        return self._rec("bbfilter", int(bw))

    def hackrf_set_txvga_gain(self, dev, g):
        return self._rec("txvga", int(g))

    def hackrf_set_amp_enable(self, dev, on):
        return self._rec("amp", int(on))

    # ------------------------------------------------------------ streaming
    def hackrf_start_tx(self, dev, cb, ctx):
        self._stop.clear()
        self._th = threading.Thread(target=self._run, args=(cb,), daemon=True)
        self._th.start()
        return self._rec("start_tx")

    def hackrf_stop_tx(self, dev):
        self._stop.set()
        if self._th is not None:
            self._th.join(5)
        return self._rec("stop_tx")

    def hackrf_is_streaming(self, dev):
        return -1004 if self.broken else (H.HACKRF_TRUE if self._th is not None and self._th.is_alive() else 0)

    def _run(self, cb):
        raw = (ctypes.c_uint8 * BUF)()
        t = H._Transfer(None, ctypes.cast(raw, ctypes.POINTER(ctypes.c_uint8)), BUF, 0, None, None)
        per = BUF / 2 / (self.rate or 10e6)
        nxt = time.perf_counter()
        while not self._stop.is_set():
            cb(ctypes.pointer(t))
            self.transfers += 1
            if len(self.tx) < self.keep:
                self.tx += bytes(raw)[:t.valid_length]
            nxt += per
            d = nxt - time.perf_counter()
            if d > 0:
                time.sleep(d)
