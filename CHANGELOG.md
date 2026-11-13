# Changelog – Muxwright HackRF edition

Based on Muxwright Beta 0.7.1 (public `main`, 7be2dbd). Upstream changes: https://github.com/pit711/muxwright

## 0.7.1-hackrf.1 – 2026-11-13

### Added
- HackRF One as a second transmitter (`pc/hackrf.py`, official libhackrf via ctypes): DAB/DAB+, FM + RDS, DVB-T,
  DVB-C Annex A and amateur-radio DATV over USB, up to 20 MS/s; slower sources are interpolated to ≥ 8 MS/s.
- Transmitter choice: `--sdr pluto|hackrf`, `runtime/sdr.json`, `GET/POST /api/sdr`, Settings page and setup step.
- Firmware page: device "HackRF One" writes the official Great Scott Gadgets firmware with `hackrf_spiflash`
  (backup first, tool's own compatibility check, reset, version check with `hackrf_info`).
- `scripts/fetch_hackrf.py`: official firmware (release v2026.01.3) and Windows tools with versions and SHA-256.
- Hardware recommendations for the HackRF in the README.

### Safety
- The HackRF's RF amplifier is never switched on; IF gain 0 … 47 dB, mute = zeros + gain 0.
- Standards whose modulator only exists in the Pluto firmware are refused with a reason and locked in the control room.
- The monitoring receiver is refused on the HackRF (half duplex).

### Not verified
- No transmission on a real HackRF One yet; all tests run against a fake libhackrf (`pc/test_hackrf_offline.py`).
