use std::collections::HashMap;
use std::f64::consts::PI;
use std::fmt;

use num_complex::Complex;
use rand::Rng;
use rand_distr::{Distribution, Normal};

// ---------------------------------------------------------------------------
// HPSDRHW enum
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HpsdrHw {
    Atlas,
    Hermes,
    HermesII,
    Angelia,
    Orion,
    OrionMkII,
    HermesLite,
    Saturn,
    SaturnMkII,
}

impl HpsdrHw {
    pub fn code(self) -> u8 {
        match self {
            Self::Atlas => 0,
            Self::Hermes => 1,
            Self::HermesII => 2,
            Self::Angelia => 3,
            Self::Orion => 4,
            Self::OrionMkII => 5,
            Self::HermesLite => 6,
            Self::Saturn => 10,
            Self::SaturnMkII => 11,
        }
    }

    pub fn max_ddcs(self) -> u8 {
        match self {
            Self::Atlas => 2,
            Self::Hermes => 4,
            Self::HermesII => 4,
            Self::Angelia => 5,
            Self::Orion => 5,
            Self::OrionMkII => 8,
            Self::HermesLite => 2,
            Self::Saturn => 10,
            Self::SaturnMkII => 10,
        }
    }

    pub fn from_name(name: &str) -> Option<Self> {
        match name.to_lowercase().as_str() {
            "atlas" => Some(Self::Atlas),
            "hermes" => Some(Self::Hermes),
            "hermesii" => Some(Self::HermesII),
            "angelia" => Some(Self::Angelia),
            "orion" => Some(Self::Orion),
            "orionmkii" => Some(Self::OrionMkII),
            "hermeslite" => Some(Self::HermesLite),
            "saturn" => Some(Self::Saturn),
            "saturnmkii" => Some(Self::SaturnMkII),
            _ => None,
        }
    }

    pub fn all_names() -> &'static [&'static str] {
        &[
            "atlas",
            "hermes",
            "hermesii",
            "angelia",
            "orion",
            "orionmkii",
            "hermeslite",
            "saturn",
            "saturnmkii",
        ]
    }
}

impl fmt::Display for HpsdrHw {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let name = match self {
            Self::Atlas => "ATLAS",
            Self::Hermes => "HERMES",
            Self::HermesII => "HERMESII",
            Self::Angelia => "ANGELIA",
            Self::Orion => "ORION",
            Self::OrionMkII => "ORIONMKII",
            Self::HermesLite => "HERMESLITE",
            Self::Saturn => "SATURN",
            Self::SaturnMkII => "SATURNMKII",
        };
        write!(f, "{}", name)
    }
}

// ---------------------------------------------------------------------------
// Sample rate mapping for Protocol 1
// ---------------------------------------------------------------------------

pub const SAMPLE_RATES_P1: &[(u32, u8)] = &[
    (48000, 0),
    (96000, 1),
    (192000, 2),
    (384000, 3),
];

pub fn sample_rate_to_code(rate: u32) -> u8 {
    for &(r, c) in SAMPLE_RATES_P1 {
        if r == rate {
            return c;
        }
    }
    0
}

pub fn code_to_sample_rate(code: u8) -> Option<u32> {
    for &(r, c) in SAMPLE_RATES_P1 {
        if c == code {
            return Some(r);
        }
    }
    None
}

// ---------------------------------------------------------------------------
// RadioState
// ---------------------------------------------------------------------------

pub struct RadioState {
    pub hw: HpsdrHw,
    pub mac: [u8; 6],
    pub firmware_version: u8,
    pub mercury_versions: [u8; 4],
    pub penny_version: u8,
    pub metis_version: u8,
    pub sample_rate: u32,
    pub nddc: u8,
    pub rx_frequencies: [u32; 12],
    pub tx_frequency: u32,
    pub tx_drive: u8,
    pub running: bool,
    pub ptt: bool,
    seq: HashMap<String, u32>,
}

impl RadioState {
    pub fn new(hw: HpsdrHw, mac: [u8; 6]) -> Self {
        Self {
            hw,
            mac,
            firmware_version: 25,
            mercury_versions: [25, 25, 25, 25],
            penny_version: 25,
            metis_version: 25,
            sample_rate: 48000,
            nddc: hw.max_ddcs(),
            rx_frequencies: [7_074_000; 12],
            tx_frequency: 7_074_000,
            tx_drive: 0,
            running: false,
            ptt: false,
            seq: HashMap::new(),
        }
    }

    pub fn next_seq(&mut self, stream: &str) -> u32 {
        let val = self.seq.entry(stream.to_string()).or_insert(0);
        let ret = *val;
        *val = val.wrapping_add(1);
        ret
    }

    pub fn random_mac() -> [u8; 6] {
        let mut rng = rand::thread_rng();
        let mut mac = [0u8; 6];
        rng.fill(&mut mac);
        mac[0] = (mac[0] | 0x02) & 0xFE; // locally-administered, unicast
        mac
    }

    pub fn mac_string(&self) -> String {
        self.mac
            .iter()
            .map(|b| format!("{:02x}", b))
            .collect::<Vec<_>>()
            .join(":")
    }
}

// ---------------------------------------------------------------------------
// SignalGenerator
// ---------------------------------------------------------------------------

pub struct SignalGenerator {
    pub sample_rate: u32,
    pub tone_offset_hz: f64,
    pub noise_level: f64,
    pub amplitude: f64,
    phase: HashMap<usize, f64>,
}

impl SignalGenerator {
    pub fn new(sample_rate: u32, tone_offset_hz: f64, noise_level: f64) -> Self {
        Self {
            sample_rate,
            tone_offset_hz,
            noise_level,
            amplitude: 0.3,
            phase: HashMap::new(),
        }
    }

    pub fn generate_iq(&mut self, n_samples: usize, ddc_index: usize) -> Vec<Complex<f64>> {
        let normal = Normal::new(0.0, 1.0).unwrap();
        let mut rng = rand::thread_rng();

        let phase = *self.phase.entry(ddc_index).or_insert(0.0);
        let sr = self.sample_rate as f64;

        let mut samples = Vec::with_capacity(n_samples);
        for i in 0..n_samples {
            let t = (i as f64 / sr) + phase;
            let angle = 2.0 * PI * self.tone_offset_hz * t;
            let tone = Complex::new(angle.cos(), angle.sin()) * self.amplitude;
            let noise = Complex::new(
                normal.sample(&mut rng) * self.noise_level,
                normal.sample(&mut rng) * self.noise_level,
            );
            samples.push(tone + noise);
        }

        let new_phase = phase + n_samples as f64 / sr;
        let stored = self.phase.get_mut(&ddc_index).unwrap();
        *stored = if new_phase > 1e6 {
            if self.tone_offset_hz != 0.0 {
                new_phase % (1.0 / self.tone_offset_hz)
            } else {
                0.0
            }
        } else {
            new_phase
        };

        samples
    }
}

// ---------------------------------------------------------------------------
// EchoBuffer
// ---------------------------------------------------------------------------

const ECHO_ATTENUATION_DB: f64 = 60.0;

pub struct EchoBuffer {
    pub sample_rate: u32,
    pub max_duration: f64,
    attenuation: f64,
    echoes: HashMap<u32, Vec<Complex<f64>>>,
    playback_pos: HashMap<u32, usize>,
    shift_phase: HashMap<u32, f64>, // per-freq angle accumulator (radians)
    recording: Vec<Complex<f64>>,
    recording_freq: u32,
    is_recording: bool,
}

impl EchoBuffer {
    pub fn new(sample_rate: u32) -> Self {
        let attenuation = 10.0_f64.powf(-ECHO_ATTENUATION_DB / 20.0);
        Self {
            sample_rate,
            max_duration: 10.0,
            attenuation,
            echoes: HashMap::new(),
            playback_pos: HashMap::new(),
            shift_phase: HashMap::new(),
            recording: Vec::new(),
            recording_freq: 0,
            is_recording: false,
        }
    }

    pub fn start_recording(&mut self, tx_freq: u32) {
        if self.is_recording {
            self.commit();
        }
        self.recording.clear();
        self.recording_freq = tx_freq;
        self.is_recording = true;
        log::info!("Echo: recording started on {} Hz", tx_freq);
    }

    pub fn feed(&mut self, samples: &[Complex<f64>]) {
        if !self.is_recording || samples.is_empty() {
            return;
        }
        self.recording.extend_from_slice(samples);
    }

    pub fn stop_recording(&mut self) {
        if self.is_recording {
            self.commit();
            self.is_recording = false;
        }
    }

    fn commit(&mut self) {
        if self.recording.is_empty() {
            return;
        }
        let freq = self.recording_freq;
        if freq == 0 {
            log::debug!("Echo: discarding recording with freq=0");
            self.recording.clear();
            return;
        }
        let max_samples = (self.sample_rate as f64 * self.max_duration) as usize;
        let mut buf = std::mem::take(&mut self.recording);
        buf.truncate(max_samples);
        if buf.is_empty() {
            return;
        }
        let len = buf.len();
        log::info!(
            "Echo: committed {} samples ({:.2}s) on {} Hz",
            len,
            len as f64 / self.sample_rate as f64,
            freq,
        );
        self.echoes.insert(freq, buf);
        self.playback_pos.insert(freq, 0);
    }

    pub fn generate_echo(
        &mut self,
        n_samples: usize,
        rx_freq: u32,
        sample_rate: u32,
    ) -> Vec<Complex<f64>> {
        if self.echoes.is_empty() {
            return vec![Complex::new(0.0, 0.0); n_samples];
        }

        let mut result = vec![Complex::new(0.0, 0.0); n_samples];
        let half_bw = sample_rate as f64 / 2.0;

        let freqs: Vec<u32> = self.echoes.keys().copied().collect();
        for freq in freqs {
            let offset_hz = rx_freq as f64 - freq as f64;
            if offset_hz.abs() > half_bw {
                continue;
            }

            let echo_buf = self.echoes.get(&freq).unwrap();
            let echo_len = echo_buf.len();
            let mut pos = *self.playback_pos.get(&freq).unwrap_or(&0);

            let mut chunk = vec![Complex::new(0.0, 0.0); n_samples];
            let mut remaining = n_samples;
            let mut write_pos = 0;
            while remaining > 0 {
                let available = remaining.min(echo_len - pos);
                chunk[write_pos..write_pos + available]
                    .copy_from_slice(&echo_buf[pos..pos + available]);
                pos = (pos + available) % echo_len;
                write_pos += available;
                remaining -= available;
            }
            self.playback_pos.insert(freq, pos);

            // Frequency-shift: track accumulated angle (radians) so the
            // shift oscillator transitions smoothly when offset changes.
            if offset_hz != 0.0 {
                let sr = sample_rate as f64;
                let phase0 = *self.shift_phase.get(&freq).unwrap_or(&0.0);
                let step = 2.0 * PI * offset_hz / sr;
                for (i, s) in chunk.iter_mut().enumerate() {
                    let angle = phase0 + step * i as f64;
                    *s *= Complex::new(angle.cos(), angle.sin());
                }
                let mut new_phase = phase0 + step * n_samples as f64;
                if new_phase.abs() > 1e6 {
                    new_phase %= 2.0 * PI;
                }
                self.shift_phase.insert(freq, new_phase);
            }

            for (i, s) in chunk.iter().enumerate() {
                result[i] += s;
            }
        }

        for s in &mut result {
            *s *= self.attenuation;
        }

        result
    }
}

// ---------------------------------------------------------------------------
// IQ packing / unpacking
// ---------------------------------------------------------------------------

/// Pack a single IQ sample into 6 bytes (3B I + 3B Q), 24-bit signed big-endian.
/// Writes directly into `buf` at `offset`. Returns new offset.
#[inline]
pub fn pack_iq_24bit_into(buf: &mut [u8], offset: usize, sample: Complex<f64>) -> usize {
    let max_val: f64 = 8_388_607.0;
    let iv = (sample.re.clamp(-1.0, 1.0) * max_val) as i32;
    let qv = (sample.im.clamp(-1.0, 1.0) * max_val) as i32;
    let iu = iv as u32 & 0xFF_FFFF;
    let qu = qv as u32 & 0xFF_FFFF;
    buf[offset] = ((iu >> 16) & 0xFF) as u8;
    buf[offset + 1] = ((iu >> 8) & 0xFF) as u8;
    buf[offset + 2] = (iu & 0xFF) as u8;
    buf[offset + 3] = ((qu >> 16) & 0xFF) as u8;
    buf[offset + 4] = ((qu >> 8) & 0xFF) as u8;
    buf[offset + 5] = (qu & 0xFF) as u8;
    offset + 6
}

/// Unpack Protocol 1 TX IQ from sub-frame data.
/// Each 8-byte block: [L(2B) R(2B) I(2B) Q(2B)], big-endian signed.
pub fn unpack_tx_iq_16bit(data: &[u8]) -> Vec<Complex<f64>> {
    let n_blocks = data.len() / 8;
    let mut samples = Vec::with_capacity(n_blocks);
    for k in 0..n_blocks {
        let off = k * 8;
        let i_val = i16::from_be_bytes([data[off + 4], data[off + 5]]);
        let q_val = i16::from_be_bytes([data[off + 6], data[off + 7]]);
        samples.push(Complex::new(
            i_val as f64 / 32768.0,
            q_val as f64 / 32768.0,
        ));
    }
    samples
}
