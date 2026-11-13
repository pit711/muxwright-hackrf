"""HackRF One access for Muxwright through the official libhackrf (Great Scott Gadgets, BSD-3-Clause).

The class `HackRF` has the same interface as `pluto.Pluto`, `TxStream` the same as `pluto.TxStream`, so the
server can use either device. Only the USB path exists: the HackRF has no CPU, the PC modulates.

Hardware facts used here (official docs, hackrf/docs/source):
- TX gain: IF 0…47 dB in 1 dB steps, RF amp 0 or ~11 dB (setting_gain.rst). The RF amp is never switched on here.
- Sampling rate below 8 MS/s is not recommended (sampling_rate.rst): slower sources are interpolated by an integer
  factor k until k·fs ≥ 8 MS/s. Maximum 20 MS/s.
- Half duplex: no RX while transmitting, so no control receiver / RX preview.
- Samples are interleaved signed 8 bit I/Q (hackrf.h, hackrf_transfer.buffer).

Safety: connect, mute, stop, error and close switch the RF amp off, set IF gain 0 and send zeros.
"""
import ctypes
import os
import queue
import sys
import threading
import time

import numpy as np

import pluto as plutomod

MUTE_DB = plutomod.MUTE_DB          # -89.75: same "mute" value as the Pluto, so the server logic stays identical
TXVGA_MAX_DB = 47                   # setting_gain.rst: IF 0…47 dB
FS_MIN_RECOMMENDED = 8_000_000      # sampling_rate.rst
FS_MAX = 20_000_000
FREQ_MIN, FREQ_MAX = 1_000_000, 6_000_000_000
RELEASE = "2026.01.3"               # official release whose firmware we ship (scripts/fetch_hackrf.py)

HACKRF_SUCCESS, HACKRF_TRUE = 0, 1
HACKRF_ERROR_NOT_FOUND = -5

_HERE = os.path.dirname(os.path.abspath(__file__))
HACKRF_DIR = next((d for d in (os.environ.get("MUXWRIGHT_HACKRF_DIR"), os.path.join(_HERE, "hackrf_bin"))
                   if d and os.path.isdir(d)), os.path.join(_HERE, "hackrf_bin"))


class HackRFError(Exception):
    pass


class _Transfer(ctypes.Structure):
    _fields_ = [("device", ctypes.c_void_p), ("buffer", ctypes.POINTER(ctypes.c_uint8)),
                ("buffer_length", ctypes.c_int), ("valid_length", ctypes.c_int),
                ("rx_ctx", ctypes.c_void_p), ("tx_ctx", ctypes.c_void_p)]


class _PartSerial(ctypes.Structure):
    _fields_ = [("part_id", ctypes.c_uint32 * 2), ("serial_no", ctypes.c_uint32 * 4)]


CALLBACK = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(_Transfer))

_lib = None
_lib_err = None


def load_lib():
    """Load hackrf.dll / libhackrf.so once. Returns the ctypes library or None (reason in lib_error())."""
    global _lib, _lib_err
    if _lib is not None or _lib_err is not None:
        return _lib
    try:
        if sys.platform == "win32":
            if os.path.isdir(HACKRF_DIR):
                os.add_dll_directory(HACKRF_DIR)
            path = os.path.join(HACKRF_DIR, "hackrf.dll")
            # an explicit MUXWRIGHT_HACKRF_DIR is the only place searched (tests use it to never reach real hardware)
            explicit = bool(os.environ.get("MUXWRIGHT_HACKRF_DIR"))
            lib = ctypes.CDLL(path if os.path.isfile(path) or explicit else "hackrf.dll")
        else:
            lib = ctypes.CDLL("libhackrf.so.0")
        vp, i, u32, u8 = ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_uint8
        for name, res, args in (
                ("hackrf_init", i, []), ("hackrf_open", i, [ctypes.POINTER(vp)]), ("hackrf_close", i, [vp]),
                ("hackrf_error_name", ctypes.c_char_p, [i]), ("hackrf_library_version", ctypes.c_char_p, []),
                ("hackrf_board_id_read", i, [vp, ctypes.POINTER(u8)]),
                ("hackrf_board_id_name", ctypes.c_char_p, [i]),
                ("hackrf_version_string_read", i, [vp, ctypes.c_char_p, u8]),
                ("hackrf_board_partid_serialno_read", i, [vp, ctypes.POINTER(_PartSerial)]),
                ("hackrf_set_freq", i, [vp, ctypes.c_uint64]),
                ("hackrf_set_sample_rate", i, [vp, ctypes.c_double]),
                ("hackrf_compute_baseband_filter_bw", u32, [u32]),
                ("hackrf_set_baseband_filter_bandwidth", i, [vp, u32]),
                ("hackrf_set_txvga_gain", i, [vp, u32]), ("hackrf_set_amp_enable", i, [vp, u8]),
                ("hackrf_start_tx", i, [vp, CALLBACK, vp]), ("hackrf_stop_tx", i, [vp]),
                ("hackrf_is_streaming", i, [vp])):
            f = getattr(lib, name)
            f.restype, f.argtypes = res, args
        rc = lib.hackrf_init()
        if rc != HACKRF_SUCCESS:
            raise HackRFError(f"hackrf_init: {rc}")
        _lib = lib
    except Exception as e:
        _lib_err = (f"{type(e).__name__}: {e} – offizielle HackRF-Bibliothek fehlt, "
                    f"scripts/fetch_hackrf.py ausführen (Ordner {HACKRF_DIR})")
    return _lib


def lib_error():
    return _lib_err


def clamp_gain(atten_db):
    """Requested attenuation (positive dB) -> gain value in the Pluto sense (≤ 0 dB).
    The HackRF covers 0…47 dB; above that only "mute" (-89.75) exists."""
    a = float(atten_db)
    if a >= -MUTE_DB:
        return MUTE_DB
    return -float(max(0, min(TXVGA_MAX_DB, round(a))))


def interp_factor(fs):
    """Smallest integer k with k·fs ≥ 8 MS/s (official recommendation); k·fs must stay ≤ 20 MS/s."""
    fs = int(fs)
    if fs <= 0 or fs > FS_MAX:
        raise HackRFError(f"Abtastrate {fs / 1e6:g} MS/s außerhalb 0 … {FS_MAX / 1e6:g} MS/s")
    k = max(1, -(-FS_MIN_RECOMMENDED // fs))
    if k * fs > FS_MAX:
        raise HackRFError(f"{fs / 1e6:g} MS/s lässt sich nicht ganzzahlig auf 8 … 20 MS/s bringen")
    return k


class Interpolator:
    """Integer-factor interpolation with state across blocks (polyphase FIR, scipy.signal.upfirdn)."""

    def __init__(self, k, taps_per_phase=16):
        self.k = int(k)
        if self.k == 1:
            return
        from scipy.signal import firwin
        self.P = taps_per_phase
        self.h = (firwin(self.k * self.P, 0.9 / self.k) * self.k).astype(np.float32)
        self.hist = np.zeros(self.P, dtype=np.complex64)

    def __call__(self, x):
        if self.k == 1:
            return x
        from scipy.signal import upfirdn
        xx = np.concatenate((self.hist, x))
        y = upfirdn(self.h, xx, up=self.k)
        start = self.P * self.k                  # every output from here on depends only on xx (no zero history)
        self.hist = xx[-self.P:]
        return y[start:start + len(x) * self.k].astype(np.complex64)


def to_int8(z):
    """complex64 in int16 scale -> interleaved int8 bytes."""
    f = z.view(np.float32) * (1.0 / 256.0)
    np.clip(np.rint(f), -127, 127, out=f)
    return f.astype(np.int8).tobytes()


class HackRF:
    """Same interface as pluto.Pluto (connect/mute/set_atten/read_gain/set_tx_lo/set_rate/poll/...)."""

    kind = "hackrf"
    full_duplex = False

    def __init__(self, uri="hackrf:", log=print, lib=None):
        self.uri = uri
        self.log = log
        self._lib = lib
        self.lock = threading.RLock()
        self.dev = None
        self.info = {"connected": False, "uri": uri, "fw": None, "temp_c": None, "lo_hz": None, "rx_lo_hz": None,
                     "atten_db": None, "model": None, "serial": None, "error": None, "kind": "hackrf",
                     "lib": None, "fw_release": RELEASE, "fw_outdated": None, "fs_out": None, "interp": None}
        self.fs = None
        self.fs_out = None
        self.interp = 1
        self.rx_busy = False
        self.ctl_retries = 0
        self.passive = False
        self.muted = True
        self.atten = -MUTE_DB
        self.streaming = False

    clamp_gain = staticmethod(clamp_gain)

    # ---------------------------------------------------------------- helpers
    def lib(self):
        return self._lib if self._lib is not None else load_lib()

    def _err(self, rc):
        try:
            name = self.lib().hackrf_error_name(rc)
            return name.decode() if isinstance(name, bytes) else str(name)
        except Exception:
            return str(rc)

    def _call(self, fname, *args):
        with self.lock:
            if self.dev is None:
                raise HackRFError("HackRF nicht verbunden")
            rc = getattr(self.lib(), fname)(self.dev, *args)
        if rc != HACKRF_SUCCESS:
            raise HackRFError(f"{fname}: {self._err(rc)} ({rc})")
        return rc

    @property
    def connected(self):
        return self.dev is not None

    # ---------------------------------------------------------------- connection
    def connect(self, passive=False):
        """passive is ignored: there is no on-device modulator that could own the TX path."""
        lib = self.lib()
        if lib is None:
            self.info["error"] = lib_error()
            return False
        with self.lock:
            if self.dev is not None:
                return True
            dev = ctypes.c_void_p()
            rc = lib.hackrf_open(ctypes.byref(dev))
            if rc != HACKRF_SUCCESS:
                self.info.update(connected=False, error=("kein HackRF gefunden (USB, WinUSB-Treiber?)"
                                                         if rc == HACKRF_ERROR_NOT_FOUND else f"hackrf_open: {self._err(rc)}"))
                return False
            self.dev = dev
            try:
                self.mute()
                bid = ctypes.c_uint8()
                self._call("hackrf_board_id_read", ctypes.byref(bid))
                name = lib.hackrf_board_id_name(bid.value)
                ver = ctypes.create_string_buffer(255)
                self._call("hackrf_version_string_read", ver, 255)
                ps = _PartSerial()
                serial = None
                if lib.hackrf_board_partid_serialno_read(self.dev, ctypes.byref(ps)) == HACKRF_SUCCESS:
                    serial = "".join(f"{v:08x}" for v in ps.serial_no)
                libver = lib.hackrf_library_version()
                fw = ver.value.decode(errors="replace")
                self.info.update(connected=True, error=None, fw=fw, serial=serial,
                                 model=name.decode() if isinstance(name, bytes) else str(name),
                                 lib=libver.decode() if isinstance(libver, bytes) else libver,
                                 fw_outdated=fw_older(fw, RELEASE))
                self.log("info", f"HackRF verbunden: {self.info['model']} fw {fw} (libhackrf {self.info['lib']})")
                if self.info["fw_outdated"]:
                    self.log("warn", f"HackRF-Firmware {fw} ist älter als die offizielle {RELEASE} – Seite Firmware")
                return True
            except Exception as e:
                self._close_dev()
                self.info.update(connected=False, error=str(e))
                return False

    def _close_dev(self):
        with self.lock:
            if self.dev is not None:
                try:
                    if self.streaming:
                        self.lib().hackrf_stop_tx(self.dev)
                    self.lib().hackrf_close(self.dev)
                except Exception:
                    pass
            self.dev = None
            self.streaming = False

    def drop(self, why=""):
        with self.lock:
            if self.dev is not None:
                self.log("warn", f"HackRF getrennt {why}".strip())
            self._close_dev()
            self.info.update(connected=False)

    def close(self):
        with self.lock:
            try:
                self.mute()
            except Exception:
                pass
            self.drop()

    # ---------------------------------------------------------------- gain / LO / rate
    def mute(self):
        """Zeros into the DAC, IF gain 0, RF amp off. Never raises if not connected."""
        self.muted = True
        self.atten = -MUTE_DB
        self.info["atten_db"] = -MUTE_DB
        with self.lock:
            if self.dev is None:
                return False
            self._call("hackrf_set_amp_enable", 0)
            self._call("hackrf_set_txvga_gain", 0)
            return True

    def set_atten(self, atten_db):
        g = clamp_gain(atten_db)
        if g == MUTE_DB:
            self.mute()
            return -MUTE_DB
        self._call("hackrf_set_amp_enable", 0)
        self._call("hackrf_set_txvga_gain", int(TXVGA_MAX_DB + g))
        self.atten = -g
        self.muted = False
        self.info["atten_db"] = self.atten
        return self.atten

    def read_gain(self):
        """No readback in libhackrf: returns the last value set (Pluto sense, ≤ 0 dB)."""
        return -self.atten

    def set_tx_lo(self, hz):
        hz = int(round(hz))
        if not FREQ_MIN <= hz <= FREQ_MAX:
            raise HackRFError(f"Frequenz {hz} Hz außerhalb 1 MHz … 6 GHz")
        self._call("hackrf_set_freq", hz)
        self.info["lo_hz"] = hz
        return hz

    def set_rx_lo(self, hz):
        raise HackRFError("HackRF ist halbduplex: kein Empfang während des Sendens")

    def set_rate(self, fs):
        """fs = rate of the IQ source. The device runs at k·fs (k from interp_factor)."""
        fs = int(fs)
        k = interp_factor(fs)
        out = fs * k
        self._call("hackrf_set_sample_rate", float(out))
        bw = self.lib().hackrf_compute_baseband_filter_bw(int(out * 0.75))   # same default as hackrf_transfer
        self._call("hackrf_set_baseband_filter_bandwidth", bw)
        self.fs, self.fs_out, self.interp = fs, out, k
        self.info.update(fs_out=out, interp=k)
        if k > 1:
            self.log("info", f"HackRF: {fs / 1e6:g} MS/s × {k} = {out / 1e6:g} MS/s (empfohlen ≥ 8 MS/s)")
        return fs

    def poll(self):
        with self.lock:
            if self.dev is None:
                return False
            try:
                if self.streaming:
                    rc = self.lib().hackrf_is_streaming(self.dev)
                    if rc != HACKRF_TRUE:
                        raise HackRFError(f"Strom abgebrochen: {self._err(rc)}")
                else:
                    self._call("hackrf_board_id_read", ctypes.byref(ctypes.c_uint8()))
                self.info["atten_db"] = self.atten
                return True
            except Exception as e:
                self.drop(f"({e})")
                return False

    def unf_read_clear(self, wait=True):
        return 0            # underruns are counted in the TX callback

    def rx_capture(self, seconds, gain_db, lo_hz, nbuf=1 << 17):
        raise HackRFError("HackRF ist halbduplex: Kontrollempfang nur mit zweitem Empfänger")


def fw_older(fw, release):
    """True if a release firmware string (e.g. '2024.02.1') is older than release; None if not comparable (git builds)."""
    def key(s):
        try:
            return tuple(int(x) for x in str(s).strip().split("."))
        except ValueError:
            return None
    a, b = key(fw), key(release)
    return None if a is None or b is None else a < b


# ======================================================================== TX streaming
class TxStream(plutomod.TxStream):
    """Like pluto.TxStream; the pusher feeds a queue that the libhackrf TX callback drains.

    reader thread : proc.stdout (int16 IQ) -> q
    pusher thread : q -> NCO -> interpolation ×k -> int8 -> oq (bounded: back-pressure paces the chain)
    libhackrf     : callback copies oq into the USB transfer, zeros when muted or empty (counted as underrun)
    """

    def __init__(self, dev, proc, fs, log, **kw):
        super().__init__(dev, proc, fs, log, **kw)
        self.k = getattr(dev, "interp", 1) or 1
        per_item = self.N * self.k
        self.oq = queue.Queue(maxsize=max(2, int(0.25 * fs * self.k / per_item) + 1))
        self._carry = b""
        self._cb = CALLBACK(self._callback)       # keep a reference: libhackrf calls it from its USB thread

    def _callback(self, tp):
        try:
            t = tp.contents
            n = t.buffer_length
            muted = self.p.muted or self.stop_ev.is_set()
            got = 0
            if not muted:
                parts = [self._carry]
                got = len(self._carry)
                while got < n:
                    try:
                        b = self.oq.get_nowait()
                    except queue.Empty:
                        break
                    parts.append(b)
                    got += len(b)
                data = b"".join(parts)
                self._carry = data[n:]
                got = min(got, n)
                if got:
                    ctypes.memmove(t.buffer, data, got)
                if got < n and self.state == "running":
                    self.underruns += 1
                    if self.underruns <= 10 or self.underruns % 100 == 0:
                        self.log("warn", f"HackRF-Underrun #{self.underruns}")
            if got < n:
                ctypes.memset(ctypes.addressof(t.buffer.contents) + got, 0, n - got)
            t.valid_length = n
        except Exception:
            pass
        return 0

    def _pusher(self):
        plutomod.set_thread_priority(2)
        p = self.p
        started = False
        interp = Interpolator(self.k)
        try:
            t0 = time.time()
            while self.q.qsize() < self.prefill and not self.stop_ev.is_set():
                if time.time() - t0 > 60:
                    raise HackRFError("keine IQ-Daten innerhalb 60 s")
                time.sleep(0.02)
            if self.stop_ev.is_set():
                return
            nbufs = 0
            prefill_out = self.oq.maxsize // 2
            while not self.stop_ev.is_set():
                if self.q.empty() and self.state == "running":
                    self.src_late += 1
                try:
                    chunk = self.q.get(timeout=1.0)
                except queue.Empty:
                    self.src_stalls += 1
                    if self.src_stalls % 5 == 1:
                        self.log("warn", "IQ-Quelle liefert nicht (Puffer leer)")
                    continue
                z = np.frombuffer(self._apply_nco(chunk), dtype="<i2").astype(np.float32).view(np.complex64)
                out = to_int8(interp(z))
                while not self.stop_ev.is_set():
                    try:
                        self.oq.put(out, timeout=0.5)
                        break
                    except queue.Full:
                        continue
                nbufs += 1
                if not started and (self.oq.qsize() >= prefill_out or self.oq.full()):
                    with p.lock:
                        rc = p.lib().hackrf_start_tx(p.dev, self._cb, None)
                        if rc != HACKRF_SUCCESS:
                            raise HackRFError(f"hackrf_start_tx: {p._err(rc)} ({rc})")
                        p.streaming = True
                    started = True
                    self.state, self.since, self.t_first = "running", time.time(), None
                    self.log("info", "TX-Strom läuft (HackRF)")
                now = time.time()
                self.samples_sent += self.N
                self.hist.append((now, self.samples_sent))
                self.kernel_level = self.oq.qsize() * self.N
                if self.t_first is None and started and self.oq.full():
                    self.t_first, self.s_first = now, self.samples_sent
        except Exception as e:
            if not self.stop_ev.is_set():
                self._fail(f"HackRF: {e}")
        finally:
            try:
                p.mute()
            except Exception:
                pass
            with p.lock:
                if p.streaming and p.dev is not None:
                    try:
                        p.lib().hackrf_stop_tx(p.dev)
                    except Exception:
                        pass
                p.streaming = False
