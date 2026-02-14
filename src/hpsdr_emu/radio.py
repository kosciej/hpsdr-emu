"""Shared radio state and signal generation for the HPSDR emulator."""

from __future__ import annotations

import enum
import logging
import os
import struct
from dataclasses import dataclass, field

import numpy as np

logger = logging.getLogger(__name__)


class HPSDRHW(enum.Enum):
    """Hardware types from Thetis HPSDRHW enum.

    Value is (board_code, max_ddcs).
    """

    ATLAS = (0, 2)
    HERMES = (1, 4)
    HERMESII = (2, 4)
    ANGELIA = (3, 5)
    ORION = (4, 5)
    ORIONMKII = (5, 8)
    HERMESLITE = (6, 2)
    SATURN = (10, 10)
    SATURNMKII = (11, 10)

    @property
    def code(self) -> int:
        return self.value[0]

    @property
    def max_ddcs(self) -> int:
        return self.value[1]


# Map CLI names to enum members
RADIO_CHOICES: dict[str, HPSDRHW] = {m.name.lower(): m for m in HPSDRHW}

# Sample rates and their Protocol 1 encoding
SAMPLE_RATES_P1 = {48000: 0, 96000: 1, 192000: 2, 384000: 3}


@dataclass
class RadioState:
    """Mutable radio configuration shared across protocol handlers."""

    hw: HPSDRHW
    mac: bytes  # 6 bytes
    firmware_version: int = 25
    protocol_version: int = 0  # overridden per protocol
    mercury_versions: tuple[int, int, int, int] = (25, 25, 25, 25)
    penny_version: int = 25
    metis_version: int = 25

    sample_rate: int = 48000
    nddc: int = 1  # active DDC count (set from hw.max_ddcs or commanded)
    rx_frequencies: list[int] = field(default_factory=lambda: [7074000] * 12)
    tx_frequency: int = 7074000
    tx_drive: int = 0
    running: bool = False
    ptt: bool = False

    # Sequence counters keyed by stream name
    seq: dict[str, int] = field(default_factory=dict)

    def next_seq(self, stream: str) -> int:
        val = self.seq.get(stream, 0)
        self.seq[stream] = (val + 1) & 0xFFFFFFFF
        return val

    @staticmethod
    def random_mac() -> bytes:
        b = os.urandom(6)
        # Set locally-administered bit, clear multicast bit
        return bytes([(b[0] | 0x02) & 0xFE]) + b[1:]


class SignalGenerator:
    """Generates test I/Q signals for each DDC."""

    def __init__(
        self,
        sample_rate: int = 48000,
        tone_offset_hz: float = 1000.0,
        noise_level: float = 3e-6,
        amplitude: float = 0.3,
    ):
        self.sample_rate = sample_rate
        self.tone_offset_hz = tone_offset_hz
        self.noise_level = noise_level
        self.amplitude = amplitude
        self._phase: dict[int, float] = {}  # per-DDC phase accumulator

    def generate_iq(self, n_samples: int, ddc_index: int = 0) -> np.ndarray:
        """Generate complex I/Q samples for one DDC.

        Returns array of complex128 with values in [-1, 1].
        """
        phase = self._phase.get(ddc_index, 0.0)
        t = (np.arange(n_samples) / self.sample_rate) + phase

        tone = self.amplitude * np.exp(2j * np.pi * self.tone_offset_hz * t)
        noise = self.noise_level * (
            np.random.randn(n_samples) + 1j * np.random.randn(n_samples)
        )
        iq = tone + noise

        self._phase[ddc_index] = phase + n_samples / self.sample_rate
        if self._phase[ddc_index] > 1e6:
            self._phase[ddc_index] %= (
                (1.0 / self.tone_offset_hz) if self.tone_offset_hz else 0.0
            )

        return iq

    def generate_iq_fft(
        self,
        n_samples: int,
        frequencies: list[float],
        amplitudes: list[float],
        phases: list[float] | None = None,
        noise_level: float | None = None,
    ) -> np.ndarray:
        """Generate I/Q signal using FFT/IFFT from spectral components.

        Args:
            n_samples: Number of samples to generate
            frequencies: List of frequencies in Hz for each spectral component
            amplitudes: List of amplitudes (0-1) for each component
            phases: Optional list of phases in radians for each component
            noise_level: Optional noise level, defaults to self.noise_level

        Returns:
            Complex I/Q array with values in [-1, 1]
        """
        if len(frequencies) != len(amplitudes):
            raise ValueError("frequencies and amplitudes must have same length")

        if phases is None:
            phases = [0.0] * len(frequencies)
        elif len(phases) != len(frequencies):
            raise ValueError("phases must have same length as frequencies")

        noise_level = noise_level if noise_level is not None else self.noise_level

        spectrum = np.zeros(n_samples, dtype=np.complex128)

        for freq, amp, phase in zip(frequencies, amplitudes, phases):
            bin_idx = int(round(freq * n_samples / self.sample_rate))
            if 0 <= bin_idx < n_samples // 2 + 1:
                spectrum[bin_idx] = amp * np.exp(1j * phase)
                if bin_idx > 0 and bin_idx < n_samples // 2:
                    spectrum[n_samples - bin_idx] = amp * np.exp(-1j * phase)

        iq = np.fft.irfft(spectrum, n=n_samples)

        iq_complex = iq + 1j * np.zeros(n_samples)
        max_val = np.max(np.abs(iq_complex))
        if max_val > 0:
            iq_complex = iq_complex / max_val * np.max(amplitudes)

        if noise_level > 0:
            noise = noise_level * (
                np.random.randn(n_samples) + 1j * np.random.randn(n_samples)
            )
            iq_complex = iq_complex + noise

        return iq_complex

    def generate_multi_tone(
        self,
        n_samples: int,
        tones: list[tuple[float, float, float]],
        noise_level: float | None = None,
    ) -> np.ndarray:
        """Generate multi-tone signal using FFT.

        Args:
            n_samples: Number of samples to generate
            tones: List of (frequency, amplitude, phase) tuples
            noise_level: Optional noise level

        Returns:
            Complex I/Q array
        """
        frequencies = [t[0] for t in tones]
        amplitudes = [t[1] for t in tones]
        phases = [t[2] for t in tones] if len(tones[0]) > 2 else None
        return self.generate_iq_fft(
            n_samples, frequencies, amplitudes, phases, noise_level
        )


def pack_iq_24bit(iq: np.ndarray) -> bytes:
    """Pack complex I/Q array into 24-bit big-endian bytes (3B I + 3B Q per sample)."""
    max_val = 8388607  # 2^23 - 1
    parts = []
    for sample in iq:
        i_val = int(np.clip(sample.real, -1.0, 1.0) * max_val)
        q_val = int(np.clip(sample.imag, -1.0, 1.0) * max_val)
        # Sign-extend to 24-bit and pack as 3 bytes big-endian
        parts.append(struct.pack(">i", i_val)[1:4])
        parts.append(struct.pack(">i", q_val)[1:4])
    return b"".join(parts)


def pack_iq_24bit_fast(iq: np.ndarray) -> bytes:
    """Vectorized version of pack_iq_24bit for better performance."""
    max_val = 8388607
    i_vals = np.clip(iq.real, -1.0, 1.0) * max_val
    q_vals = np.clip(iq.imag, -1.0, 1.0) * max_val
    i_ints = i_vals.astype(np.int32)
    q_ints = q_vals.astype(np.int32)

    result = bytearray(len(iq) * 6)
    for idx in range(len(iq)):
        iv = i_ints[idx] & 0xFFFFFF
        qv = q_ints[idx] & 0xFFFFFF
        off = idx * 6
        result[off] = (iv >> 16) & 0xFF
        result[off + 1] = (iv >> 8) & 0xFF
        result[off + 2] = iv & 0xFF
        result[off + 3] = (qv >> 16) & 0xFF
        result[off + 4] = (qv >> 8) & 0xFF
        result[off + 5] = qv & 0xFF
    return bytes(result)


def pack_silence_16bit(n_samples: int) -> bytes:
    """Pack n_samples of 16-bit silence (zeros)."""
    return b"\x00\x00" * n_samples


def unpack_tx_iq_16bit(data: bytes) -> np.ndarray:
    """Unpack Protocol 1 TX IQ from host sub-frame data.

    Each 8-byte block is [L(2B) R(2B) I(2B) Q(2B)], all big-endian signed.
    We extract I and Q (bytes 4-7 of each block).
    """
    n_blocks = len(data) // 8
    if n_blocks == 0:
        return np.empty(0, dtype=np.complex128)
    samples = np.empty(n_blocks, dtype=np.complex128)
    for k in range(n_blocks):
        off = k * 8
        i_val = struct.unpack_from(">h", data, off + 4)[0]
        q_val = struct.unpack_from(">h", data, off + 6)[0]
        samples[k] = (i_val + 1j * q_val) / 32768.0
    return samples


def unpack_tx_audio_16bit(data: bytes) -> np.ndarray:
    """Unpack Protocol 2 TX audio (16-bit L + 16-bit R per sample, big-endian).

    Treats L as real, R as imaginary to form complex IQ.
    """
    n_samples = len(data) // 4
    if n_samples == 0:
        return np.empty(0, dtype=np.complex128)
    samples = np.empty(n_samples, dtype=np.complex128)
    for k in range(n_samples):
        off = k * 4
        l_val = struct.unpack_from(">h", data, off)[0]
        r_val = struct.unpack_from(">h", data, off + 2)[0]
        samples[k] = (l_val + 1j * r_val) / 32768.0
    return samples


def unpack_tx_iq_24bit(data: bytes) -> np.ndarray:
    """Unpack Protocol 2 TX IQ (24-bit I + 24-bit Q per sample, big-endian)."""
    n_samples = len(data) // 6
    if n_samples == 0:
        return np.empty(0, dtype=np.complex128)
    samples = np.empty(n_samples, dtype=np.complex128)
    for k in range(n_samples):
        off = k * 6
        # 24-bit signed big-endian
        ib = data[off] << 16 | data[off + 1] << 8 | data[off + 2]
        if ib & 0x800000:
            ib -= 0x1000000
        qb = data[off + 3] << 16 | data[off + 4] << 8 | data[off + 5]
        if qb & 0x800000:
            qb -= 0x1000000
        samples[k] = (ib + 1j * qb) / 8388607.0
    return samples


class EchoBuffer:
    """Captures TX IQ and replays as looping echoes on RX."""

    ATTENUATION_DB = 80.0  # echo playback attenuation
    ATTENUATION = 10 ** (-ATTENUATION_DB / 20.0)  # ~0.0001

    def __init__(self, sample_rate: int = 48000, max_duration: float = 10.0):
        self.sample_rate = sample_rate
        self.max_duration = max_duration
        self._echoes: dict[int, np.ndarray] = {}  # freq -> looping IQ
        self._recording: list[np.ndarray] = []
        self._recording_freq: int = 0
        self._is_recording: bool = False
        self._playback_pos: dict[int, int] = {}  # per-freq read position
        self._shift_phase: dict[int, float] = {}  # per-freq angle accumulator (radians)

    def start_recording(self, tx_freq: int) -> None:
        """Begin recording TX IQ at the given frequency."""
        if self._is_recording:
            self._commit()
        self._recording = []
        self._recording_freq = tx_freq
        self._is_recording = True
        logger.info("Echo: recording started on %d Hz", tx_freq)

    def feed(self, samples: np.ndarray) -> None:
        """Append TX IQ samples during active recording."""
        if not self._is_recording or len(samples) == 0:
            return
        self._recording.append(samples.copy())

    def stop_recording(self) -> None:
        """Stop recording and commit to echo loop."""
        if self._is_recording:
            self._commit()
            self._is_recording = False

    def _commit(self) -> None:
        """Commit current recording to the echo dictionary."""
        if not self._recording:
            return
        freq = self._recording_freq
        if freq == 0:
            logger.debug("Echo: discarding recording with freq=0")
            self._recording = []
            return
        buf = np.concatenate(self._recording)
        max_samples = int(self.sample_rate * self.max_duration)
        if len(buf) > max_samples:
            buf = buf[:max_samples]
        if len(buf) == 0:
            return
        self._echoes[freq] = buf
        self._playback_pos[freq] = 0
        logger.info(
            "Echo: committed %d samples (%.2fs) on %d Hz",
            len(buf),
            len(buf) / self.sample_rate,
            freq,
        )
        self._recording = []

    def generate_echo(
        self, n_samples: int, rx_freq: int, sample_rate: int
    ) -> np.ndarray:
        """Generate mixed echo IQ for one DDC.

        Iterates all stored echoes, reads looping samples, frequency-shifts
        by (echo_freq - rx_freq), and sums. Skips echoes outside DDC bandwidth.
        """
        if not self._echoes:
            return np.zeros(n_samples, dtype=np.complex128)

        result = np.zeros(n_samples, dtype=np.complex128)
        half_bw = sample_rate / 2.0

        for freq, echo_buf in self._echoes.items():
            offset_hz = rx_freq - freq
            if abs(offset_hz) > half_bw:
                continue

            # Read looping samples
            pos = self._playback_pos.get(freq, 0)
            echo_len = len(echo_buf)
            chunk = np.empty(n_samples, dtype=np.complex128)
            remaining = n_samples
            write_pos = 0
            while remaining > 0:
                available = min(remaining, echo_len - pos)
                chunk[write_pos : write_pos + available] = echo_buf[
                    pos : pos + available
                ]
                pos = (pos + available) % echo_len
                write_pos += available
                remaining -= available
            self._playback_pos[freq] = pos

            # Frequency-shift if echo is not at DDC center.
            # Track accumulated angle (radians) so the shift oscillator
            # transitions smoothly when offset_hz changes due to retuning.
            if offset_hz != 0:
                phase0 = self._shift_phase.get(freq, 0.0)
                step = 2.0 * np.pi * offset_hz / sample_rate
                angles = phase0 + step * np.arange(n_samples)
                chunk = chunk * np.exp(1j * angles)
                new_phase = phase0 + step * n_samples
                if abs(new_phase) > 1e6:
                    new_phase %= 2.0 * np.pi
                self._shift_phase[freq] = new_phase

            result += chunk

        return result * self.ATTENUATION
