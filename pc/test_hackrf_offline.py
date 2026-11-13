"""Hardware-free tests for HackRF support: device class and TX stream against a fake libhackrf, server API with
--sdr hackrf. Never touches a real HackRF (server runs with an empty MUXWRIGHT_HACKRF_DIR, so libhackrf is missing).

    python test_hackrf_offline.py [--verbose]
"""
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests_fake"))
import hackrf as H  # noqa: E402
from fake_hackrf import FakeLib  # noqa: E402

FAILS = []
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
LOG = []


def check(name, cond, info=""):
    print(("ok   " if cond else "FAIL ") + name + (f"  {str(info)[:400]}" if info and not cond else ""))
    if not cond:
        FAILS.append(name)


def log(lvl, msg):
    LOG.append((lvl, msg))
    if "--verbose" in sys.argv:
        print(f"    [{lvl}] {msg}")


# ---------------------------------------------------------------------------- pure functions
def unit_tests():
    check("interp: 4,096 MS/s × 2", H.interp_factor(4096000) == 2)
    check("interp: 2,048 MS/s × 4", H.interp_factor(2048000) == 4)
    check("interp: DVB-T 8 MHz 9,142857 MS/s × 1", H.interp_factor(9142857) == 1)
    try:
        H.interp_factor(25_000_000)
        check("interp: 25 MS/s rejected", False)
    except H.HackRFError:
        check("interp: 25 MS/s rejected", True)
    check("clamp: 10,4 dB -> 10", H.clamp_gain(10.4) == -10)
    check("clamp: 60 dB -> 47 (HackRF range)", H.clamp_gain(60) == -47)
    check("clamp: 89,75 = mute", H.clamp_gain(89.75) == H.MUTE_DB)
    check("fw: 2024.02.1 older than 2026.01.3", H.fw_older("2024.02.1", "2026.01.3") is True)
    check("fw: git build not comparable", H.fw_older("git-3a63b29", "2026.01.3") is None)


# ---------------------------------------------------------------------------- device class
def device_tests():
    lib = FakeLib(present=False)
    d = H.HackRF(log=log, lib=lib)
    check("no board -> connect False with hint", not d.connect() and "kein HackRF" in d.info["error"], d.info)

    lib = FakeLib(fw=b"2024.02.1")
    d = H.HackRF(log=log, lib=lib)
    check("connect", d.connect() and d.info["model"] == "HackRF One" and d.info["serial"].endswith("cafe"), d.info)
    check("connect mutes first (amp off, txvga 0)", lib.calls[1:3] == [("amp", 0), ("txvga", 0)], lib.calls[:4])
    check("old firmware flagged", d.info["fw_outdated"] is True, d.info)
    check("atten 10 -> txvga 37", d.set_atten(10) == 10 and lib.calls[-1] == ("txvga", 37) and not d.muted)
    check("atten 60 -> txvga 0, readback 47", d.set_atten(60) == 47 and lib.calls[-1] == ("txvga", 0) and d.read_gain() == -47)
    d.set_atten(89.75)
    check("atten 89,75 -> muted", d.muted and d.read_gain() == H.MUTE_DB)
    check("RF amp never switched on", all(c[1] == 0 for c in lib.calls if c[0] == "amp"))
    check("rate 4,096 MS/s -> device 8,192 MS/s, filter 0,75·fs", d.set_rate(4096000) == 4096000 and lib.rate == 8192000
          and ("bbfilter", 6144000) in lib.calls, lib.calls[-3:])
    for bad in (500_000, 7_000_000_000):
        try:
            d.set_tx_lo(bad)
            check(f"LO {bad} rejected", False)
        except H.HackRFError:
            check(f"LO {bad} rejected", True)
    check("LO 223,936 MHz", d.set_tx_lo(223936000) == 223936000 and lib.calls[-1] == ("freq", 223936000))
    try:
        d.rx_capture(1, 20, 100e6)
        check("rx_capture -> half duplex error", False)
    except H.HackRFError as e:
        check("rx_capture -> half duplex error", "halbduplex" in str(e))
    check("poll ok", d.poll())
    lib.broken = True
    check("poll detects lost device", not d.poll() and not d.connected)


# ---------------------------------------------------------------------------- TX stream
def stream_tests():
    fs, f0, secs = 4096000, 300e3, 3
    lib = FakeLib()
    d = H.HackRF(log=log, lib=lib)
    d.connect()
    d.set_rate(fs)
    d.set_tx_lo(223936000)
    src = (f"import sys,numpy as np;fs={fs};n=np.arange(fs*{secs});"
           f"z=(16000*np.exp(2j*np.pi*{f0}*n/fs)).astype(np.complex64).view(np.float32);"
           "sys.stdout.buffer.write(np.rint(z).astype('<i2').tobytes())")
    proc = subprocess.Popen([sys.executable, "-c", src], stdout=subprocess.PIPE, bufsize=1 << 20)
    st = H.TxStream(d, proc, fs, log, nsamp=1 << 16, qchunks=32, prefill=4)
    st.start()
    t0 = time.time()
    while st.state != "running" and time.time() - t0 < 20:
        time.sleep(0.05)
    check("stream running", st.state == "running", (st.state, st.error))
    check("start_tx called, streaming flag", ("start_tx",) in lib.calls and d.streaming)
    time.sleep(0.4)
    muted_bytes = len(lib.tx)
    check("muted while not unmuted: only zeros", not any(lib.tx[:muted_bytes]))
    d.set_atten(20)
    time.sleep(1.2)
    check("poll while streaming", d.poll())
    s = st.stats()
    check("rate ≈ 4,096 MS/s from source side", s["rate_ms"] and abs(s["rate_ms"] - 4.096) < 0.3, s)
    st.stop()
    proc.kill()
    check("stop -> stop_tx, not streaming", ("stop_tx",) in lib.calls and not d.streaming)
    check("stop leaves the device muted", d.muted and lib.calls[-1][0] in ("stop_tx", "txvga"), lib.calls[-4:])
    x = np.frombuffer(bytes(lib.tx[muted_bytes:]), np.int8).astype(np.float32)
    z = (x[0::2] + 1j * x[1::2])
    z = z[np.abs(z) > 0]
    check("signal after unmute", len(z) > 100000, len(z))
    if len(z) > 65536:
        seg = z[-65536:]
        spec = np.abs(np.fft.fftshift(np.fft.fft(seg * np.hanning(len(seg)))))
        f = np.fft.fftshift(np.fft.fftfreq(len(seg), 1 / (fs * 2)))
        fpk = f[int(np.argmax(spec))]
        check("tone at 300 kHz in the 8,192 MS/s stream (interpolated)", abs(fpk - f0) < 500, fpk)
        img = spec[np.argmin(np.abs(f - (f0 - fs)))] / spec.max()
        check("interpolation image at f0-fs suppressed > 40 dB", 20 * np.log10(img + 1e-12) < -40, 20 * np.log10(img + 1e-12))
        amp = np.abs(seg).mean()
        check("int8 level ≈ 16000/256", 55 < amp < 70, amp)
    check("no underruns with a fast source", st.underruns == 0, st.underruns)


# ---------------------------------------------------------------------------- server API
def server_tests():
    from test_device_offline import Server, DVBT8, check as _c  # noqa: F401
    from mock_agent import MockAgent
    empty = tempfile.mkdtemp(prefix="nohackrf_")
    os.environ["MUXWRIGHT_HACKRF_DIR"] = empty                 # libhackrf missing -> never real hardware
    ag = MockAgent().start()
    sv = Server(ag, extra=("--sdr", "hackrf"))
    try:
        s, r = sv.get("/api/sdr")
        check("GET /api/sdr -> hackrf, half duplex, 20 MS/s", s == 200 and r["kind"] == "hackrf" and not r["full_duplex"]
              and r["usb_max_sps"] == 20_000_000, r)
        st = sv.get("/api/state")[1]
        check("state.sdr + no agent with HackRF", st["sdr"]["kind"] == "hackrf" and not st["rf"]["device"]["reachable"], st["sdr"])
        s, r = sv.req("/api/config", DVBT8, "PUT")
        st = sv.get("/api/state")[1]
        check("DVB-T 8 MHz with HackRF -> USB path (Pluto would need device)", s == 200 and st["rf"]["path"] == "usb", (s, r, st["rf"]))
        s, r = sv.req("/api/tx/start", {})
        check("tx/start without libhackrf -> 503 HackRF not connected", s == 503 and "HackRF" in r["error"], (s, r))
        s, r = sv.req("/api/monitor", {"seconds": 2})
        check("monitor -> 409 half duplex", s == 409 and r.get("code") == "half_duplex", (s, r))
        pal = {"name": "PAL", "mode": "pal", "pal": {"system": "PAL_BG", "image": {"kind": "testpattern", "pattern": "smptebars"},
                                                     "sound": {"kind": "tone"}}, "rf": {"channel": "custom", "freq_hz": 471250000}}
        s, r = sv.req("/api/config", pal, "PUT")
        check("PAL with HackRF -> rejected with reason", s == 400 and any("HackRF" in str(e) for e in r.get("errors", [])), (s, r))
        s, r = sv.req("/api/sdr", {"kind": "pluto"})
        check("switch to pluto", s == 200 and r["sdr"]["kind"] == "pluto", (s, r))
        st = sv.get("/api/state")[1]
        check("DVB-T 8 MHz with Pluto -> device path again", st["rf"]["path"] == "device", st["rf"])
        check("sdr.json persisted", open(os.path.join(sv.rt, "sdr.json")).read().find("pluto") > 0)
        s, r = sv.req("/api/sdr", {"kind": "rtl"})
        check("unknown device -> 400", s == 400)
        sv.req("/api/sdr", {"kind": "hackrf"})
    finally:
        sv.close()
        ag.stop()


if __name__ == "__main__":
    unit_tests()
    device_tests()
    stream_tests()
    server_tests()
    print(f"\n{'ALLE OK' if not FAILS else f'{len(FAILS)} FEHLER: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
