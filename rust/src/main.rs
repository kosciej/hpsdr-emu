mod protocol1;
mod radio;

use std::sync::Arc;

use clap::Parser;
use tokio::sync::Mutex;

use radio::{EchoBuffer, HpsdrHw, RadioState, SignalGenerator};

#[derive(Parser)]
#[command(name = "hpsdr-emu", about = "OpenHPSDR Protocol 1 radio emulator (Rust)")]
struct Cli {
    /// Radio hardware type
    #[arg(long, value_parser = parse_radio)]
    radio: HpsdrHw,

    /// MAC address (hex, e.g. 00:1c:c0:a2:22:5e). Random if omitted.
    #[arg(long)]
    mac: Option<String>,

    /// Test tone offset from center in Hz
    #[arg(long, default_value = "1000.0")]
    freq: f64,

    /// Noise level as fraction of full-scale
    #[arg(long, default_value = "3e-6")]
    noise: f64,

    /// Enable echo mode: TX IQ is recorded and looped back on RX
    #[arg(long)]
    echo: bool,

    /// Enable debug logging
    #[arg(short, long)]
    verbose: bool,
}

fn parse_radio(s: &str) -> Result<HpsdrHw, String> {
    HpsdrHw::from_name(s).ok_or_else(|| {
        format!(
            "unknown radio '{}'. Valid: {}",
            s,
            HpsdrHw::all_names().join(", ")
        )
    })
}

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    // Init logging
    let log_level = if cli.verbose { "debug" } else { "info" };
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or(log_level))
        .format_timestamp_secs()
        .init();

    // MAC address
    let mac = if let Some(ref mac_str) = cli.mac {
        let hex: String = mac_str.chars().filter(|c| c.is_ascii_hexdigit()).collect();
        if hex.len() != 12 {
            eprintln!("MAC address must be 6 bytes (12 hex digits)");
            std::process::exit(1);
        }
        let mut bytes = [0u8; 6];
        for i in 0..6 {
            bytes[i] = u8::from_str_radix(&hex[i * 2..i * 2 + 2], 16).unwrap();
        }
        bytes
    } else {
        RadioState::random_mac()
    };

    let sample_rate: u32 = 48000;

    let mut state = RadioState::new(cli.radio, mac);
    state.sample_rate = sample_rate;

    let siggen = SignalGenerator::new(sample_rate, cli.freq, cli.noise);

    let echo_buf = if cli.echo {
        Some(EchoBuffer::new(sample_rate))
    } else {
        None
    };

    log::info!(
        "Starting HPSDR emulator: radio={}, tone={:.0} Hz, noise={:.2e}, echo={}",
        cli.radio,
        cli.freq,
        cli.noise,
        if cli.echo { "on" } else { "off" },
    );

    let state = Arc::new(Mutex::new(state));
    let siggen = Arc::new(Mutex::new(siggen));
    let echo = Arc::new(Mutex::new(echo_buf));

    tokio::select! {
        _ = protocol1::run_protocol1(state, siggen, echo) => {}
        _ = tokio::signal::ctrl_c() => {
            log::info!("Shutting down...");
        }
    }
}
