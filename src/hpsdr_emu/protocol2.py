"""Protocol 2 (Modern/Ethernet) OpenHPSDR emulator.

Listens on UDP ports 1024-1029 for host commands.
Sends DDC IQ data on ports 1035+, high-priority status on 1025, mic on 1026.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import time

import numpy as np

from .radio import (
    EchoBuffer,
    RadioState,
    SignalGenerator,
    pack_iq_24bit_fast,
    unpack_tx_audio_16bit,
    unpack_tx_iq_24bit,
)

logger = logging.getLogger(__name__)

# Host -> Radio ports
PORT_GENERAL = 1024
PORT_RX_SPECIFIC = 1025
PORT_TX_SPECIFIC = 1026
PORT_HIGH_PRIORITY = 1027
PORT_TX_AUDIO = 1028
PORT_TX_IQ = 1029

# Radio -> Host ports
PORT_HP_STATUS = 1025
PORT_MIC = 1026
PORT_DDC_BASE = 1035

SAMPLES_PER_DDC_PACKET = 238
SAMPLES_PER_MIC_PACKET = 64
HP_STATUS_INTERVAL = 0.1  # 10 Hz


class _PortHandler(asyncio.DatagramProtocol):
    """Base UDP handler that dispatches to the Protocol2Server."""

    def __init__(self, server: Protocol2Server, port: int) -> None:
        self.server = server
        self.port = port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.DatagramTransport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.server.handle_packet(self.port, data, addr)


class Protocol2Server:
    """Protocol 2 multi-port UDP handler."""

    def __init__(
        self,
        state: RadioState,
        siggen: SignalGenerator,
        echo: EchoBuffer | None = None,
    ) -> None:
        self.state = state
        self.siggen = siggen
        self.echo = echo
        self.client_addr: tuple[str, int] | None = None
        self._handlers: dict[int, _PortHandler] = {}
        self._stream_tasks: list[asyncio.Task] = []
        # Per-source-port send sockets (deskHPSDR demuxes by source port)
        self._send_sockets: dict[int, asyncio.DatagramTransport] = {}
        # Echo TX detection: TX data presence with timeout
        self._echo_tx_active: bool = False
        self._echo_tx_timer: asyncio.TimerHandle | None = None

    async def start(self) -> None:
        """Bind all ports and start listening."""
        loop = asyncio.get_running_loop()

        ports = [
            PORT_GENERAL,
            PORT_RX_SPECIFIC,
            PORT_TX_SPECIFIC,
            PORT_HIGH_PRIORITY,
            PORT_TX_AUDIO,
            PORT_TX_IQ,
        ]

        for port in ports:
            handler = _PortHandler(self, port)
            transport, _ = await loop.create_datagram_endpoint(
                lambda h=handler: h,
                local_addr=("0.0.0.0", port),
            )
            handler.transport = transport
            self._handlers[port] = handler
            logger.info("Protocol 2 listening on UDP port %d", port)

        # Set up per-source-port send sockets.
        # deskHPSDR demultiplexes incoming packets by source port:
        #   1025 = HP status, 1026 = mic, 1035+ = DDC IQ
        # Ports 1025/1026 are already bound by receive handlers — reuse them.
        self._send_sockets[PORT_HP_STATUS] = self._handlers[PORT_RX_SPECIFIC].transport
        self._send_sockets[PORT_MIC] = self._handlers[PORT_TX_SPECIFIC].transport
        # DDC IQ ports (1035+) need new sockets.
        for ddc in range(self.state.nddc):
            sport = PORT_DDC_BASE + ddc
            transport, _ = await loop.create_datagram_endpoint(
                asyncio.DatagramProtocol,
                local_addr=("0.0.0.0", sport),
            )
            self._send_sockets[sport] = transport
            logger.info("Protocol 2 send socket on port %d", sport)

        logger.info(
            "Radio: %s (code=%d, DDCs=%d)",
            self.state.hw.name,
            self.state.hw.code,
            self.state.nddc,
        )
        logger.info("MAC: %s", ":".join(f"{b:02x}" for b in self.state.mac))

    def handle_packet(self, port: int, data: bytes, addr: tuple[str, int]) -> None:
        """Dispatch incoming packet by port."""
        if port == PORT_GENERAL:
            self._handle_general(data, addr)
        elif port == PORT_RX_SPECIFIC:
            self._handle_rx_specific(data, addr)
        elif port == PORT_TX_SPECIFIC:
            self._handle_tx_specific(data, addr)
        elif port == PORT_HIGH_PRIORITY:
            self._handle_high_priority(data, addr)
        elif port == PORT_TX_AUDIO:
            self._handle_tx_audio(data, addr)
        elif port == PORT_TX_IQ:
            self._handle_tx_iq(data, addr)

    # --- Port handlers ---

    def _handle_general(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 5:
            return

        # Check if discovery (byte 4 == 0x02)
        if data[4] == 0x02:
            logger.info("P2 Discovery request from %s", addr)
            resp = self._build_discovery_response()
            self._handlers[PORT_GENERAL].transport.sendto(resp, addr)
            logger.info("P2 Discovery response sent")
        elif data[4] == 0x00:
            logger.debug("P2 General config from %s", addr)
            self.client_addr = addr

    def _build_discovery_response(self) -> bytes:
        """Build 60-byte Protocol 2 discovery response."""
        buf = bytearray(60)
        s = self.state

        # Bytes 0-3: zeros (header)
        buf[4] = 0x02  # status: normal
        buf[5:11] = s.mac  # MAC address
        buf[11] = s.hw.code  # board type
        buf[12] = 1  # protocol version (1 for P2)
        buf[13] = s.firmware_version
        buf[14] = s.mercury_versions[0]
        buf[15] = s.mercury_versions[1]
        buf[16] = s.mercury_versions[2]
        buf[17] = s.mercury_versions[3]
        buf[18] = s.penny_version
        buf[19] = s.metis_version
        buf[20] = s.nddc  # number of receivers

        return bytes(buf)

    def _handle_rx_specific(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 5:
            return
        self.client_addr = addr
        logger.debug("P2 RX-specific config from %s (%d bytes)", addr, len(data))

        # Parse enabled receivers (byte 7)
        if len(data) > 7:
            enabled_bits = data[7]
            count = bin(enabled_bits).count("1")
            if count > 0 and count != self.state.nddc:
                logger.info("P2 Enabled RXs: %d (bits=0x%02x)", count, enabled_bits)

        # Parse per-receiver sample rate from byte 18-19 (RX0)
        # Value is in kHz (e.g., 192 for 192000 Hz)
        if len(data) > 19:
            sr_khz = struct.unpack(">H", data[18:20])[0]
            if sr_khz > 0:
                sr_hz = sr_khz * 1000
                if sr_hz != self.state.sample_rate:
                    logger.info("P2 RX0 sample rate -> %d Hz", sr_hz)
                    self.state.sample_rate = sr_hz
                    self.siggen.sample_rate = sr_hz

    def _handle_tx_specific(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client_addr = addr
        logger.debug("P2 TX-specific config from %s (%d bytes)", addr, len(data))

    def _handle_high_priority(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) < 57:
            return
        self.client_addr = addr

        # Byte 4: run (bit 0), PTT (bit 1)
        flags = data[4]
        run = bool(flags & 0x01)
        ptt = bool(flags & 0x02)

        if ptt != self.state.ptt:
            logger.info("P2 PTT -> %s", ptt)
            self.state.ptt = ptt
            if self.echo is not None and not ptt and self._echo_tx_active:
                self._cancel_echo_tx_timer()
                self._echo_tx_active = False
                self.echo.stop_recording()

        # RX frequencies: bytes 9-56 (12 × 4 bytes)
        for i in range(12):
            off = 9 + i * 4
            if off + 4 <= len(data):
                freq = struct.unpack(">I", data[off : off + 4])[0]
                if freq > 0 and i < len(self.state.rx_frequencies):
                    if self.state.rx_frequencies[i] != freq:
                        logger.info("P2 RX%d freq -> %d Hz", i, freq)
                        self.state.rx_frequencies[i] = freq

        # TX frequency at byte 329
        if len(data) > 332:
            tx_freq = struct.unpack(">I", data[329:333])[0]
            if tx_freq > 0 and self.state.tx_frequency != tx_freq:
                logger.info("P2 TX freq -> %d Hz", tx_freq)
                self.state.tx_frequency = tx_freq

        # TX drive at byte 345
        if len(data) > 345:
            drive = data[345]
            if self.state.tx_drive != drive:
                logger.info("P2 TX drive -> %d", drive)
                self.state.tx_drive = drive

        # Handle run state change
        if run and not self.state.running:
            self.state.running = True
            logger.info("P2 RUN -> started")
            self._start_streaming()
        elif not run and self.state.running:
            self.state.running = False
            logger.info("P2 RUN -> stopped")
            self._stop_streaming()

    def _handle_tx_audio(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client_addr = addr
        if self.echo is None or not self.state.ptt or len(data) <= 4:
            return
        payload = data[4:]
        # Detect format by payload size:
        # 1440 bytes = 240 × 6B (24-bit I/Q), 256 bytes = 64 × 4B (16-bit L/R)
        if len(payload) % 6 == 0 and len(payload) >= 6 * 60:
            tx_iq = unpack_tx_iq_24bit(payload)
        elif len(payload) % 4 == 0:
            tx_iq = unpack_tx_audio_16bit(payload)
        else:
            return
        self._echo_feed_tx(tx_iq)

    def _handle_tx_iq(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client_addr = addr
        if self.echo is None or not self.state.ptt or len(data) <= 4:
            return
        tx_iq = unpack_tx_iq_24bit(data[4:])
        self._echo_feed_tx(tx_iq)

    def _echo_feed_tx(self, tx_iq: np.ndarray) -> None:
        """Feed TX samples to echo buffer. PTT already verified by caller."""
        if not self._echo_tx_active:
            self._echo_tx_active = True
            self.echo.start_recording(self.state.tx_frequency)
        self.echo.feed(tx_iq)
        # Fallback timeout for abrupt client disconnect
        self._reset_echo_tx_timer()

    def _reset_echo_tx_timer(self) -> None:
        """Reset the fallback TX data timeout."""
        if self._echo_tx_timer is not None:
            self._echo_tx_timer.cancel()
        loop = asyncio.get_event_loop()
        self._echo_tx_timer = loop.call_later(1.0, self._echo_tx_timeout)

    def _cancel_echo_tx_timer(self) -> None:
        if self._echo_tx_timer is not None:
            self._echo_tx_timer.cancel()
            self._echo_tx_timer = None

    def _echo_tx_timeout(self) -> None:
        """Fallback: no TX data for 1s → stop recording (client disconnect)."""
        if self._echo_tx_active:
            self._echo_tx_active = False
            self.echo.stop_recording()
            logger.info("P2 Echo: TX data timeout (fallback), recording stopped")

    # --- Streaming ---

    def _start_streaming(self) -> None:
        self._stop_streaming()

        # High-priority status at 10 Hz
        self._stream_tasks.append(asyncio.ensure_future(self._hp_status_loop()))

        # DDC IQ streams
        for ddc in range(self.state.nddc):
            self._stream_tasks.append(asyncio.ensure_future(self._ddc_iq_loop(ddc)))

        # Mic samples
        self._stream_tasks.append(asyncio.ensure_future(self._mic_loop()))

        logger.info(
            "P2 Started %d stream tasks (nddc=%d)",
            len(self._stream_tasks),
            self.state.nddc,
        )

    def _stop_streaming(self) -> None:
        for task in self._stream_tasks:
            if not task.done():
                task.cancel()
        self._stream_tasks.clear()

    def _send_to_client(self, source_port: int, data: bytes) -> None:
        """Send data to client FROM the specified source port."""
        sock = self._send_sockets.get(source_port)
        if self.client_addr and sock:
            sock.sendto(data, self.client_addr)

    async def _hp_status_loop(self) -> None:
        """Send high-priority status to host on port 1025 at 10 Hz."""
        try:
            while self.state.running:
                pkt = self._build_hp_status()
                self._send_to_client(PORT_HP_STATUS, pkt)
                await asyncio.sleep(HP_STATUS_INTERVAL)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("P2 HP status loop error")

    def _build_hp_status(self) -> bytes:
        """Build high-priority status response (60 bytes).

        Layout (Radio -> Host, port 1025):
          Bytes 0-3:   Sequence number (32-bit BE)
          Byte 4:      PTT (bit 0), Dot (bit 1), Dash (bit 2)
          Byte 5:      ADC overload flags
          Bytes 6-7:   Exciter power (16-bit BE)
          Bytes 14-15: Forward power (16-bit BE)
          Bytes 22-23: Reverse power (16-bit BE)
        """
        buf = bytearray(60)
        seq = self.state.next_seq("hp_status")
        struct.pack_into(">I", buf, 0, seq)
        buf[4] = 1 if self.state.ptt else 0  # PTT in bit 0

        s = self.state
        if s.ptt and s.tx_drive > 0:
            exc = s.tx_drive * 10
            fwd = (s.tx_drive * s.tx_drive) >> 4
            rev = max(1, fwd // 50)
            struct.pack_into(">H", buf, 6, exc)
            struct.pack_into(">H", buf, 14, fwd)
            struct.pack_into(">H", buf, 22, rev)

        return bytes(buf)

    async def _ddc_iq_loop(self, ddc_index: int) -> None:
        """Stream DDC IQ data to host from source port 1035+ddc_index."""
        source_port = PORT_DDC_BASE + ddc_index
        stream_name = f"ddc_{ddc_index}"
        logger.info("P2 DDC%d IQ stream from port %d", ddc_index, source_port)

        try:
            while self.state.running:
                pkt = self._build_ddc_iq_packet(ddc_index, stream_name)
                self._send_to_client(source_port, pkt)

                interval = SAMPLES_PER_DDC_PACKET / self.state.sample_rate
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("P2 DDC%d IQ loop error", ddc_index)

    def _build_ddc_iq_packet(self, ddc_index: int, stream_name: str) -> bytes:
        """Build 1444-byte DDC IQ packet.

        Header (16 bytes):
          0-3:   Sequence number (32-bit BE)
          4-11:  Timestamp (64-bit BE)
          12-13: Bits per sample (16-bit BE) = 24
          14-15: Samples per frame (16-bit BE) = 238

        Data (1428 bytes = 238 × 6):
          24-bit I + 24-bit Q per sample
        """
        seq = self.state.next_seq(stream_name)
        timestamp = int(time.time() * 1e6) & 0xFFFFFFFFFFFFFFFF

        header = struct.pack(
            ">IqHH",
            seq,
            timestamp,
            24,
            SAMPLES_PER_DDC_PACKET,
        )

        if self.echo is not None:
            iq = self.echo.generate_echo(
                SAMPLES_PER_DDC_PACKET,
                self.state.rx_frequencies[ddc_index],
                self.state.sample_rate,
            )
        else:
            iq = self.siggen.generate_iq(SAMPLES_PER_DDC_PACKET, ddc_index)
        data = pack_iq_24bit_fast(iq)

        return header + data

    async def _mic_loop(self) -> None:
        """Stream mic silence to host on port 1026."""
        try:
            while self.state.running:
                pkt = self._build_mic_packet()
                self._send_to_client(PORT_MIC, pkt)

                interval = SAMPLES_PER_MIC_PACKET / 48000  # mic always 48 kHz
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("P2 Mic loop error")

    def _build_mic_packet(self) -> bytes:
        """Build mic data packet (silence).

        4-byte seq header + 128 bytes (64 samples × 2 bytes) = 132 bytes.
        """
        seq = self.state.next_seq("mic")
        header = struct.pack(">I", seq)
        data = b"\x00" * (SAMPLES_PER_MIC_PACKET * 2)
        return header + data

    def close(self) -> None:
        self._stop_streaming()
        for handler in self._handlers.values():
            if handler.transport:
                handler.transport.close()
        # Close DDC send sockets (1035+); 1025/1026 are shared with handlers
        for sport, transport in self._send_sockets.items():
            if sport >= PORT_DDC_BASE:
                transport.close()


async def run_protocol2(
    state: RadioState,
    siggen: SignalGenerator,
    echo: EchoBuffer | None = None,
) -> None:
    """Start Protocol 2 server."""
    server = Protocol2Server(state, siggen, echo)
    await server.start()

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        server.close()
