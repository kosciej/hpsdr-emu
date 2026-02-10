"""CLI entry point for the HPSDR radio emulator."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal

from .radio import RADIO_CHOICES, EchoBuffer, RadioState, SignalGenerator


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="hpsdr-emu",
        description="OpenHPSDR Protocol 1 & 2 radio emulator",
    )
    parser.add_argument(
        "--protocol",
        type=int,
        choices=[1, 2],
        required=True,
        help="Protocol version (1=legacy, 2=modern)",
    )
    parser.add_argument(
        "--radio",
        type=str,
        choices=sorted(RADIO_CHOICES.keys()),
        default="hermeslite",
        help="Radio hardware type (default: hermeslite)",
    )
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="MAC address (hex, e.g. 00:1c:c0:a2:22:5e). Random if omitted.",
    )
    parser.add_argument(
        "--freq",
        type=float,
        default=1000.0,
        help="Test tone offset from center in Hz (default: 1000)",
    )
    parser.add_argument(
        "--noise",
        type=float,
        default=3e-6,
        help="Noise level as fraction of full-scale (default: 3e-6, ~-100 dBm)",
    )
    parser.add_argument(
        "--echo",
        action="store_true",
        help="Enable echo mode: TX IQ is recorded and looped back on RX",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Radio hardware type
    hw = RADIO_CHOICES[args.radio]

    # MAC address
    if args.mac:
        mac = bytes.fromhex(args.mac.replace(":", "").replace("-", ""))
        if len(mac) != 6:
            parser.error("MAC address must be 6 bytes")
    else:
        mac = RadioState.random_mac()

    # Default sample rate depends on protocol
    sample_rate = 48000 if args.protocol == 1 else 192000

    state = RadioState(
        hw=hw,
        mac=mac,
        sample_rate=sample_rate,
        nddc=hw.max_ddcs,
    )

    siggen = SignalGenerator(
        sample_rate=sample_rate,
        tone_offset_hz=args.freq,
        noise_level=args.noise,
    )

    echo = EchoBuffer(sample_rate=sample_rate) if args.echo else None

    logging.getLogger(__name__).info(
        "Starting HPSDR emulator: protocol=%d, radio=%s, tone=%.0f Hz, noise=%.2f, echo=%s",
        args.protocol,
        hw.name,
        args.freq,
        args.noise,
        "on" if echo else "off",
    )

    try:
        if args.protocol == 1:
            from .protocol1 import run_protocol1

            asyncio.run(_run_with_shutdown(run_protocol1(state, siggen, echo)))
        else:
            from .protocol2 import run_protocol2

            asyncio.run(_run_with_shutdown(run_protocol2(state, siggen, echo)))
    except KeyboardInterrupt:
        pass


async def _run_with_shutdown(coro) -> None:
    """Run coroutine with graceful SIGINT/SIGTERM handling."""
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _signal_handler():
        logging.getLogger(__name__).info("Shutting down...")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

    task = asyncio.ensure_future(coro)
    stop_task = asyncio.ensure_future(stop.wait())

    done, pending = await asyncio.wait(
        [task, stop_task], return_when=asyncio.FIRST_COMPLETED
    )

    for t in pending:
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    main()
