//! The recorder: the microphone and the system audio (both through `mic`) into `mic.wav` and `system.wav`,
//! 16 kHz mono 16-bit, on one shared clock so both tracks come out equally long, plus `recording.json`
//! beside them (marked moments, and `finished` once the files are complete). The track logic is the
//! Phase 0 probe's (tools/recorder-probe), which recorded a real call on Windows with MATCH and 0 s dropped.

use std::fs::File;
use std::io::{BufWriter, Seek, Write};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{channel, Receiver, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde::Serialize;

use crate::mic::{Backend, Capture, Choice, Chunk, Source, Status};

pub const RATE: u32 = 16_000;
/// A track this far behind the clock gets zeros; anything less is callback jitter.
const SLACK: u64 = RATE as u64 / 10;
const NAMES: [&str; 2] = ["mic", "system"];
pub const INFO_FILE: &str = "recording.json";

/// Where the shared clock says a track should be, in 16 kHz samples.
fn clock_samples(elapsed: Duration) -> u64 {
    (elapsed.as_micros() * RATE as u128 / 1_000_000) as u64
}

/// One output file: downmix to mono, resample to 16 kHz, keep to the clock.
struct Track<W: Write + Seek> {
    wav: hound::WavWriter<W>,
    written: u64,
    padded: u64,
    dropped: u64,
    // resampler state: sum and count of the input frames inside the current output sample
    sum: f32,
    count: u32,
    phase: u32,
}

impl<W: Write + Seek> Track<W> {
    fn new(out: W) -> hound::Result<Self> {
        let spec = hound::WavSpec { channels: 1, sample_rate: RATE, bits_per_sample: 16, sample_format: hound::SampleFormat::Int };
        Ok(Track { wav: hound::WavWriter::new(out, spec)?, written: 0, padded: 0, dropped: 0, sum: 0.0, count: 0, phase: 0 })
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
        // shortcut: box-filter resampler (weak anti-aliasing above 8 kHz); fine for speech and Whisper in the
        // Phase 0 call, move to a windowed-sinc resampler if transcripts of music-heavy calls suffer.
        let mut out = Vec::new();
        for frame in c.samples.chunks_exact(c.channels.max(1)) {
            self.sum += frame.iter().sum::<f32>() / frame.len() as f32;
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
        // Behind the clock (loopback delivers nothing while nothing plays, or the stream was rebuilt):
        // fill with zeros, ending SLACK / 2 early so jitter does not run into the cap.
        let now = clock_samples(c.at);
        if self.written + out.len() as u64 + SLACK < now {
            self.pad_to(now - out.len() as u64 - SLACK / 2)?;
        }
        // Never ahead of the clock: a chunk cannot end after the moment it arrived. With this and the
        // padding at the end, both tracks come out exactly equal in length.
        let room = now.saturating_sub(self.written) as usize;
        if out.len() > room {
            self.dropped += (out.len() - room) as u64;
            out.truncate(room);
        }
        let flushed = self.written / (5 * RATE as u64);
        self.written += out.len() as u64;
        for s in out {
            self.wav.write_sample(s)?;
        }
        // A flushed file is readable up to the flush, so a crash loses seconds, not the meeting.
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

#[derive(Clone, Debug, Default, Serialize)]
pub struct Info {
    pub started_unix: f64,
    pub marks: Vec<f64>,
    pub finished: bool,
    pub seconds: Option<f64>,
    pub mic: Option<String>,
    pub system: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
pub struct Live {
    pub folder: PathBuf,
    pub seconds: f64,
    pub marks: Vec<f64>,
    pub mic: Status,
    pub mic_level: f32,
    pub system: Status,
    pub system_level: f32,
}

#[derive(Debug, Serialize)]
pub struct Summary {
    pub folder: PathBuf,
    pub seconds: f64,
    pub samples: [u64; 2],
    pub padded: [f64; 2],
    pub dropped: [f64; 2],
}

pub struct Recorder {
    folder: PathBuf,
    start: Instant,
    info: Mutex<Info>,
    captures: [Capture; 2],
    senders: [Sender<Option<Chunk>>; 2],
    writers: [JoinHandle<hound::Result<Track<BufWriter<File>>>>; 2],
}

fn write_info(folder: &Path, info: &Info) -> std::io::Result<()> {
    let tmp = folder.join(format!("{INFO_FILE}.tmp"));
    std::fs::write(&tmp, serde_json::to_vec_pretty(info).expect("plain data"))?;
    std::fs::rename(tmp, folder.join(INFO_FILE))
}

/// Collects one track's chunks for its writer, and nothing after the writer was told to stop.
fn forward(rx: Receiver<Chunk>, out: Sender<Option<Chunk>>) {
    std::thread::spawn(move || {
        while let Ok(chunk) = rx.recv() {
            if out.send(Some(chunk)).is_err() {
                break;
            }
        }
    });
}

fn two<T>(v: Vec<T>) -> [T; 2] {
    v.try_into().unwrap_or_else(|_| unreachable!("one per track"))
}

impl Recorder {
    /// Starts at once; nothing in `folder` is overwritten.
    pub fn start(backend: Arc<dyn Backend>, folder: &Path, mic: Choice) -> std::io::Result<Recorder> {
        std::fs::create_dir_all(folder)?;
        let paths = NAMES.map(|n| folder.join(format!("{n}.wav")));
        if let Some(taken) = paths.iter().find(|p| p.exists()) {
            return Err(std::io::Error::new(std::io::ErrorKind::AlreadyExists, format!("{} already exists", taken.display())));
        }
        let start = Instant::now();
        let started_unix = SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64());
        let info = Info { started_unix, ..Default::default() };
        write_info(folder, &info)?;
        let mut senders = Vec::new();
        let mut writers = Vec::new();
        let mut captures = Vec::new();
        for (path, source) in paths.into_iter().zip([Source::Microphone(mic), Source::System]) {
            let (tx, rx) = channel();
            writers.push(std::thread::spawn(move || write_track(&path, rx)));
            let (chunks, from_capture) = channel();
            forward(from_capture, tx.clone());
            captures.push(Capture::start(backend.clone(), source, start, Some(chunks)));
            senders.push(tx);
        }
        Ok(Recorder { folder: folder.to_path_buf(), start, info: Mutex::new(info), captures: two(captures),
            senders: two(senders), writers: two(writers) })
    }

    pub fn folder(&self) -> &Path {
        &self.folder
    }

    /// Seconds into the recording, written to `recording.json` at once.
    pub fn mark(&self) -> f64 {
        let at = (self.start.elapsed().as_secs_f64() * 10.0).round() / 10.0;
        let mut info = self.info.lock().unwrap();
        info.marks.push(at);
        let _ = write_info(&self.folder, &info);
        at
    }

    pub fn choose_mic(&self, choice: Choice) {
        self.captures[0].choose(choice);
    }

    pub fn live(&self) -> Live {
        let [mic, system] = &self.captures;
        Live {
            folder: self.folder.clone(),
            seconds: self.start.elapsed().as_secs_f64(),
            marks: self.info.lock().unwrap().marks.clone(),
            mic: mic.status(),
            mic_level: mic.level(),
            system: system.status(),
            system_level: system.level(),
        }
    }

    /// Ends both tracks at the same sample, finalises the files, then marks the folder finished.
    pub fn stop(self) -> hound::Result<Summary> {
        let Recorder { folder, start, info, captures, senders, writers } = self;
        let names = captures.each_ref().map(|c| c.status().device);
        for capture in captures {
            capture.stop();
        }
        // The writers stop on `None`, not on the streams closing: closing a stream whose device went away
        // can block, and stopping must not wait for that.
        for tx in &senders {
            let _ = tx.send(None);
        }
        let mut tracks = Vec::new();
        for writer in writers {
            tracks.push(writer.join().expect("writer thread panicked")?);
        }
        // Taken after the writers drained, so no chunk in a file is stamped later than this.
        let total = clock_samples(start.elapsed());
        let mut summary = Summary { folder: folder.clone(), seconds: total as f64 / RATE as f64, samples: [0; 2], padded: [0.0; 2], dropped: [0.0; 2] };
        for (i, mut track) in tracks.into_iter().enumerate() {
            track.pad_to(total)?;
            summary.samples[i] = track.written;
            summary.padded[i] = track.padded as f64 / RATE as f64;
            summary.dropped[i] = track.dropped as f64 / RATE as f64;
            track.wav.finalize()?;
        }
        let mut info = info.into_inner().unwrap();
        (info.finished, info.seconds) = (true, Some(summary.seconds));
        [info.mic, info.system] = names;
        write_info(&folder, &info)?;
        Ok(summary)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mic::fake::{dev, FakeBackend};
    use std::io::Cursor;

    fn chunk(at_ms: u64, rate: u32, channels: usize, frames: usize, value: f32) -> Chunk {
        Chunk { at: Duration::from_millis(at_ms), rate, channels, samples: vec![value; frames * channels] }
    }

    #[test]
    fn resamples_to_16k_mono() {
        for (rate, channels) in [(48_000, 2), (44_100, 1), (8_000, 1)] {
            let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
            t.push(&chunk(1000, rate, channels, rate as usize, 0.5)).unwrap();
            assert_eq!(t.written, 16_000, "{rate} Hz");
        }
    }

    #[test]
    fn pads_a_silent_gap_by_the_clock_and_keeps_tracks_equal() {
        let mut t = Track::new(Cursor::new(Vec::new())).unwrap();
        // steady stream: 10 chunks of 100 ms, each arriving 20 ms late -> jitter, no padding
        for i in 1..=10 {
            t.push(&chunk(i * 100 + 20, 48_000, 2, 4_800, 0.1)).unwrap();
        }
        assert_eq!((t.written, t.padded, t.dropped), (16_000, 0, 0));
        // nothing for 5 s (loopback while nothing plays, or a rebuilt stream), then one more 100 ms chunk
        t.push(&chunk(6_120, 48_000, 2, 4_800, 0.1)).unwrap();
        assert_eq!((t.written, t.padded, t.dropped), (6_070 * 16, 4_970 * 16, 0));
        // a burst: 100 ms of audio only 10 ms later must not push the track past the clock
        t.push(&chunk(6_130, 48_000, 2, 4_800, 0.1)).unwrap();
        assert_eq!((t.written, t.dropped), (6_130 * 16, 40 * 16));
        let mut silent = Track::new(Cursor::new(Vec::new())).unwrap();
        t.pad_to(160_000).unwrap();
        silent.pad_to(160_000).unwrap();
        assert_eq!((t.written, silent.written, silent.padded), (160_000, 160_000, 160_000));
    }

    #[test]
    fn records_two_equal_tracks_with_marks_and_a_finished_flag() {
        let dir = std::env::temp_dir().join(format!("vi-rec-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        let backend = FakeBackend::with(vec![dev("array", "Microphone Array", true, false)]);
        *backend.output.lock().unwrap() = Some(dev("speakers", "Speakers", true, false));
        let rec = Recorder::start(backend.clone(), &dir, Choice::Default).unwrap();
        let deadline = Instant::now() + Duration::from_secs(3);
        while backend.opens().len() < 2 {
            assert!(Instant::now() < deadline);
            std::thread::sleep(Duration::from_millis(5));
        }
        // only the microphone delivers; the system track stays silent (nothing playing)
        let mic = backend.opened.lock().unwrap().iter().find(|(id, _, _)| id == "array").unwrap().1.clone();
        for _ in 0..5 {
            (mic.lock().unwrap())(48_000, 1, &[0.2; 4_800]);
            std::thread::sleep(Duration::from_millis(100));
        }
        assert_eq!(rec.mark(), rec.live().marks[0]);
        let info: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join(INFO_FILE)).unwrap()).unwrap();
        assert_eq!((info["marks"].as_array().unwrap().len(), &info["finished"]), (1, &serde_json::Value::Bool(false)));
        assert!(Recorder::start(backend.clone(), &dir, Choice::Default).is_err()); // never overwrites

        let summary = rec.stop().unwrap();
        assert_eq!(summary.samples[0], summary.samples[1]);
        assert!(summary.seconds > 0.4 && summary.dropped[0] < 0.1);
        let lengths: Vec<u32> = NAMES.iter().map(|n| hound::WavReader::open(dir.join(format!("{n}.wav"))).unwrap().duration()).collect();
        assert_eq!(lengths[0] as u64, summary.samples[0]);
        assert_eq!(lengths[0], lengths[1]);
        let spec = hound::WavReader::open(dir.join("mic.wav")).unwrap().spec();
        assert_eq!((spec.sample_rate, spec.channels, spec.bits_per_sample), (16_000, 1, 16));
        let info: serde_json::Value = serde_json::from_slice(&std::fs::read(dir.join(INFO_FILE)).unwrap()).unwrap();
        assert_eq!((&info["finished"], info["mic"].as_str(), info["system"].as_str()),
            (&serde_json::Value::Bool(true), Some("Microphone Array"), Some("Speakers")));
        std::fs::remove_dir_all(dir).unwrap();
    }

    /// The real devices: `VI_RECORD_DIR=<folder> VI_RECORD_SECONDS=20 cargo test real_devices -- --ignored --nocapture`.
    /// Prints the devices and the result line the Phase 0 probe printed.
    #[test]
    #[ignore]
    fn real_devices() {
        let dir = PathBuf::from(std::env::var("VI_RECORD_DIR").expect("VI_RECORD_DIR"));
        let seconds: u64 = std::env::var("VI_RECORD_SECONDS").map_or(10, |s| s.parse().unwrap());
        let backend: Arc<dyn Backend> = Arc::new(crate::mic::Cpal::default());
        println!("microphones: {:?}\nsystem: {:?}", backend.microphones(), backend.system());
        let rec = Recorder::start(backend, &dir, Choice::Default).unwrap();
        let mut last = (None, None);
        for _ in 0..seconds * 10 {
            std::thread::sleep(Duration::from_millis(100));
            let live = rec.live();
            let now = (live.mic.device.clone().map(|d| format!("{d} rebuilds={}", live.mic.rebuilds)), live.system.device.clone().map(|d| format!("{d} rebuilds={}", live.system.rebuilds)));
            if now != last {
                println!("[{:6.1}s] mic: {:?} {:?}  system: {:?} {:?}", live.seconds, now.0, live.mic.problem, now.1, live.system.problem);
                last = now;
            }
        }
        let s = rec.stop().unwrap();
        println!("{s:?}");
        println!("{}", if s.samples[0] == s.samples[1] { "MATCH" } else { "MISMATCH" });
    }
}
