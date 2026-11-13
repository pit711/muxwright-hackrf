"""Fetch the official HackRF files from Great Scott Gadgets into pc/hackrf_bin (not in Git).

- Firmware + licence: official release (github.com/greatscottgadgets/hackrf/releases, tag RELEASE)
  -> pc/hackrf_bin/firmware/hackrf_one_usb.bin (+ .dfu), COPYING
- Windows host tools (hackrf.dll, hackrf_info.exe, hackrf_spiflash.exe, libusb-1.0.dll, pthreadVC3.dll):
  the official GSG CI build "hackrf-tools-windows" of branch main (installing_hackrf_software.rst: "binaries are
  available as build artifacts under the 'Actions'-tab"). Needs the GitHub CLI `gh`, logged in (artifact download
  requires a login). GSG publishes no Windows binaries in the release itself.

Writes pc/hackrf_bin/VERSIONS.json with release tag, artifact id, commit and SHA-256 of every file.

    python scripts/fetch_hackrf.py [--release 2026.01.3] [--force]
"""
import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEST = os.path.join(ROOT, "pc", "hackrf_bin")
REPO = "greatscottgadgets/hackrf"
RELEASE = "2026.01.3"
WIN_FILES = ("hackrf.dll", "hackrf_info.exe", "hackrf_spiflash.exe", "hackrf_transfer.exe",
             "libusb-1.0.dll", "pthreadVC3.dll", "fftw3f.dll")
FW_FILES = ("hackrf_one_usb.bin", "hackrf_one_usb.dfu")


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def gh(*args):
    try:
        return subprocess.run(["gh", *args], check=True, capture_output=True).stdout
    except FileNotFoundError:
        sys.exit("GitHub CLI 'gh' fehlt – installieren und 'gh auth login' ausführen")
    except subprocess.CalledProcessError as e:
        sys.exit(f"gh {' '.join(args[:2])} fehlgeschlagen: {e.stderr.decode(errors='replace').strip()}")


def fetch_release(tag, dest):
    url = f"https://github.com/{REPO}/releases/download/v{tag}/hackrf-{tag}.zip"
    print(f"Release {tag}: {url}")
    with urllib.request.urlopen(url, timeout=120) as r:
        data = r.read()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        top = f"hackrf-{tag}/"
        os.makedirs(os.path.join(dest, "firmware"), exist_ok=True)
        for name in FW_FILES:
            with z.open(top + "firmware-bin/" + name) as src, open(os.path.join(dest, "firmware", name), "wb") as dst:
                shutil.copyfileobj(src, dst)
        with z.open(top + "COPYING") as src, open(os.path.join(dest, "COPYING"), "wb") as dst:
            shutil.copyfileobj(src, dst)
        lic = z.read(top + "host/libhackrf/src/hackrf.c").decode(errors="replace").split("*/")[0]
        with open(os.path.join(dest, "LICENSE-libhackrf.txt"), "w", encoding="utf-8") as f:
            f.write(lic.lstrip("/*").strip() + "\n")


def fetch_windows(dest):
    raw = gh("api", f"repos/{REPO}/actions/artifacts?name=hackrf-tools-windows&per_page=50")
    arts = [a for a in json.loads(raw)["artifacts"]
            if not a["expired"] and (a.get("workflow_run") or {}).get("head_branch") == "main"]
    if not arts:
        sys.exit("kein gültiger offizieller Windows-Build (hackrf-tools-windows, Branch main) gefunden")
    a = max(arts, key=lambda x: x["created_at"])
    print(f"Windows-Build: Artefakt {a['id']} vom {a['created_at']} (Commit {a['workflow_run']['head_sha'][:10]})")
    blob = gh("api", f"repos/{REPO}/actions/artifacts/{a['id']}/zip")
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        names = {os.path.basename(n): n for n in z.namelist()}
        for f in WIN_FILES:
            if f not in names:
                sys.exit(f"{f} fehlt im offiziellen Build {a['id']}")
            with z.open(names[f]) as src, open(os.path.join(dest, f), "wb") as dst:
                shutil.copyfileobj(src, dst)
    return {"artifact_id": a["id"], "created_at": a["created_at"], "commit": a["workflow_run"]["head_sha"]}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--release", default=RELEASE)
    ap.add_argument("--force", action="store_true", help="vorhandenen Ordner ersetzen")
    ap.add_argument("--no-windows", action="store_true", help="nur Firmware (z. B. unter Linux: libhackrf aus dem Paket)")
    a = ap.parse_args()
    if os.path.isdir(DEST) and os.listdir(DEST) and not a.force:
        sys.exit(f"{DEST} ist nicht leer – mit --force ersetzen")
    tmp = DEST + ".new"
    shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    fetch_release(a.release, tmp)
    win = None if a.no_windows else fetch_windows(tmp)
    files = {}
    for base, _, names in os.walk(tmp):
        for n in names:
            p = os.path.join(base, n)
            files[os.path.relpath(p, tmp).replace("\\", "/")] = sha256(p)
    with open(os.path.join(tmp, "VERSIONS.json"), "w", encoding="utf-8") as f:
        json.dump({"source": f"https://github.com/{REPO}", "release": a.release, "windows_build": win,
                   "files": files}, f, indent=1)
    shutil.rmtree(DEST, ignore_errors=True)
    os.replace(tmp, DEST)
    print(f"fertig: {DEST} ({len(files)} Dateien)")


if __name__ == "__main__":
    main()
