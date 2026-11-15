//! Phase 0 recorder probe: records the default microphone and the system audio (WASAPI
//! loopback on Windows) at the same time into `mic.wav` and `system.wav`, both 16 kHz mono
//! 16-bit, padded with silence by one shared clock so the two tracks stay equal in length.
//!
//! Usage: recorder-probe <folder> [seconds]     (no seconds = until Ctrl+C)

use std::fs::File;
use std::io::{BufWriter, Seek, Write};
use std::path::Path;
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use cpal::{ErrorKind, FromSample, Sample, SampleFormat, SizedSample};

const RATE: u32 = 16_000;
/// A track this far behind the clock gets zeros; anything less is callback jitter.
const SLACK: u64 = RATE as u64 / 10;
/// A stream that keeps failing is given up after this many restarts; a device change needs one or two.
const MAX_RESTARTS: u32 = 10;
const NAMES: [&str; 2] = ["mic", "system"];

/// Where the shared clock says a track should be, in 16 kHz samples.
fn clock_samples(elapsed: Duration) -> u64 {
    (elapsed.as_micros() * RATE as u128 / 1_000_000) as u64
}

/// Samples from one audio callback, stamped with the shared clock when they arrived.
struct Chunk {
    at: Duration,
    rate: u32,
    channels: usize,
    samples: Vec<f32>,
}

enum Event {
    Stop,
    /// Track, the restart count of the stream reporting it, the error.
    StreamError(usize, u32, cpal::Error),
}

/// One output file: downmix to mono, resample to 16 kHz, keep to the clock.
struct Track<W: Write + Seek> {
    wav: hound::WavWriter<W>,
    written: u64,
    padded: u64,
    dropped: u64,
    peak: i16,
    // resampler state: sum and count of the input frames inside the current output sample
    sum: f32,
    count: u32,
    phase: u32,
}

impl<W: Write + Seek> Track<W> {
    fn new(out: W) -> hound::Result<Self> {
        let spec = hound::WavSpec {
            channels: 1,
            sample_rate: RATE,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let wav = hound::WavWriter::new(out, spec)?;
        Ok(Track {
            wav,
            written: 0,
            padded: 0,
            dropped: 0,
            peak: 0,
            sum: 0.0,
            count: 0,
            phase: 0,
        })
    }

    fn pad_to(&mut self, total: u64) -> hound::Result<()> {
        while self.written < total {
            self.wav.write_sample(0i16)?;
            self.written += 1;
            self.padded += 1;
        }
        Ok(())
    }

    fn push(&mut self, c: &Chunk) -> hound::Result<()> {
        // shortcut: box-filter resampler (weak anti-aliasing), use rubato in the app's recorder.
        let mut out = Vec::new();
        for frame in c.samples.chunks_exact(c.channels) {
            self.sum += frame.iter().sum::<f32>() / c.channels as f32;
            self.count += 1;
            self.phase += RATE;
            if self.phase >= c.rate {
                let mean = (self.sum / self.count as f32 * 32767.0).clamp(-32768.0, 32767.0) as i16;
                while self.phase >= c.rate {
                    self.phase -= c.rate;
                    out.push(mean);
                }
                (self.sum, self.count) = (0.0, 0);
            }
        }
        // Behind the clock (loopback delivers nothing while nothing plays, or the stream was
        // rebuilt): fill with zeros, ending SLACK / 2 early so jitter does not run into the cap.
        let now = clock_samples(c.at);
        if self.written + out.len() as u64 + SLACK < now {
            self.pad_to(now - out.len() as u64 - SLACK / 2)?;
        }
        // Never ahead of the clock: a chunk cannot end after the moment it arrived. With this
        // and the padding at the end, both tracks come out exactly equal in length.
        let room = now.saturating_sub(self.written) as usize;
        if out.len() > room {
            self.dropped += (out.len() - room) as u64;
            out.truncate(room);
        }
        let flushed = self.written / (5 * RATE as u64);
        self.written += out.len() as u64;
        for s in out {
            self.wav.write_sample(s)?;
            self.peak = self.peak.max(s.saturating_abs());
        }
        // A flushed file is readable up to the flush, so a crash loses seconds, not the test.
        if self.written / (5 * RATE as u64) != flushed {
            self.wav.flush()?;
        }
        Ok(())
    }
}

fn write_track(path: &Path, rx: Receiver<Option<Chunk>>) -> hound::Result<Track<BufWriter<File>>> {
    let mut track = Track::new(BufWriter::new(File::create(path)?))?;
    while let Ok(Some(chunk)) = rx.recv() {
        track.push(&chunk)?;
    }
    Ok(track)
}

fn build<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    start: Instant,
    tx: Sender<Option<Chunk>>,
    on_error: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<cpal::Stream, cpal::Error>
where
    T: SizedSample,
    f32: FromSample<T>,
{
    let (rate, channels) = (config.sample_rate, config.channels as usize);
    device.build_input_stream(
        config,
        move |data: &[T], _: &cpal::InputCallbackInfo| {
            let samples = data.iter().map(|&s| f32::from_sample(s)).collect();
            let _ = tx.send(Some(Chunk {
                at: start.elapsed(),
                rate,
                channels,
                samples,
            }));
        },
        on_error,
        None,
    )
}

/// Opens the default microphone (0) or the default output device as an input (1 = loopback).
fn open(
    which: usize,
    start: Instant,
    tx: &Sender<Option<Chunk>>,
    on_error: impl FnMut(cpal::Error) + Send + 'static,
) -> Result<cpal::Stream, cpal::Error> {
    let host = cpal::default_host();
    let device = if which == 0 {
        host.default_input_device()
    } else {
        host.default_output_device()
    }
    .ok_or(ErrorKind::DeviceNotAvailable)?;
    let supported = if which == 0 {
        device.default_input_config()
    } else {
        device.default_output_config()
    }?;
    let (format, config) = (supported.sample_format(), supported.config());
    let tx = tx.clone();
    println!(
        "[{:6.1}s] {}: {} ({} Hz, {} ch, {format})",
        start.elapsed().as_secs_f64(),
        NAMES[which],
        device
            .description()
            .map(|d| d.to_string())
            .unwrap_or_else(|e| e.to_string()),
        config.sample_rate,
        config.channels,
    );
    let stream = match format {
        SampleFormat::F32 => build::<f32>(&device, config, start, tx, on_error),
        SampleFormat::I16 => build::<i16>(&device, config, start, tx, on_error),
        SampleFormat::I32 => build::<i32>(&device, config, start, tx, on_error),
        SampleFormat::U16 => build::<u16>(&device, config, start, tx, on_error),
        other => Err(cpal::Error::with_message(
            ErrorKind::UnsupportedConfig,
            format!("sample format {other}"),
        )),
    }?;
    stream.play()?;
    Ok(stream)
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 || args.len() > 3 {
        eprintln!("usage: recorder-probe <folder> [seconds]   (no seconds = until Ctrl+C)");
        std::process::exit(2);
    }
    let folder = Path::new(&args[1]);
    let limit = match args.get(2) {
        Some(s) => Some(Duration::from_secs(s.parse()?)),
        None => None,
    };
    std::fs::create_dir_all(folder)?;
    let paths = NAMES.map(|n| folder.join(format!("{n}.wav")));
    if let Some(taken) = paths.iter().find(|p| p.exists()) {
        eprintln!(
            "{} already exists. Pick another folder; nothing is overwritten.",
            taken.display()
        );
        std::process::exit(2);
    }

    let (events_tx, events) = channel();
    let stop = events_tx.clone();
    ctrlc::set_handler(move || {
        let _ = stop.send(Event::Stop);
    })?;

    let start = Instant::now();
    let mut senders = Vec::new();
    let mut writers = Vec::new();
    for path in paths.clone() {
        let (tx, rx) = channel();
        senders.push(tx);
        writers.push(std::thread::spawn(move || write_track(&path, rx)));
    }

    println!("Recording into {}. Ctrl+C stops.", folder.display());
    let mut streams: [Option<cpal::Stream>; 2] = [None, None];
    let mut last_error = [String::new(), String::new()];
    let mut restarts = [0u32; 2];
    loop {
        // Also the way back after a device change: the stream is rebuilt on the new default device.
        for which in 0..2 {
            if streams[which].is_none() && restarts[which] <= MAX_RESTARTS {
                let (events, restart) = (events_tx.clone(), restarts[which]);
                let on_error = move |e| {
                    let _ = events.send(Event::StreamError(which, restart, e));
                };
                match open(which, start, &senders[which], on_error) {
                    Ok(stream) => streams[which] = Some(stream),
                    Err(e) if e.to_string() != last_error[which] => {
                        println!(
                            "[{:6.1}s] {} could not be opened: {e}",
                            start.elapsed().as_secs_f64(),
                            NAMES[which]
                        );
                        last_error[which] = e.to_string();
                    }
                    Err(_) => {}
                }
            }
        }
        let left = limit.map_or(Duration::from_secs(1), |l| {
            l.saturating_sub(start.elapsed())
        });
        match events.recv_timeout(left.min(Duration::from_secs(1))) {
            Ok(Event::Stop) => break,
            // A stream that was already replaced can still have errors queued; they are not news.
            Ok(Event::StreamError(which, restart, _)) if restart != restarts[which] => {}
            Ok(Event::StreamError(which, _, e)) => {
                println!(
                    "[{:6.1}s] {} stream: {e}",
                    start.elapsed().as_secs_f64(),
                    NAMES[which]
                );
                if !matches!(
                    e.kind(),
                    ErrorKind::Xrun | ErrorKind::DeviceChanged | ErrorKind::RealtimeDenied
                ) {
                    // Closed on its own thread: closing a stream whose device went away can block.
                    if let Some(dead) = streams[which].take() {
                        std::thread::spawn(move || drop(dead));
                    }
                    last_error[which].clear();
                    restarts[which] += 1;
                    if restarts[which] > MAX_RESTARTS {
                        println!(
                            "{} is given up; the rest of its track is silence.",
                            NAMES[which]
                        );
                    }
                    std::thread::sleep(Duration::from_millis(300)); // let the new default device settle
                }
            }
            Err(RecvTimeoutError::Timeout) => {}
            Err(RecvTimeoutError::Disconnected) => break,
        }
        if limit.is_some_and(|l| start.elapsed() >= l) {
            break;
        }
    }

    // The writers stop on `None`, not on the streams closing: dropping a stream whose device
    // went away can block, and the report must not wait for that.
    for tx in &senders {
        let _ = tx.send(None);
    }
    let mut tracks = Vec::new();
    for writer in writers {
        tracks.push(writer.join().expect("writer thread panicked")?);
    }
    // Taken after the writers drained, so no chunk in a file is stamped later than this.
    let total = clock_samples(start.elapsed());
    let mut stats = Vec::new();
    for mut track in tracks {
        track.pad_to(total)?;
        stats.push((track.padded, track.dropped, track.peak));
        track.wav.finalize()?;
    }

    // Report what is on disk, not what the writers believe.
    let mut lengths = Vec::new();
    for (path, (padded, dropped, peak)) in paths.iter().zip(stats) {
        let reader = hound::WavReader::open(path)?;
        let (spec, samples) = (reader.spec(), reader.duration() as u64);
        println!(
            "{:<10} {:>9} samples  {:>8.3} s  {} Hz {} ch {}-bit  silence padded {:.3} s  dropped {:.3} s  peak {:.1}%",
            path.file_name().unwrap().to_string_lossy(),
            samples,
            samples as f64 / spec.sample_rate as f64,
            spec.sample_rate,
            spec.channels,
            spec.bits_per_sample,
            padded as f64 / RATE as f64,
            dropped as f64 / RATE as f64,
            peak as f64 / 327.67,
        );
        lengths.push(samples);
    }
    if lengths[0] == lengths[1] {
        println!("MATCH: both tracks have the same length.");
    } else {
        let diff = lengths[0] as f64 - lengths[1] as f64;
        println!(
            "MISMATCH: mic.wav is {:+.3} s against system.wav.",
            diff / RATE as f64
        );
    }
    // Exit without dropping the streams (see above).
    std::process::exit(if lengths[0] == lengths[1] { 0 } else { 1 });
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn chunk(at_ms: u64, rate: u32, channels: usize, frames: usize, value: f32) -> Chunk {
        Chunk {
            at: Duration::from_millis(at_ms),
            rate,
            channels,
            samples: vec![value; frames * channels],
        }
    }

    #[test]
    fn resamples_to_16k_mono() {
        let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
        t.push(&chunk(1000, 48_000, 2, 48_000, 0.5)).unwrap();
        assert_eq!(t.written, 16_000);
        assert_eq!(t.peak, 16_383);
        let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
        t.push(&chunk(1000, 44_100, 1, 44_100, 0.5)).unwrap();
        assert_eq!(t.written, 16_000);
        let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
        t.push(&chunk(1000, 8_000, 1, 8_000, 0.5)).unwrap();
        assert_eq!(t.written, 16_000);
    }

    #[test]
    fn pads_a_silent_gap_by_the_clock_and_keeps_tracks_equal() {
        let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
        // steady stream: 10 chunks of 100 ms, each arriving 20 ms late -> jitter, no padding
        for i in 1..=10 {
            t.push(&chunk(i * 100 + 20, 48_000, 2, 4_800, 0.1)).unwrap();
        }
        assert_eq!((t.written, t.padded, t.dropped), (16_000, 0, 0));
        // nothing for 5 s (loopback while nothing plays), then one more 100 ms chunk:
        // zeros up to 50 ms before where the clock puts that chunk
        t.push(&chunk(6_120, 48_000, 2, 4_800, 0.1)).unwrap();
        assert_eq!(
            (t.written, t.padded, t.dropped),
            (6_070 * 16, 4_970 * 16, 0)
        );
        // a burst: 100 ms of audio only 10 ms later must not push the track past the clock
        t.push(&chunk(6_130, 48_000, 2, 4_800, 0.1)).unwrap();
        assert_eq!((t.written, t.dropped), (6_130 * 16, 40 * 16));
        // the end of the recording pads both tracks to the same clock value
        let mut silent = Track::new(Cursor::new(Vec::new())).unwrap();
        t.pad_to(160_000).unwrap();
        silent.pad_to(160_000).unwrap();
        assert_eq!(t.written, silent.written);
        assert_eq!(silent.padded, 160_000);
    }
}
