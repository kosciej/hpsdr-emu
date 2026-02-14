"""Protocol 1 (Legacy) OpenHPSDR emulator.

Listens on UDP port 1024. Handles discovery, start/stop, control commands,
and streams I/Q data back to the client.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from .radio import (
    SAMPLE_RATES_P1,
    EchoBuffer,
    RadioState,
    SignalGenerator,
    unpack_tx_iq_16bit,
)

logger = logging.getLogger(__name__)

PORT = 1024
PACKET_SIZE = 1032
SUBFRAME_SIZE = 512
SYNC = b"\x7f\x7f\x7f"

# Response C0 addresses the radio rotates through (matched to Thetis parsing)
_RESPONSE_ADDRS = [0x00, 0x08, 0x10, 0x18]


class Protocol1Server(asyncio.DatagramProtocol):
    """Protocol 1 UDP handler."""

    def __init__(
        self,
        state: RadioState,
        siggen: SignalGenerator,
        echo: EchoBuffer | None = None,
    ) -> None:
        self.state = state
        self.siggen = siggen
        self.echo = echo
        self.transport: asyncio.DatagramTransport | None = None
        self.client_addr: tuple[str, int] | None = None
        self._stream_task: asyncio.Task | None = None
        self._control_idx = 0  # rotating C0 index for responses

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 4:
            return

        # Check magic bytes
        if data[0] != 0xEF or data[1] != 0xFE:
            return

        cmd = data[2]

        if cmd == 0x02:
            # Discovery request
            self._handle_discovery(addr)
        elif cmd == 0x04:
            # Start/stop
            if len(data) > 3:
                if data[3] == 0x01:
                    self._handle_start(addr)
                elif data[3] == 0x00:
                    self._handle_stop()
        elif cmd == 0x01:
            # Data packet from host (contains C0-C4 control commands)
            self._handle_host_data(data, addr)

    def _handle_discovery(self, addr: tuple[str, int]) -> None:
        logger.info("P1 Discovery request from %s", addr)
        resp = self._build_discovery_response()
        self.transport.sendto(resp, addr)
        logger.info("P1 Discovery response sent (%d bytes)", len(resp))

    def _build_discovery_response(self) -> bytes:
        """Build 60-byte Protocol 1 discovery response."""
        buf = bytearray(60)
        s = self.state

        buf[0] = 0xEF
        buf[1] = 0xFE
        buf[2] = 0x02  # status: normal
        buf[3:9] = s.mac  # MAC address
        buf[9] = s.firmware_version
        buf[10] = s.hw.code  # device type
        buf[11] = 0  # protocol version (0 for P1)
        # bytes 12-13 reserved
        buf[14] = s.mercury_versions[0]
        buf[15] = s.mercury_versions[1]
        buf[16] = s.mercury_versions[2]
        buf[17] = s.mercury_versions[3]
        buf[18] = s.penny_version
        buf[19] = s.metis_version
        buf[20] = s.nddc  # number of receivers

        return bytes(buf)

    def _handle_start(self, addr: tuple[str, int]) -> None:
        logger.info("P1 Start streaming to %s", addr)
        self.client_addr = addr
        self.state.running = True
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = asyncio.ensure_future(self._stream_loop())

    def _handle_stop(self) -> None:
        logger.info("P1 Stop streaming")
        self.state.running = False
        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            self._stream_task = None

    def _handle_host_data(self, data: bytes, addr: tuple[str, int]) -> None:
        """Parse C0-C4 from both sub-frames in a host data packet."""
        if len(data) < PACKET_SIZE:
            return

        self.client_addr = addr

        # Parse both sub-frames
        for offset in (8, 520):
            sf = data[offset : offset + SUBFRAME_SIZE]
            if sf[0:3] != SYNC:
                continue
            c0, c1, c2, c3, c4 = sf[3], sf[4], sf[5], sf[6], sf[7]
            self._process_control(c0, c1, c2, c3, c4)

            # Extract TX IQ from sub-frame data (after 8-byte header)
            # 63 blocks of [L(2B) R(2B) I(2B) Q(2B)] = 504 bytes
            if self.echo is not None and self.state.ptt:
                tx_data = sf[8 : 8 + 63 * 8]
                if len(tx_data) == 63 * 8:
                    tx_iq = unpack_tx_iq_16bit(tx_data)
                    self.echo.feed(tx_iq)

    def _process_control(self, c0: int, c1: int, c2: int, c3: int, c4: int) -> None:
        """Process a C0-C4 control command from the host.

        C0 bit 0 = MOX (PTT), bits [7:1] = command address.
        """
        s = self.state

        # Extract MOX from bit 0, address from remaining bits
        mox = bool(c0 & 0x01)
        addr = c0 & 0xFE

        if mox != s.ptt:
            logger.info("P1 MOX -> %s", mox)
            s.ptt = mox
            if self.echo is not None:
                if mox:
                    self.echo.start_recording(s.tx_frequency)
                else:
                    self.echo.stop_recording()

        if addr == 0x00:
            # Sample rate
            rate_code = c1 & 0x03
            for rate, code in SAMPLE_RATES_P1.items():
                if code == rate_code:
                    if s.sample_rate != rate:
                        logger.info("P1 Sample rate -> %d Hz", rate)
                        s.sample_rate = rate
                        self.siggen.sample_rate = rate
                    break
            # Number of receivers: C4 bits [5:3] = (nddc - 1)
            nddc = ((c4 >> 3) & 0x07) + 1
            if nddc != s.nddc:
                logger.info("P1 Active DDCs -> %d", nddc)
                s.nddc = nddc
        elif addr == 0x02:
            # TX frequency
            freq = struct.unpack(">I", bytes([c1, c2, c3, c4]))[0]
            if s.tx_frequency != freq:
                logger.info("P1 TX freq -> %d Hz", freq)
                s.tx_frequency = freq
        elif addr in range(0x04, 0x12, 2):
            # RX frequencies: 0x04=RX0, 0x06=RX1, 0x08=RX2, ...
            ddc_idx = (addr - 0x04) // 2
            freq = struct.unpack(">I", bytes([c1, c2, c3, c4]))[0]
            if ddc_idx < len(s.rx_frequencies) and s.rx_frequencies[ddc_idx] != freq:
                logger.info("P1 RX%d freq -> %d Hz", ddc_idx, freq)
                s.rx_frequencies[ddc_idx] = freq
        elif addr == 0x12:
            # TX drive level in C1
            drive = c1
            if s.tx_drive != drive:
                logger.info("P1 TX drive -> %d", drive)
                s.tx_drive = drive

    async def _stream_loop(self) -> None:
        """Stream I/Q data packets to the client."""
        logger.info(
            "P1 Streaming started (nddc=%d, rate=%d)",
            self.state.nddc,
            self.state.sample_rate,
        )
        try:
            while self.state.running and self.client_addr:
                packet = self._build_data_packet()
                self.transport.sendto(packet, self.client_addr)

                # Calculate sleep time based on samples per packet
                nddc = max(1, self.state.nddc)
                spr = 504 // (6 * nddc + 2)
                samples_per_packet = spr * 2  # 2 sub-frames
                interval = samples_per_packet / self.state.sample_rate
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            logger.info("P1 Streaming cancelled")
        except Exception:
            logger.exception("P1 Streaming error")

    def _build_data_packet(self) -> bytes:
        """Build a 1032-byte Protocol 1 data packet."""
        buf = bytearray(PACKET_SIZE)
        s = self.state
        seq = s.next_seq("p1_data")

        # Header
        buf[0] = 0xEF
        buf[1] = 0xFE
        buf[2] = 0x01  # data packet
        buf[3] = 0x06  # endpoint 6
        struct.pack_into(">I", buf, 4, seq)

        # Build both sub-frames
        for sf_offset in (8, 520):
            self._fill_subframe(buf, sf_offset)

        return bytes(buf)

    def _fill_subframe(self, buf: bytearray, offset: int) -> None:
        """Fill a 512-byte sub-frame with sync, control response, and I/Q data."""
        s = self.state
        nddc = max(1, s.nddc)
        spr = 504 // (6 * nddc + 2)

        # Sync bytes
        buf[offset] = 0x7F
        buf[offset + 1] = 0x7F
        buf[offset + 2] = 0x7F

        # Control response bytes (C0-C4)
        # Rotate through the 4 response addresses Thetis expects
        c0_addr = _RESPONSE_ADDRS[self._control_idx % len(_RESPONSE_ADDRS)]
        self._control_idx = (self._control_idx + 1) % len(_RESPONSE_ADDRS)

        ptt_bit = 1 if s.ptt else 0
        c0 = c0_addr | 0x80 | ptt_bit
        buf[offset + 3] = c0

        # Fill C1-C4 based on response address
        if c0_addr == 0x00:
            # C1: ADC overflow (bit 0) — none
            # C2: Mercury software version
            # C3: Penny version
            # C4: reserved
            buf[offset + 4] = 0x00
            buf[offset + 5] = s.firmware_version
            buf[offset + 6] = s.penny_version
            buf[offset + 7] = 0x00
        elif c0_addr == 0x08:
            # C1-C2: Exciter power (AIN5), C3-C4: Forward power (AIN1)
            if s.ptt:
                exc = s.tx_drive * 10
                fwd = (s.tx_drive * s.tx_drive) >> 4
            else:
                exc = 0
                fwd = 0
            struct.pack_into(">HH", buf, offset + 4, exc, fwd)
        elif c0_addr == 0x10:
            # C1-C2: Reverse power (AIN2), C3-C4: PA volts (AIN3)
            if s.ptt and s.tx_drive > 0:
                fwd = (s.tx_drive * s.tx_drive) >> 4
                rev = max(1, fwd // 50)
            else:
                rev = 0
            supply = 3200  # ~13.2 V equivalent
            struct.pack_into(">HH", buf, offset + 4, rev, supply)
        elif c0_addr == 0x18:
            # C1-C2: PA current (AIN4), C3-C4: Supply volts (AIN6)
            pa_amps = s.tx_drive * 5 if s.ptt else 0
            supply = 3200
            struct.pack_into(">HH", buf, offset + 4, pa_amps, supply)
        else:
            buf[offset + 4 : offset + 8] = b"\x00\x00\x00\x00"

        # Generate samples in batch per DDC, then interleave into sub-frame
        ddc_samples = []
        for ddc in range(nddc):
            if self.echo is not None:
                iq = self.echo.generate_echo(spr, s.rx_frequencies[ddc], s.sample_rate)
            else:
                iq = self.siggen.generate_iq(spr, ddc)
            ddc_samples.append(iq)

        # Pack interleaved: [I(3B) Q(3B)] × nddc + [Mic(2B)] per sample row
        data_offset = offset + 8
        for row in range(spr):
            for ddc in range(nddc):
                sample = ddc_samples[ddc][row]
                iv = int(max(-1.0, min(1.0, sample.real)) * 8388607) & 0xFFFFFF
                qv = int(max(-1.0, min(1.0, sample.imag)) * 8388607) & 0xFFFFFF
                buf[data_offset] = (iv >> 16) & 0xFF
                buf[data_offset + 1] = (iv >> 8) & 0xFF
                buf[data_offset + 2] = iv & 0xFF
                buf[data_offset + 3] = (qv >> 16) & 0xFF
                buf[data_offset + 4] = (qv >> 8) & 0xFF
                buf[data_offset + 5] = qv & 0xFF
                data_offset += 6
            # Mic: 2 bytes silence
            buf[data_offset] = 0
            buf[data_offset + 1] = 0
            data_offset += 2


async def run_protocol1(
    state: RadioState,
    siggen: SignalGenerator,
    echo: EchoBuffer | None = None,
) -> None:
    """Start Protocol 1 server."""
    loop = asyncio.get_running_loop()
    transport, protocol = await loop.create_datagram_endpoint(
        lambda: Protocol1Server(state, siggen, echo),
        local_addr=("0.0.0.0", PORT),
    )
    logger.info("Protocol 1 listening on UDP port %d", PORT)
    logger.info(
        "Radio: %s (code=%d, DDCs=%d)",
        state.hw.name,
        state.hw.code,
        state.nddc,
    )
    logger.info("MAC: %s", ":".join(f"{b:02x}" for b in state.mac))

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        transport.close()
