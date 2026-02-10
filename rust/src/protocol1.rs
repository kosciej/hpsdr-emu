use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use num_complex::Complex;
use tokio::net::UdpSocket;
use tokio::sync::Mutex;

use crate::radio::{
    code_to_sample_rate, pack_iq_24bit_into, unpack_tx_iq_16bit, EchoBuffer, RadioState,
    SignalGenerator,
};

const PORT: u16 = 1024;
const PACKET_SIZE: usize = 1032;
const SUBFRAME_SIZE: usize = 512;
const SYNC: [u8; 3] = [0x7F, 0x7F, 0x7F];

/// Response C0 addresses the radio rotates through (matched to Thetis parsing).
const RESPONSE_ADDRS: [u8; 4] = [0x00, 0x08, 0x10, 0x18];

pub struct Protocol1Server {
    state: Arc<Mutex<RadioState>>,
    siggen: Arc<Mutex<SignalGenerator>>,
    echo: Arc<Mutex<Option<EchoBuffer>>>,
    control_idx: u8,
}

impl Protocol1Server {
    pub fn new(
        state: Arc<Mutex<RadioState>>,
        siggen: Arc<Mutex<SignalGenerator>>,
        echo: Arc<Mutex<Option<EchoBuffer>>>,
    ) -> Self {
        Self {
            state,
            siggen,
            echo,
            control_idx: 0,
        }
    }

    // -- Discovery ----------------------------------------------------------

    async fn build_discovery_response(&self) -> Vec<u8> {
        let s = self.state.lock().await;
        let mut buf = vec![0u8; 60];
        buf[0] = 0xEF;
        buf[1] = 0xFE;
        buf[2] = 0x02;
        buf[3..9].copy_from_slice(&s.mac);
        buf[9] = s.firmware_version;
        buf[10] = s.hw.code();
        buf[11] = 0; // protocol version 0 for P1
        buf[14] = s.mercury_versions[0];
        buf[15] = s.mercury_versions[1];
        buf[16] = s.mercury_versions[2];
        buf[17] = s.mercury_versions[3];
        buf[18] = s.penny_version;
        buf[19] = s.metis_version;
        buf[20] = s.nddc;
        buf
    }

    // -- Control processing -------------------------------------------------

    async fn process_control(&mut self, c0: u8, c1: u8, c2: u8, c3: u8, c4: u8) {
        let mut s = self.state.lock().await;

        let mox = (c0 & 0x01) != 0;
        let addr = c0 & 0xFE;

        if mox != s.ptt {
            log::info!("P1 MOX -> {}", mox);
            s.ptt = mox;
            let tx_freq = s.tx_frequency;
            drop(s); // release state lock before echo lock
            let mut echo_guard = self.echo.lock().await;
            if let Some(echo) = echo_guard.as_mut() {
                if mox {
                    echo.start_recording(tx_freq);
                } else {
                    echo.stop_recording();
                }
            }
            s = self.state.lock().await;
        }

        match addr {
            0x00 => {
                // Sample rate
                let rate_code = c1 & 0x03;
                if let Some(rate) = code_to_sample_rate(rate_code) {
                    if s.sample_rate != rate {
                        log::info!("P1 Sample rate -> {} Hz", rate);
                        s.sample_rate = rate;
                        drop(s);
                        self.siggen.lock().await.sample_rate = rate;
                        s = self.state.lock().await;
                    }
                }
                // Number of receivers: C4 bits [5:3] = (nddc - 1)
                let nddc = ((c4 >> 3) & 0x07) + 1;
                if nddc != s.nddc {
                    log::info!("P1 Active DDCs -> {}", nddc);
                    s.nddc = nddc;
                }
            }
            0x02 => {
                // TX frequency
                let freq = u32::from_be_bytes([c1, c2, c3, c4]);
                if s.tx_frequency != freq {
                    log::info!("P1 TX freq -> {} Hz", freq);
                    s.tx_frequency = freq;
                }
            }
            a if (0x04..0x12).contains(&a) && (a % 2 == 0) => {
                // RX frequencies: 0x04=RX0, 0x06=RX1, ...
                let ddc_idx = ((a - 0x04) / 2) as usize;
                let freq = u32::from_be_bytes([c1, c2, c3, c4]);
                if ddc_idx < s.rx_frequencies.len() && s.rx_frequencies[ddc_idx] != freq {
                    log::info!("P1 RX{} freq -> {} Hz", ddc_idx, freq);
                    s.rx_frequencies[ddc_idx] = freq;
                }
            }
            0x12 => {
                // TX drive level
                if s.tx_drive != c1 {
                    log::info!("P1 TX drive -> {}", c1);
                    s.tx_drive = c1;
                }
            }
            _ => {}
        }
    }

    // -- Host data handling -------------------------------------------------

    async fn handle_host_data(&mut self, data: &[u8]) {
        if data.len() < PACKET_SIZE {
            return;
        }

        for &offset in &[8usize, 520usize] {
            let sf = &data[offset..offset + SUBFRAME_SIZE];
            if sf[0..3] != SYNC {
                continue;
            }
            let (c0, c1, c2, c3, c4) = (sf[3], sf[4], sf[5], sf[6], sf[7]);
            self.process_control(c0, c1, c2, c3, c4).await;

            // Extract TX IQ for echo
            let ptt = self.state.lock().await.ptt;
            if ptt {
                let mut echo_guard = self.echo.lock().await;
                if let Some(echo) = echo_guard.as_mut() {
                    let tx_data = &sf[8..8 + 63 * 8];
                    if tx_data.len() == 63 * 8 {
                        let tx_iq = unpack_tx_iq_16bit(tx_data);
                        echo.feed(&tx_iq);
                    }
                }
            }
        }
    }

    // -- Sub-frame building -------------------------------------------------

    async fn fill_subframe(&mut self, buf: &mut [u8], offset: usize) {
        let s = self.state.lock().await;
        let nddc = s.nddc.max(1) as usize;
        let spr = 504 / (6 * nddc + 2);

        // Sync
        buf[offset] = 0x7F;
        buf[offset + 1] = 0x7F;
        buf[offset + 2] = 0x7F;

        // Control response (C0-C4)
        // Rotate through the 4 response addresses Thetis expects
        let c0_addr = RESPONSE_ADDRS[self.control_idx as usize % RESPONSE_ADDRS.len()];
        self.control_idx = (self.control_idx + 1) % RESPONSE_ADDRS.len() as u8;

        let ptt_bit = if s.ptt { 1u8 } else { 0u8 };
        buf[offset + 3] = c0_addr | 0x80 | ptt_bit;

        match c0_addr {
            0x00 => {
                // C1: ADC overflow (none), C2: Mercury FW, C3: Penny ver, C4: reserved
                buf[offset + 4] = 0x00;
                buf[offset + 5] = s.firmware_version;
                buf[offset + 6] = s.penny_version;
                buf[offset + 7] = 0x00;
            }
            0x08 => {
                // C1-C2: Exciter power (AIN5), C3-C4: Forward power (AIN1)
                let (exc, fwd) = if s.ptt {
                    let d = s.tx_drive as u16;
                    (d * 10, (d * d) >> 4)
                } else {
                    (0, 0)
                };
                buf[offset + 4..offset + 6].copy_from_slice(&exc.to_be_bytes());
                buf[offset + 6..offset + 8].copy_from_slice(&fwd.to_be_bytes());
            }
            0x10 => {
                // C1-C2: Reverse power (AIN2), C3-C4: PA volts (AIN3)
                let rev = if s.ptt {
                    let d = s.tx_drive as u16;
                    let fwd = (d * d) >> 4;
                    (fwd / 50).max(1)
                } else {
                    0
                };
                let supply: u16 = 3200;
                buf[offset + 4..offset + 6].copy_from_slice(&rev.to_be_bytes());
                buf[offset + 6..offset + 8].copy_from_slice(&supply.to_be_bytes());
            }
            0x18 => {
                // C1-C2: PA current (AIN4), C3-C4: Supply volts (AIN6)
                let pa_amps: u16 = if s.ptt { s.tx_drive as u16 * 5 } else { 0 };
                let supply: u16 = 3200;
                buf[offset + 4..offset + 6].copy_from_slice(&pa_amps.to_be_bytes());
                buf[offset + 6..offset + 8].copy_from_slice(&supply.to_be_bytes());
            }
            _ => {
                buf[offset + 4..offset + 8].fill(0);
            }
        }

        // Generate IQ samples for each DDC
        let rx_freqs: Vec<u32> = s.rx_frequencies[..nddc].to_vec();
        let sample_rate = s.sample_rate;
        drop(s); // release state lock before siggen/echo

        let mut ddc_samples: Vec<Vec<Complex<f64>>> = Vec::with_capacity(nddc);
        {
            let mut echo_guard = self.echo.lock().await;
            let has_echo = echo_guard.is_some();
            if has_echo {
                let echo = echo_guard.as_mut().unwrap();
                for ddc in 0..nddc {
                    let iq = echo.generate_echo(spr, rx_freqs[ddc], sample_rate);
                    ddc_samples.push(iq);
                }
            } else {
                drop(echo_guard);
                let mut sg = self.siggen.lock().await;
                for ddc in 0..nddc {
                    let iq = sg.generate_iq(spr, ddc);
                    ddc_samples.push(iq);
                }
            }
        }

        // Pack interleaved: [I(3B) Q(3B)] x nddc + [Mic(2B)] per sample row
        let mut data_offset = offset + 8;
        for row in 0..spr {
            for ddc in 0..nddc {
                data_offset = pack_iq_24bit_into(buf, data_offset, ddc_samples[ddc][row]);
            }
            // Mic: 2 bytes silence
            buf[data_offset] = 0;
            buf[data_offset + 1] = 0;
            data_offset += 2;
        }
    }

    // -- Packet building ----------------------------------------------------

    async fn build_data_packet(&mut self) -> Vec<u8> {
        let mut buf = vec![0u8; PACKET_SIZE];

        let seq = self.state.lock().await.next_seq("p1_data");

        // Header
        buf[0] = 0xEF;
        buf[1] = 0xFE;
        buf[2] = 0x01; // data packet
        buf[3] = 0x06; // endpoint 6
        buf[4..8].copy_from_slice(&seq.to_be_bytes());

        // Two sub-frames
        self.fill_subframe(&mut buf, 8).await;
        self.fill_subframe(&mut buf, 520).await;

        buf
    }
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

pub async fn run_protocol1(
    state: Arc<Mutex<RadioState>>,
    siggen: Arc<Mutex<SignalGenerator>>,
    echo: Arc<Mutex<Option<EchoBuffer>>>,
) {
    let socket = Arc::new(
        UdpSocket::bind(format!("0.0.0.0:{}", PORT))
            .await
            .expect("Failed to bind UDP socket on port 1024"),
    );

    {
        let s = state.lock().await;
        log::info!("Protocol 1 listening on UDP port {}", PORT);
        log::info!(
            "Radio: {} (code={}, DDCs={})",
            s.hw,
            s.hw.code(),
            s.nddc
        );
        log::info!("MAC: {}", s.mac_string());
    }

    let mut server = Protocol1Server::new(
        Arc::clone(&state),
        Arc::clone(&siggen),
        Arc::clone(&echo),
    );

    let mut recv_buf = vec![0u8; 2048];
    let mut client_addr: Option<SocketAddr> = None;
    let mut streaming = false;

    loop {
        // If streaming, we need to send packets on a timer AND receive
        if streaming {
            let (nddc, sample_rate) = {
                let s = state.lock().await;
                (s.nddc.max(1) as usize, s.sample_rate)
            };
            let spr = 504 / (6 * nddc + 2);
            let samples_per_packet = spr * 2;
            let interval = Duration::from_secs_f64(samples_per_packet as f64 / sample_rate as f64);

            let mut timer = tokio::time::interval(interval);
            timer.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);

            loop {
                tokio::select! {
                    _ = timer.tick() => {
                        let is_running = state.lock().await.running;
                        if !is_running {
                            streaming = false;
                            log::info!("P1 Streaming stopped");
                            break;
                        }
                        if let Some(addr) = client_addr {
                            let packet = server.build_data_packet().await;
                            if let Err(e) = socket.send_to(&packet, addr).await {
                                log::error!("P1 Send error: {}", e);
                            }
                        }
                    }
                    result = socket.recv_from(&mut recv_buf) => {
                        match result {
                            Ok((len, addr)) => {
                                let data = &recv_buf[..len];
                                if len < 4 || data[0] != 0xEF || data[1] != 0xFE {
                                    continue;
                                }
                                match data[2] {
                                    0x02 => {
                                        log::info!("P1 Discovery request from {}", addr);
                                        let resp = server.build_discovery_response().await;
                                        let _ = socket.send_to(&resp, addr).await;
                                        log::info!("P1 Discovery response sent ({} bytes)", resp.len());
                                    }
                                    0x04 if len > 3 => {
                                        if data[3] == 0x01 {
                                            log::info!("P1 Start streaming to {}", addr);
                                            client_addr = Some(addr);
                                            state.lock().await.running = true;
                                            // Already streaming, just update client
                                        } else if data[3] == 0x00 {
                                            log::info!("P1 Stop streaming");
                                            state.lock().await.running = false;
                                            streaming = false;
                                            break;
                                        }
                                    }
                                    0x01 => {
                                        client_addr = Some(addr);
                                        server.handle_host_data(data).await;
                                    }
                                    _ => {}
                                }
                            }
                            Err(e) => {
                                log::error!("P1 Recv error: {}", e);
                            }
                        }
                    }
                }
            }
        } else {
            // Not streaming — just wait for packets
            match socket.recv_from(&mut recv_buf).await {
                Ok((len, addr)) => {
                    let data = &recv_buf[..len];
                    if len < 4 || data[0] != 0xEF || data[1] != 0xFE {
                        continue;
                    }
                    match data[2] {
                        0x02 => {
                            log::info!("P1 Discovery request from {}", addr);
                            let resp = server.build_discovery_response().await;
                            let _ = socket.send_to(&resp, addr).await;
                            log::info!("P1 Discovery response sent ({} bytes)", resp.len());
                        }
                        0x04 if len > 3 && data[3] == 0x01 => {
                            log::info!("P1 Start streaming to {}", addr);
                            client_addr = Some(addr);
                            state.lock().await.running = true;
                            streaming = true;
                        }
                        0x04 if len > 3 && data[3] == 0x00 => {
                            log::info!("P1 Stop (already stopped)");
                            state.lock().await.running = false;
                        }
                        0x01 => {
                            client_addr = Some(addr);
                            server.handle_host_data(data).await;
                        }
                        _ => {}
                    }
                }
                Err(e) => {
                    log::error!("P1 Recv error: {}", e);
                }
            }
        }
    }
}
