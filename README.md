# hpsdr-emu

OpenHPSDR radio emulator that presents itself as a real HPSDR radio on the network so SDR applications like [Thetis](https://github.com/ramdor/Thetis) or [deskHPSDR](https://github.com/dl1ycf/deskhpsdr) can discover and connect to it.

Two implementations are available:

| | Python | Rust |
|---|--------|------|
| **Protocols** | Protocol 1 + Protocol 2 | Protocol 1 only |
| **Features** | Test tone, echo mode, TX feedback | Test tone, echo mode, TX feedback |
| **Requirements** | Python >= 3.12, uv | Rust toolchain (cargo) |

Both generate a test tone with configurable noise on all RX DDC channels, support echo mode (TX IQ looped back on RX), and report forward/reverse power and supply voltage during TX for power/SWR metering.

## Python version

### Requirements

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)

### Install

```bash
uv sync
```

### Usage

```bash
# Protocol 1, Hermes Lite (2 DDCs)
uv run hpsdr-emu --protocol 1 --radio hermeslite

# Protocol 2, Orion MKII (8 DDCs)
uv run hpsdr-emu --protocol 2 --radio orionmkii

# Custom tone and noise level
uv run hpsdr-emu --protocol 1 --radio hermes --freq 800 --noise 1e-5

# Echo mode — TX IQ loops back on RX
uv run hpsdr-emu --protocol 1 --radio hermes --echo

# Fixed MAC address
uv run hpsdr-emu --protocol 2 --radio angelia --mac 00:1c:c0:a2:22:5e

# Debug logging
uv run hpsdr-emu --protocol 1 --radio hermeslite -v
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--protocol {1,2}` | Protocol version (required) | — |
| `--radio TYPE` | Hardware type to emulate | `hermeslite` |
| `--mac MAC` | MAC address (hex) | random |
| `--freq HZ` | Test tone offset from center | `1000` |
| `--noise LEVEL` | Noise level as fraction of full-scale | `3e-6` (~-100 dBm) |
| `--echo` | Echo mode (TX IQ looped back on RX) | off |
| `-v, --verbose` | Debug logging | off |

## Rust version

A native Rust port of the Protocol 1 emulator, located in the `rust/` directory.

### Requirements

- Rust toolchain ([rustup](https://rustup.rs/))

### Build

```bash
cd rust
cargo build --release
```

The binary is at `rust/target/release/hpsdr-emu`.

### Usage

```bash
# Hermes Lite (2 DDCs)
cargo run --release -- --radio hermeslite

# Orion MKII with debug logging
cargo run --release -- --radio orionmkii -v

# Custom tone, noise, and echo mode
cargo run --release -- --radio hermes --freq 800 --noise 1e-5 --echo

# Fixed MAC address
cargo run --release -- --radio angelia --mac 00:1c:c0:a2:22:5e
```

### Options

| Flag | Description | Default |
|------|-------------|---------|
| `--radio TYPE` | Hardware type to emulate (required) | — |
| `--mac MAC` | MAC address (hex) | random |
| `--freq HZ` | Test tone offset from center | `1000` |
| `--noise LEVEL` | Noise level as fraction of full-scale | `3e-6` |
| `--echo` | Echo mode (TX IQ looped back on RX) | off |
| `-v, --verbose` | Debug logging | off |

### Supported radio types

| Name | Code | Max DDCs |
|------|------|----------|
| `atlas` | 0 | 2 |
| `hermes` | 1 | 4 |
| `hermesii` | 2 | 4 |
| `angelia` | 3 | 5 |
| `orion` | 4 | 5 |
| `orionmkii` | 5 | 8 |
| `hermeslite` | 6 | 2 |
| `saturn` | 10 | 10 |
| `saturnmkii` | 11 | 10 |

## What it does

### Protocol 1 (port 1024)

- Responds to broadcast discovery requests
- Handles start/stop streaming commands
- Parses C0–C4 control bytes (sample rate, frequencies, TX drive)
- Streams 1032-byte data packets with 24-bit I/Q samples interleaved with 16-bit mic data
- Reports exciter/forward/reverse power and supply voltage during TX (addresses 0x08, 0x10, 0x18)
- Echo mode: records TX IQ on PTT and loops it back on RX with frequency shifting

### Protocol 2 (ports 1024–1029)

- Responds to discovery on port 1024
- Accepts general, RX-specific, TX-specific, and high-priority commands
- Accepts TX audio (port 1028) and TX IQ (port 1029) — logged and discarded
- Streams per-DDC I/Q data on ports 1035+ (1444-byte packets, 238 samples, 24-bit)
- Sends high-priority status at 10 Hz on port 1025
- Sends mic silence on port 1026

## Known limitations

- **Protocol 2 TX is non-functional** (Python): TX audio and TX IQ packets are received and logged but not processed. Echo mode is not supported in Protocol 2.
- **Power/SWR metering not working** (Protocol 1, both implementations): Forward and reverse power ADC values are sent in the control response bytes (addresses 0x08, 0x10) but the values or encoding are incorrect — Thetis does not show power or SWR readings.
- **Echo position lost on frequency change** (both implementations): When the RX frequency is changed, previously recorded echoes are keyed by their original TX frequency but the frequency-shift playback does not account for the new tuning offset correctly, causing echoes to appear at the wrong position or disappear.

## Project structure

```
src/hpsdr_emu/               # Python implementation
├── __main__.py              # CLI and async entry point
├── radio.py                 # Radio state, signal generator, HPSDRHW enum
├── protocol1.py             # Protocol 1 UDP server
└── protocol2.py             # Protocol 2 multi-port UDP server

rust/                        # Rust implementation (Protocol 1 only)
├── Cargo.toml
└── src/
    ├── main.rs              # CLI (clap) + tokio runtime + signal handling
    ├── radio.rs             # HPSDRHW enum, RadioState, SignalGenerator, EchoBuffer
    └── protocol1.rs         # Protocol 1 UDP server
```

## Protocol reference

See [protocols.md](protocols.md) for the full protocol specification verified against the Thetis source code.
