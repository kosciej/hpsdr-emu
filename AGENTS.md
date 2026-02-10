# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

OpenHPSDR radio emulator with two implementations: Python (Protocol 1 + 2) and Rust (Protocol 1 only). Emulates HPSDR hardware (Hermes, Angelia, Orion, etc.) over UDP so SDR applications like Thetis and deskHPSDR can discover and connect to it. Generates test tone + noise IQ data on all RX DDC channels. Supports echo mode (TX IQ recorded and looped back on RX) and reports TX power/SWR feedback.

## Commands

### Python

```bash
uv sync                                           # install dependencies
uv run hpsdr-emu --protocol 1 --radio hermeslite   # run Protocol 1
uv run hpsdr-emu --protocol 2 --radio orionmkii    # run Protocol 2
uv run hpsdr-emu --protocol 1 --radio hermes --echo # echo mode
uv run hpsdr-emu --protocol 1 --radio hermes -v    # debug logging
uv run ruff check .                                # lint
uv run ruff format .                               # format
uv run pytest                                      # tests (when they exist)
uv run pytest tests/test_radio.py::test_name       # single test
```

### Rust

```bash
cd rust
cargo build --release                              # build
cargo run --release -- --radio hermeslite           # run
cargo run --release -- --radio hermes --echo -v     # echo + debug
cargo test                                         # tests (when they exist)
```

## Architecture

### Python — `src/hpsdr_emu/`

- **`radio.py`** — Shared state and signal generation. `HPSDRHW` enum maps radio types to (board_code, max_ddcs). `RadioState` dataclass holds mutable config (frequencies, sample rate, running state, sequence counters). `SignalGenerator` produces complex IQ samples (tone + Gaussian noise) with per-DDC phase accumulators. `EchoBuffer` records TX IQ during PTT and loops it back on RX with frequency shifting and 80 dB attenuation. `pack_iq_24bit_fast()` converts float IQ to 24-bit big-endian wire format.

- **`protocol1.py`** — Single UDP socket on port 1024. `Protocol1Server` is an `asyncio.DatagramProtocol`. Handles discovery (0xEF 0xFE 0x02), start/stop (0x04), and host data packets (0x01) containing C0-C4 control bytes. Streams 1032-byte packets with two 512-byte sub-frames, each containing sync + control response + interleaved `[I(3B) Q(3B)] x nddc + [Mic(2B)]` repeated `spr` times. Control responses rotate through addresses 0x00/0x08/0x10/0x18 to provide firmware info and TX power/SWR feedback.

- **`protocol2.py`** — Multi-port UDP. `_PortHandler` delegates to `Protocol2Server` which dispatches by port. Inbound: general/discovery (1024), RX config (1025), TX config (1026), high-priority commands (1027), TX audio/IQ (1028-1029). Outbound streams: high-priority status at 10 Hz (port 1025), per-DDC IQ on ports 1035+ (1444-byte packets, 238 samples), mic silence on port 1026.

- **`__main__.py`** — CLI via argparse, creates RadioState + SignalGenerator + optional EchoBuffer, runs the selected protocol with graceful shutdown handling.

### Rust — `rust/src/` (Protocol 1 only)

Direct port of the Python Protocol 1 implementation using tokio async runtime. Mirrors the same module split:

- **`radio.rs`** — `HpsdrHw` enum, `RadioState`, `SignalGenerator`, `EchoBuffer`, IQ pack/unpack functions. Shared state wrapped in `Arc<Mutex<T>>`.

- **`protocol1.rs`** — `Protocol1Server` with `tokio::select!` loop for concurrent recv + timed streaming. Same packet format, control parsing, and response generation as Python.

- **`main.rs`** — CLI via clap derive, tokio runtime, Ctrl+C shutdown.

## Key Data Flow

1. Client sends discovery -> emulator replies with device info (60 bytes)
2. Client sends start/run command -> emulator begins streaming IQ data
3. `SignalGenerator.generate_iq()` -> 24-bit IQ packing -> UDP packet to client
4. Client sends control commands (frequencies, sample rate) -> `RadioState` updated
5. When echo mode: PTT on -> record TX IQ; PTT off -> commit to loop buffer; RX reads from loop with frequency shift

## Protocol 1 Control Response Addresses

The radio-to-host response C0 byte = `address | 0x80 | ptt_bit`. Thetis parses these specific addresses:

| Address | C1-C2 | C3-C4 |
|---------|-------|-------|
| 0x00 | ADC overflow, Mercury FW ver | Penny ver |
| 0x08 | Exciter power (AIN5) | Forward power (AIN1) |
| 0x10 | Reverse power (AIN2) | PA volts (AIN3) |
| 0x18 | PA current (AIN4) | Supply volts (AIN6) |

## Protocol Gotchas

- **Protocol 1 response addresses**: Thetis parses addresses 0x00/0x08/0x10/0x18 (not 0x02/0x04/0x06). The response C0 is `addr | 0x80 | ptt_bit`, and Thetis masks with `& 0x7E` to extract the address.
- **Protocol 1 NDC count**: Host sends active DDC count in C4[5:3] of C0=0x00 command. Sub-frame interleaving block size depends on nddc, so a mismatch corrupts all sample data.
- **Echo frequency shift phase**: The complex exponential shift in `generate_echo()` must maintain a per-frequency phase accumulator across calls. Without this, every buffer boundary (called hundreds of times/sec) produces a phase discontinuity that ruins audio quality.
- **Protocol 2 sample rate is in kHz**: The RX-specific packet (port 1025) encodes sample rate in kHz (e.g., 192 means 192000 Hz). Must multiply by 1000 when parsing.
- **Protocol 2 source port routing**: deskHPSDR demultiplexes incoming data by source port (1025=HP status, 1026=mic, 1035+=DDC IQ). The emulator must send from correct source ports.

## Protocol Reference

`protocols.md` contains the full protocol specification verified against Thetis and deskHPSDR source code. Consult this when modifying packet formats or adding new command handling.

## Code Conventions

### Python
- Python >= 3.12, `from __future__ import annotations` in all files
- Type unions: `X | None` not `Optional[X]`
- Relative imports within package: `from .radio import RadioState`
- Module-level loggers: `logger = logging.getLogger(__name__)`
- Binary protocol: `struct.pack()`/`struct.pack_into()` with `bytearray`, named constants for magic numbers
- Numpy for vectorized signal generation; performance-critical packing uses manual byte operations

### Rust
- Edition 2021, async with tokio
- Shared state via `Arc<Mutex<T>>` between recv and stream tasks
- `num-complex::Complex<f64>` for IQ samples
- `log` + `env_logger` for logging (mirrors Python's `logging` module)
- CLI via `clap` derive macros
