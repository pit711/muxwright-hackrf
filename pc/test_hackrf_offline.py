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
    from test_device_offline import Server, DVBT8
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


def e2e_tests():
    """Real server process, fake libhackrf (tests_fake/server_fake_hackrf.py), test generator as IQ source."""
    import json
    import shutil
    import urllib.error
    import urllib.request
    from test_device_offline import free_port, DVBT8
    rt = tempfile.mkdtemp(prefix="mx_hackrf_e2e_")
    out = os.path.join(rt, "fake.json")
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, FAKE_HACKRF_OUT=out)
    p = subprocess.Popen([sys.executable, "-u", os.path.join(HERE, "tests_fake", "server_fake_hackrf.py"), "--port", str(port),
                          "--sdr", "hackrf", "--source", "testgen", "--backend", "local", "--agent", "off", "--no-device-ssh",
                          "--runtime", rt, "--no-priority"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def req(path, body=None, method=None):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(base + path, data=data, method=method or ("POST" if data is not None else "GET"),
                                   headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(r, timeout=30) as h:
                return h.status, json.loads(h.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def fake():
        try:
            with open(out) as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def wait(pred, timeout=30):
        t = time.time()
        while time.time() - t < timeout:
            try:
                st = req("/api/state")[1]
                if pred(st, fake()):
                    return st
            except (urllib.error.URLError, ConnectionError):
                pass
            time.sleep(0.5)
        return req("/api/state")[1]

    try:
        st = wait(lambda s, f: s["pluto"]["connected"])
        check("e2e: server connects the (fake) HackRF", st["pluto"]["connected"] and st["pluto"]["model"] == "HackRF One", st["pluto"])
        s, r = req("/api/tx/start", {})
        check("e2e: tx/start DAB -> 200 usb", s == 200 and r["path"] == "usb", (s, r))
        st = wait(lambda s, f: s["tx"]["state"] == "running" and f.get("streaming"))
        f = fake()
        check("e2e: DAB stream running at 4,096×2 MS/s", st["tx"]["state"] == "running" and f.get("rate") == 8192000, (st["tx"], f))
        time.sleep(1.5)
        f = fake()
        check("e2e: muted chain sends only zeros, IF gain 0", f.get("tail_nonzero") == 0 and f.get("txvga_last") == 0, f)
        s, r = req("/api/rf", {"enabled": True, "atten_db": 30})
        check("e2e: RF on refused before setup", s >= 400, (s, r))
        s, r = req("/api/onboarding", {"mode": "lab", "accept": ["shielded", "no_broadcast_radiation", "terms", "privacy", "codecs"]})
        check("e2e: setup done", s == 200, (s, r))
        s, r = req("/api/rf", {"enabled": True, "atten_db": 30})
        st = wait(lambda s, f: f.get("txvga_last") == 17 and f.get("tail_nonzero", 0) > 0, 15)
        f = fake()
        check("e2e: RF on, 30 dB -> txvga 17, signal flows", s == 200 and f.get("txvga_last") == 17 and f.get("tail_nonzero", 0) > 0, (s, r, f))
        check("e2e: readback 30 dB, RF amp never on", st["pluto"]["atten_db"] == 30 and not any(f.get("amp", [1])), (st["pluto"], f))
        s, r = req("/api/rf", {"atten_db": 60})
        st = wait(lambda s, f: f.get("txvga_last") == 0 and s["pluto"]["atten_db"] == 47, 10)
        check("e2e: 60 dB -> clamped to 47 (txvga 0), no resync loop", st["pluto"]["atten_db"] == 47, st["pluto"])
        time.sleep(3)
        log_ = " ".join(x.get("msg", "") for x in req("/api/state")[1]["log"][-30:])
        check("e2e: no 'Dämpfung extern verändert' loop", "extern verändert" not in log_, log_[-300:])
        st = req("/api/state")[1]
        check("e2e: no underruns", st["tx"]["underruns"] == 0, st["tx"])
        req("/api/tx/stop", {})
        st = wait(lambda s, f: s["tx"]["state"] in ("idle", "off") and not f.get("streaming"), 15)
        f = fake()
        check("e2e: stop -> stream ended, IF gain 0", not f.get("streaming") and f.get("txvga_last") == 0, (st["tx"], f))
        s, r = req("/api/config", DVBT8, "PUT")
        check("e2e: DVB-T 8 MHz accepted (usb)", s == 200 and req("/api/state")[1]["rf"]["path"] == "usb", (s, r))
        s, r = req("/api/tx/start", {})
        st = wait(lambda s, f: s["tx"]["state"] == "running" and f.get("streaming"))
        f = fake()
        check("e2e: DVB-T 8 MHz streams at 9,142857 MS/s (no interpolation)", abs((f.get("rate") or 0) - 9142857) < 2 and f.get("freq_last") == 474000000, (s, r, st["tx"], f))
        s, r = req("/api/sdr", {"kind": "pluto"})
        check("e2e: device switch refused while on air", s == 409, (s, r))
        req("/api/tx/stop", {})
    finally:
        p.terminate()
        try:
            o = p.communicate(timeout=10)[0].decode("utf-8", "replace")
        except Exception:
            p.kill()
            o = ""
        if "--verbose" in sys.argv:
            print(o[-4000:])
        shutil.rmtree(rt, ignore_errors=True)


def flasher_tests():
    """hackrf_spiflash flow with the official tools replaced by a stub (order, backup, version check, hold/release)."""
    import hashlib
    import json
    import shutil
    import flasher as F
    d = tempfile.mkdtemp(prefix="mx_hrfw_")
    os.makedirs(os.path.join(d, "firmware"))
    fw = os.path.join(d, "firmware", F.HACKRF_FW)
    with open(fw, "wb") as f:
        f.write(b"\x01" * 4096)
    for exe in ("hackrf_spiflash.exe", "hackrf_info.exe"):
        open(os.path.join(d, exe), "wb").close()
    sha = hashlib.sha256(open(fw, "rb").read()).hexdigest()
    with open(os.path.join(d, "VERSIONS.json"), "w") as f:
        json.dump({"release": "2026.01.3", "files": {f"firmware/{F.HACKRF_FW}": sha}}, f)
    old = os.environ.get("MUXWRIGHT_HACKRF_DIR")
    os.environ["MUXWRIGHT_HACKRF_DIR"] = d
    rt = tempfile.mkdtemp(prefix="mx_hrrt_")

    class App:
        holds = []

        def hold(self, on):
            self.holds.append(on)

    calls = []

    def tool(dd, exe, args, timeout, reported="2026.01.3"):
        calls.append((exe, *args))
        if exe == "hackrf_spiflash" and args[0] == "-r":
            open(args[1], "wb").write(b"old")
        return 0, f"Firmware Version: {reported} (API:1.10)\n" if exe == "hackrf_info" else ""

    try:
        app = App()
        fl = F.Flasher(os.path.dirname(HERE), rt, app=app, wait_scale=0.01)
        st = fl.status()
        check("flasher: hackrf_one board + official image listed", st["boards"].get("hackrf_one") == "spiflash"
              and st["hackrf_image"]["ok"] and st["hackrf_image"]["version"] == "2026.01.3", st.get("hackrf_image"))
        check("flasher: spiflash needs confirm", fl.start("spiflash")[0] == 400)
        fl._hackrf_tool = tool
        check("flasher: spiflash start", fl.start("spiflash", confirm=True)[0] == 200)
        t0 = time.time()
        while fl.job["running"] and time.time() - t0 < 10:
            time.sleep(0.05)
        check("flasher: job ok", fl.job["result"] == "ok", fl.job)
        check("flasher: backup -r, then -w <bin> -R, then hackrf_info",
              [c[:2] for c in calls[:2]] == [("hackrf_spiflash", "-r"), ("hackrf_spiflash", "-w")] and calls[1][2:] == (fw, "-R")
              and calls[2][0] == "hackrf_info", calls)
        check("flasher: never '-i' (skip compatibility check)", not any("-i" in c for c in calls))
        check("flasher: device released and taken back", app.holds == [True, False], app.holds)
        check("flasher: backup file kept", any(n.startswith("hackrf_backup_") for n in os.listdir(os.path.join(rt, "firmware"))))
        calls.clear()
        fl._hackrf_tool = lambda dd, exe, args, timeout: tool(dd, exe, args, timeout, reported="2024.02.1")
        fl.start("spiflash", confirm=True)
        t0 = time.time()
        while fl.job["running"] and time.time() - t0 < 10:
            time.sleep(0.05)
        check("flasher: wrong version after reset -> fail", fl.job["result"] == "fail" and "2024.02.1" in (fl.job["error"] or ""), fl.job)
        with open(fw, "ab") as f:
            f.write(b"x")
        check("flasher: tampered firmware rejected", fl.start("spiflash", confirm=True)[0] == 400)
    finally:
        if old is None:
            os.environ.pop("MUXWRIGHT_HACKRF_DIR", None)
        else:
            os.environ["MUXWRIGHT_HACKRF_DIR"] = old
        shutil.rmtree(d, ignore_errors=True)
        shutil.rmtree(rt, ignore_errors=True)


if __name__ == "__main__":
    flasher_tests()
    unit_tests()
    device_tests()
    stream_tests()
    server_tests()
    e2e_tests()
    print(f"\n{'ALLE OK' if not FAILS else f'{len(FAILS)} FEHLER: ' + ', '.join(FAILS)}")
    sys.exit(1 if FAILS else 0)
