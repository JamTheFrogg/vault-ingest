//! Everything about audio input devices, and the only code in the app that touches them: listing the
//! microphones, resolving the user's choice (system default, Windows communications default, or a device
//! picked by id with its name as a fallback), opening the stream, following device changes by rebuilding
//! it, and the level meter. The system audio is opened here too (WASAPI / Core Audio loopback, the
//! PulseAudio monitor of the default output), because it is an input stream with the same failure modes.
//!
//! The interface is `devices`, `resolve`, `Capture` (start / level / status / choose / stop) and the
//! `Chunk`s it sends. `Backend` is the seam for tests; `Cpal` is the real one.

use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::{Arc, Mutex};
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

use serde::{Deserialize, Serialize};

/// What the user chose in Settings. Stored by the engine (`MIC_DEVICE`, `MIC_DEVICE_NAME`).
#[derive(Clone, Debug, Default, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "lowercase")]
pub enum Choice {
    #[default]
    Default,
    /// Windows' "default communications device"; elsewhere the same as `Default`.
    Communications,
    Device { id: String, name: String },
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct Device {
    pub id: String,
    pub name: String,
    pub default: bool,
    pub communications: bool,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Source {
    Microphone(Choice),
    System,
}

/// Samples from one audio callback, stamped with the shared clock when they arrived.
pub struct Chunk {
    pub at: Duration,
    pub rate: u32,
    pub channels: usize,
    pub samples: Vec<f32>,
}

pub enum Fault {
    /// A glitch the stream survives (an xrun, a reroute it handles itself).
    Transient,
    /// The stream is dead (device unplugged, server restarted): it must be rebuilt.
    Lost(String),
}

pub type DataFn = Box<dyn FnMut(u32, usize, &[f32]) + Send>;
pub type ErrorFn = Box<dyn FnMut(Fault) + Send>;

pub trait Backend: Send + Sync + 'static {
    fn microphones(&self) -> Vec<Device>;
    /// The device whose sound is "the call": the default output, opened for capture.
    fn system(&self) -> Option<Device>;
    /// PulseAudio's switch-on-connect moves every open input stream to a newly connected microphone,
    /// the loopback included; such a backend gets its streams reopened when the device list changes.
    fn moves_streams(&self) -> bool {
        false
    }
    /// A running stream; dropping the box closes it.
    fn open(&self, device: &Device, system: bool, data: DataFn, error: ErrorFn) -> Result<Box<dyn Send>, String>;
}

#[derive(Clone, Debug, PartialEq)]
pub struct Resolved {
    pub device: Device,
    /// The chosen device is not there; this is the default instead.
    pub fell_back: bool,
}

/// The device a choice means right now. A picked device is found by id, then by name (Windows gives the
/// same headset a new id on another USB port), else the default stands in until it comes back.
pub fn resolve(choice: &Choice, devices: &[Device]) -> Option<Resolved> {
    let default = devices.iter().find(|d| d.default).or(devices.first());
    let (found, fell_back) = match choice {
        Choice::Default => (default, false),
        Choice::Communications => match devices.iter().find(|d| d.communications) {
            Some(d) => (Some(d), false),
            None => (default, false),
        },
        Choice::Device { id, name } => match devices.iter().find(|d| &d.id == id).or_else(|| devices.iter().find(|d| &d.name == name)) {
            Some(d) => (Some(d), false),
            None => (default, true),
        },
    };
    found.map(|d| Resolved { device: d.clone(), fell_back })
}

/// 0..1 for a meter: RMS in dBFS, -60 dB (silence) to 0 dB (full scale).
pub fn level_of(samples: &[f32]) -> f32 {
    if samples.is_empty() {
        return 0.0;
    }
    let rms = (samples.iter().map(|s| s * s).sum::<f32>() / samples.len() as f32).sqrt();
    if rms <= 1e-6 {
        return 0.0;
    }
    ((20.0 * rms.log10() + 60.0) / 60.0).clamp(0.0, 1.0)
}

#[derive(Clone, Debug, Default, PartialEq, Serialize)]
pub struct Status {
    /// The device being captured; `None` while there is none.
    pub device: Option<String>,
    pub fell_back: bool,
    /// Why there is no stream, in words for the user.
    pub problem: Option<String>,
    pub rebuilds: u32,
}

struct Shared {
    level: AtomicU32,
    /// Milliseconds after `start` of the last audio, plus one (0 = never).
    heard: AtomicU64,
    status: Mutex<Status>,
    start: Instant,
}

enum Event {
    Stop,
    Choose(Choice),
    Fault(u64, Fault),
}

/// One stream kept alive: rebuilt when it dies, moved when the resolved device changes.
pub struct Capture {
    shared: Arc<Shared>,
    events: Sender<Event>,
    thread: Option<JoinHandle<()>>,
}

const RECHECK: Duration = Duration::from_secs(2);
/// The meter falls to zero when no audio arrived for this long (a loopback while nothing plays).
const LEVEL_HOLD: Duration = Duration::from_millis(400);

impl Capture {
    /// `start` is the shared clock the chunks are stamped with; `sink` gets every chunk (none for a meter only).
    pub fn start(backend: Arc<dyn Backend>, source: Source, start: Instant, sink: Option<Sender<Chunk>>) -> Capture {
        Self::start_with(backend, source, start, sink, RECHECK)
    }

    fn start_with(backend: Arc<dyn Backend>, source: Source, start: Instant, sink: Option<Sender<Chunk>>, recheck: Duration) -> Capture {
        let shared = Arc::new(Shared { level: AtomicU32::new(0), heard: AtomicU64::new(0), status: Mutex::default(), start });
        let (events, rx) = channel();
        let (s, tx) = (shared.clone(), events.clone());
        let thread = std::thread::Builder::new()
            .name("audio-capture".into())
            .spawn(move || supervise(backend, source, s, sink, tx, rx, recheck))
            .expect("could not start the capture thread");
        Capture { shared, events, thread: Some(thread) }
    }

    pub fn level(&self) -> f32 {
        let heard = self.shared.heard.load(Ordering::Relaxed);
        let now = self.shared.start.elapsed().as_millis() as u64 + 1;
        if heard == 0 || now.saturating_sub(heard) > LEVEL_HOLD.as_millis() as u64 {
            return 0.0;
        }
        f32::from_bits(self.shared.level.load(Ordering::Relaxed))
    }

    pub fn status(&self) -> Status {
        self.shared.status.lock().unwrap().clone()
    }

    /// Switch to another microphone without stopping (a pick in Settings during a call).
    pub fn choose(&self, choice: Choice) {
        let _ = self.events.send(Event::Choose(choice));
    }

    /// Waits up to two seconds for the stream to close; a stream still starting (PulseAudio waits for its
    /// first audio) is left to close by itself, so stopping a recording never hangs on a device.
    pub fn stop(mut self) {
        let _ = self.events.send(Event::Stop);
        if let Some(thread) = self.thread.take() {
            let deadline = Instant::now() + Duration::from_secs(2);
            while !thread.is_finished() && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(10));
            }
        }
    }
}

impl Drop for Capture {
    fn drop(&mut self) {
        let _ = self.events.send(Event::Stop);
    }
}

fn wanted(backend: &dyn Backend, source: &Source) -> Option<Resolved> {
    match source {
        Source::Microphone(choice) => resolve(choice, &backend.microphones()),
        Source::System => backend.system().map(|device| Resolved { device, fell_back: false }),
    }
}

/// Closed on its own thread: closing a stream whose device went away can block (Phase 0 probe).
fn close(stream: Box<dyn Send>) {
    std::thread::spawn(move || drop(stream));
}

fn supervise(backend: Arc<dyn Backend>, mut source: Source, shared: Arc<Shared>, sink: Option<Sender<Chunk>>,
    events: Sender<Event>, rx: Receiver<Event>, recheck: Duration) {
    let system = source == Source::System;
    let mut current: Option<(String, Box<dyn Send>)> = None;
    let mut generation = 0u64;
    let mut retry_at = Instant::now();
    let mut last_lost: Option<Instant> = None;
    let mut known: Vec<String> = vec![];
    let set = |f: &dyn Fn(&mut Status)| f(&mut shared.status.lock().unwrap());
    loop {
        if Instant::now() >= retry_at {
            let target = wanted(backend.as_ref(), &source);
            let mut same = matches!((&current, &target), (Some((id, _)), Some(t)) if *id == t.device.id);
            if backend.moves_streams() {
                let ids: Vec<String> = backend.microphones().into_iter().map(|d| d.id).collect();
                same &= ids.iter().all(|id| known.contains(id)); // a new device: our stream may have been moved to it
                known = ids;
            }
            if !same {
                if let Some((_, old)) = current.take() {
                    close(old);
                }
                match target {
                    None => set(&|s| {
                        s.device = None;
                        s.problem = Some(if system { "No sound output found." } else { "No microphone found." }.into());
                    }),
                    Some(t) => {
                        generation += 1;
                        let (gen, faults) = (generation, events.clone());
                        let on_error: ErrorFn = Box::new(move |f| {
                            let _ = faults.send(Event::Fault(gen, f));
                        });
                        let (meter, out) = (shared.clone(), sink.clone());
                        let on_data: DataFn = Box::new(move |rate, channels, samples| {
                            let at = meter.start.elapsed();
                            meter.level.store(level_of(samples).to_bits(), Ordering::Relaxed);
                            meter.heard.store(at.as_millis() as u64 + 1, Ordering::Relaxed);
                            if let Some(out) = &out {
                                let _ = out.send(Chunk { at, rate, channels, samples: samples.to_vec() });
                            }
                        });
                        match backend.open(&t.device, system, on_data, on_error) {
                            Ok(stream) => {
                                current = Some((t.device.id.clone(), stream));
                                set(&|s| {
                                    s.device = Some(t.device.name.clone());
                                    s.fell_back = t.fell_back;
                                    s.problem = None;
                                });
                            }
                            Err(e) => set(&|s| {
                                s.device = None;
                                s.problem = Some(format!("{} could not be opened: {e}", t.device.name));
                            }),
                        }
                    }
                }
            }
            retry_at = Instant::now() + recheck;
        }
        match rx.recv_timeout(retry_at.saturating_duration_since(Instant::now())) {
            Ok(Event::Stop) | Err(RecvTimeoutError::Disconnected) => break,
            Ok(Event::Choose(choice)) => {
                if !system {
                    source = Source::Microphone(choice);
                    retry_at = Instant::now();
                }
            }
            // A stream that was already replaced can still have errors queued; they are not news.
            Ok(Event::Fault(gen, _)) if gen != generation => {}
            Ok(Event::Fault(_, Fault::Transient)) => {}
            Ok(Event::Fault(_, Fault::Lost(why))) => {
                if let Some((_, dead)) = current.take() {
                    close(dead);
                }
                set(&|s| {
                    s.device = None;
                    s.rebuilds += 1;
                    s.problem = Some(why.clone());
                });
                // let the new default device settle; a stream that keeps dying is retried less often
                let soon = !matches!(last_lost, Some(t) if t.elapsed() <= Duration::from_secs(5));
                retry_at = Instant::now() + if soon { Duration::from_millis(300).min(recheck) } else { recheck };
                last_lost = Some(Instant::now());
            }
            Err(RecvTimeoutError::Timeout) => {}
        }
    }
    if let Some((_, stream)) = current.take() {
        close(stream);
    }
}

// ------------------------------------------------------------------ the real devices (cpal)

pub struct Cpal {
    host: Mutex<cpal::Host>,
}

impl Default for Cpal {
    fn default() -> Self {
        let host = cpal::default_host();
        eprintln!("audio: {:?} host", host.id()); // the one line that tells a support log which sound system is used
        Cpal { host: Mutex::new(host) }
    }
}

mod real {
    use super::*;
    use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
    use cpal::{ErrorKind, FromSample, Sample, SampleFormat, SizedSample};

    fn raw_id(d: &cpal::Device) -> Option<String> {
        d.id().ok().map(|id| id.id().to_string())
    }

    fn name(d: &cpal::Device) -> String {
        d.description().map(|d| d.name().to_string()).unwrap_or_else(|_| "Unnamed device".into())
    }

    /// A PulseAudio "Monitor of ..." source is the output's sound, not a microphone.
    fn is_monitor(id: &str) -> bool {
        cfg!(target_os = "linux") && id.ends_with(".monitor")
    }

    impl Cpal {
        /// Runs `f` on the host; when it finds nothing, once more on a new connection. A PulseAudio client can
        /// be dropped by the server (restarted, or it closed the connection), and cpal does not reconnect.
        fn with_host<T>(&self, f: impl Fn(&cpal::Host) -> Option<T>) -> Option<T> {
            let mut host = self.host.lock().unwrap();
            if let Some(found) = f(&host) {
                return Some(found);
            }
            *host = cpal::default_host();
            f(&host)
        }
    }

    impl Backend for Cpal {
        fn microphones(&self) -> Vec<Device> {
            self.with_host(|host| {
                let default = host.default_input_device().and_then(|d| raw_id(&d));
                let communications = communications_capture_id();
                let inputs = host.input_devices().ok()?;
                Some(inputs
                    .filter_map(|d| {
                        let id = raw_id(&d)?;
                        (!is_monitor(&id)).then(|| Device {
                            name: name(&d),
                            default: Some(&id) == default.as_ref(),
                            communications: Some(&id) == communications.as_ref(),
                            id,
                        })
                    })
                    .collect())
            })
            .unwrap_or_default()
        }

        fn moves_streams(&self) -> bool {
            cfg!(target_os = "linux")
        }

        fn system(&self) -> Option<Device> {
            self.with_host(|host| {
                let output = host.default_output_device()?;
                let (id, name) = (raw_id(&output)?, name(&output));
                if cfg!(target_os = "linux") {
                    // PulseAudio and PipeWire publish every output's sound as an input named <sink>.monitor
                    let monitor = format!("{id}.monitor");
                    let found = host.input_devices().ok()?.any(|d| raw_id(&d).as_deref() == Some(monitor.as_str()));
                    return found.then_some(Device { id: monitor, name, default: true, communications: false });
                }
                Some(Device { id, name, default: true, communications: false })
            })
        }

        fn open(&self, device: &Device, system: bool, data: DataFn, mut error: ErrorFn) -> Result<Box<dyn Send>, String> {
            // Windows and macOS: an input stream on an output device is a loopback capture
            let loopback = system && !cfg!(target_os = "linux");
            // the host is not held while the stream starts: the other track must not wait
            let dev = self.with_host(|host| {
                let mut found = if loopback { host.output_devices() } else { host.input_devices() }.ok()?;
                found.find(|d| raw_id(d).as_deref() == Some(device.id.as_str()))
            }).ok_or("the device is gone")?;
            let supported = if loopback { dev.default_output_config() } else { dev.default_input_config() }.map_err(|e| e.to_string())?;
            let (format, mut config) = (supported.sample_format(), supported.config());
            let (rate, channels) = (config.sample_rate, config.channels as usize);
            if cfg!(target_os = "linux") {
                // PulseAudio's default record fragment is about 2 s; the clock rule needs audio in small steps
                config.buffer_size = cpal::BufferSize::Fixed(rate / 50);
            }
            let on_error = move |e: cpal::Error| {
                error(match e.kind() {
                    ErrorKind::Xrun | ErrorKind::DeviceChanged | ErrorKind::RealtimeDenied => Fault::Transient,
                    _ => Fault::Lost(e.to_string()),
                })
            };
            fn build<T: SizedSample>(dev: &cpal::Device, config: cpal::StreamConfig, rate: u32, channels: usize, mut data: DataFn,
                on_error: impl FnMut(cpal::Error) + Send + 'static) -> Result<cpal::Stream, cpal::Error>
            where
                f32: FromSample<T>,
            {
                let mut buf = Vec::new();
                dev.build_input_stream(config, move |input: &[T], _: &cpal::InputCallbackInfo| {
                    buf.clear();
                    buf.extend(input.iter().map(|&s| f32::from_sample(s)));
                    data(rate, channels, &buf);
                }, on_error, None)
            }
            let stream = match format {
                SampleFormat::F32 => build::<f32>(&dev, config, rate, channels, data, on_error),
                SampleFormat::I16 => build::<i16>(&dev, config, rate, channels, data, on_error),
                SampleFormat::I32 => build::<i32>(&dev, config, rate, channels, data, on_error),
                SampleFormat::U16 => build::<u16>(&dev, config, rate, channels, data, on_error),
                other => return Err(format!("sample format {other} is not supported")),
            }
            .map_err(|e| e.to_string())?;
            stream.play().map_err(|e| e.to_string())?;
            Ok(Box::new(stream))
        }
    }

    /// The id of Windows' default communications microphone (the one call apps use).
    #[cfg(windows)]
    fn communications_capture_id() -> Option<String> {
        use windows::Win32::Media::Audio::{eCapture, eCommunications, IMMDeviceEnumerator, MMDeviceEnumerator};
        use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, CLSCTX_ALL, COINIT_MULTITHREADED};
        // SAFETY: plain COM calls; a thread that already has COM keeps its own mode (the call then fails harmlessly)
        unsafe {
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            let enumerator: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).ok()?;
            let device = enumerator.GetDefaultAudioEndpoint(eCapture, eCommunications).ok()?;
            let id = device.GetId().ok()?;
            let text = id.to_string().ok();
            windows::Win32::System::Com::CoTaskMemFree(Some(id.0 as *const _));
            text
        }
    }

    #[cfg(not(windows))]
    fn communications_capture_id() -> Option<String> {
        None
    }
}

/// Whether "communications default" means anything on this system.
pub const HAS_COMMUNICATIONS: bool = cfg!(windows);

#[cfg(test)]
pub mod fake {
    //! A backend whose devices, audio and failures the test controls.
    use super::*;

    pub type Opened = (String, Arc<Mutex<DataFn>>, Arc<Mutex<ErrorFn>>);

    #[derive(Default)]
    pub struct FakeBackend {
        pub mics: Mutex<Vec<Device>>,
        pub output: Mutex<Option<Device>>,
        /// Every open: device id, and the callbacks the "driver" calls.
        pub opened: Mutex<Vec<Opened>>,
        pub refuse: Mutex<Vec<String>>,
        pub moves: bool,
    }

    pub fn dev(id: &str, name: &str, default: bool, communications: bool) -> Device {
        Device { id: id.into(), name: name.into(), default, communications }
    }

    impl FakeBackend {
        pub fn with(mics: Vec<Device>) -> Arc<Self> {
            Arc::new(FakeBackend { mics: Mutex::new(mics), ..Default::default() })
        }
        pub fn opens(&self) -> Vec<String> {
            self.opened.lock().unwrap().iter().map(|(id, _, _)| id.clone()).collect()
        }
        /// Audio from the newest stream.
        pub fn feed(&self, rate: u32, channels: usize, samples: &[f32]) {
            let data = self.opened.lock().unwrap().last().unwrap().1.clone();
            (data.lock().unwrap())(rate, channels, samples);
        }
        pub fn fail(&self, fault: Fault) {
            let error = self.opened.lock().unwrap().last().unwrap().2.clone();
            (error.lock().unwrap())(fault);
        }
    }

    impl Backend for FakeBackend {
        fn microphones(&self) -> Vec<Device> {
            self.mics.lock().unwrap().clone()
        }
        fn moves_streams(&self) -> bool {
            self.moves
        }
        fn system(&self) -> Option<Device> {
            self.output.lock().unwrap().clone()
        }
        fn open(&self, device: &Device, _system: bool, data: DataFn, error: ErrorFn) -> Result<Box<dyn Send>, String> {
            if self.refuse.lock().unwrap().contains(&device.id) {
                return Err("busy".into());
            }
            self.opened.lock().unwrap().push((device.id.clone(), Arc::new(Mutex::new(data)), Arc::new(Mutex::new(error))));
            Ok(Box::new(()))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::fake::{dev, FakeBackend};
    use super::*;

    fn wait_for(what: &str, mut ok: impl FnMut() -> bool) {
        let deadline = Instant::now() + Duration::from_secs(3);
        while !ok() {
            assert!(Instant::now() < deadline, "timed out waiting for {what}");
            std::thread::sleep(Duration::from_millis(5));
        }
    }

    fn laptop() -> Vec<Device> {
        vec![dev("array", "Microphone Array", true, false), dev("jabra-1", "Headset (Jabra)", false, true)]
    }

    #[test]
    fn resolves_the_choice() {
        let devices = laptop();
        let pick = |c: &Choice| resolve(c, &devices).map(|r| (r.device.id, r.fell_back));
        assert_eq!(pick(&Choice::Default), Some(("array".into(), false)));
        assert_eq!(pick(&Choice::Communications), Some(("jabra-1".into(), false)));
        let jabra = Choice::Device { id: "jabra-1".into(), name: "Headset (Jabra)".into() };
        assert_eq!(pick(&jabra), Some(("jabra-1".into(), false)));
        // the same headset on another USB port: new id, same name
        let moved = vec![dev("array", "Microphone Array", true, false), dev("jabra-2", "Headset (Jabra)", false, false)];
        assert_eq!(resolve(&jabra, &moved).map(|r| (r.device.id, r.fell_back)), Some(("jabra-2".into(), false)));
        // no communications default (macOS, Linux): the default
        let plain = vec![dev("a", "A", false, false), dev("b", "B", true, false)];
        assert_eq!(resolve(&Choice::Communications, &plain).unwrap().device.id, "b");
        // no device marked default: the first one
        assert_eq!(resolve(&Choice::Default, &[dev("x", "X", false, false)]).unwrap().device.id, "x");
        assert_eq!(resolve(&Choice::Default, &[]), None);
    }

    #[test]
    fn falls_back_when_the_chosen_device_is_gone() {
        let jabra = Choice::Device { id: "jabra-1".into(), name: "Headset (Jabra)".into() };
        let r = resolve(&jabra, &[dev("array", "Microphone Array", true, false)]).unwrap();
        assert_eq!((r.device.id.as_str(), r.fell_back), ("array", true));
        assert_eq!(resolve(&jabra, &[]), None);
    }

    #[test]
    fn computes_the_level() {
        assert_eq!(level_of(&[]), 0.0);
        assert_eq!(level_of(&[0.0; 480]), 0.0);
        assert!((level_of(&[1.0, -1.0, 1.0, -1.0]) - 1.0).abs() < 1e-6); // full scale
        assert!((level_of(&[0.1; 480]) - 2.0 / 3.0).abs() < 1e-3); // -20 dBFS
        assert!((level_of(&[0.001; 480]) - 0.0).abs() < 1e-3); // -60 dBFS is the floor
        assert!(level_of(&[0.03; 480]) > level_of(&[0.01; 480]));
    }

    #[test]
    fn delivers_chunks_and_the_meter_falls_back_to_zero() {
        let backend = FakeBackend::with(laptop());
        let (tx, rx) = channel();
        let start = Instant::now();
        let cap = Capture::start_with(backend.clone(), Source::Microphone(Choice::Default), start, Some(tx), Duration::from_millis(20));
        wait_for("open", || backend.opens() == ["array"]);
        assert_eq!(cap.status().device.as_deref(), Some("Microphone Array"));
        assert_eq!(cap.level(), 0.0);
        backend.feed(48_000, 2, &[0.1; 960]);
        let chunk = rx.recv_timeout(Duration::from_secs(1)).unwrap();
        assert_eq!((chunk.rate, chunk.channels, chunk.samples.len()), (48_000, 2, 960));
        assert!((cap.level() - 2.0 / 3.0).abs() < 1e-3);
        std::thread::sleep(LEVEL_HOLD + Duration::from_millis(50)); // nothing more arrives: the meter drops
        assert_eq!(cap.level(), 0.0);
        cap.stop();
    }

    #[test]
    fn rebuilds_after_the_stream_is_invalidated() {
        let backend = FakeBackend::with(laptop());
        let (tx, rx) = channel();
        let cap = Capture::start_with(backend.clone(), Source::Microphone(Choice::Communications), Instant::now(), Some(tx), Duration::from_millis(20));
        wait_for("open", || backend.opens() == ["jabra-1"]);
        backend.fail(Fault::Transient); // an xrun is not a reason to rebuild
        std::thread::sleep(Duration::from_millis(60));
        assert_eq!(backend.opens(), ["jabra-1"]);

        // the headset is unplugged: Windows moves the communications role to the array, the stream dies
        *backend.mics.lock().unwrap() = vec![dev("array", "Microphone Array", true, true)];
        backend.fail(Fault::Lost("StreamInvalidated".into()));
        wait_for("rebuild", || backend.opens() == ["jabra-1", "array"]);
        wait_for("status", || cap.status().device.as_deref() == Some("Microphone Array"));
        assert_eq!(cap.status().rebuilds, 1);
        backend.feed(16_000, 1, &[0.5; 160]);
        assert_eq!(rx.recv_timeout(Duration::from_secs(1)).unwrap().samples.len(), 160);
        cap.stop();
    }

    #[test]
    fn follows_the_chosen_device_away_and_back() {
        let backend = FakeBackend::with(laptop());
        let jabra = Choice::Device { id: "jabra-1".into(), name: "Headset (Jabra)".into() };
        let cap = Capture::start_with(backend.clone(), Source::Microphone(jabra), Instant::now(), None, Duration::from_millis(20));
        wait_for("open", || backend.opens() == ["jabra-1"]);

        *backend.mics.lock().unwrap() = vec![dev("array", "Microphone Array", true, false)];
        backend.fail(Fault::Lost("device unplugged".into()));
        wait_for("fallback", || cap.status() == Status { device: Some("Microphone Array".into()), fell_back: true, problem: None, rebuilds: 1 });

        *backend.mics.lock().unwrap() = laptop(); // plugged back in: the next check moves back to it
        wait_for("back", || backend.opens() == ["jabra-1", "array", "jabra-1"]);
        wait_for("status", || !cap.status().fell_back);

        cap.choose(Choice::Default); // a new pick in Settings applies at once
        wait_for("choose", || backend.opens().last().map(String::as_str) == Some("array"));
        cap.stop();
    }

    #[test]
    fn reopens_streams_a_new_device_may_have_taken() {
        let backend = Arc::new(FakeBackend { moves: true, ..Default::default() });
        *backend.mics.lock().unwrap() = vec![dev("array", "Microphone Array", true, false)];
        *backend.output.lock().unwrap() = Some(dev("speakers.monitor", "Speakers", true, false));
        let cap = Capture::start_with(backend.clone(), Source::System, Instant::now(), None, Duration::from_millis(20));
        wait_for("open", || backend.opens() == ["speakers.monitor"]);
        std::thread::sleep(Duration::from_millis(60));
        assert_eq!(backend.opens(), ["speakers.monitor"]); // nothing changed: no reopen
        backend.mics.lock().unwrap().push(dev("usb", "USB Headset", false, false));
        wait_for("reopen", || backend.opens() == ["speakers.monitor", "speakers.monitor"]);
        backend.mics.lock().unwrap().pop(); // a device going away moves nothing
        std::thread::sleep(Duration::from_millis(60));
        assert_eq!(backend.opens().len(), 2);
        cap.stop();
    }

    #[test]
    fn reports_no_device_and_retries() {
        let backend = FakeBackend::with(vec![]);
        let cap = Capture::start_with(backend.clone(), Source::Microphone(Choice::Default), Instant::now(), None, Duration::from_millis(20));
        wait_for("problem", || cap.status().problem.as_deref() == Some("No microphone found."));
        backend.refuse.lock().unwrap().push("array".into());
        *backend.mics.lock().unwrap() = laptop();
        wait_for("refused", || cap.status().problem.as_deref() == Some("Microphone Array could not be opened: busy"));
        backend.refuse.lock().unwrap().clear();
        wait_for("open", || cap.status().device.as_deref() == Some("Microphone Array"));
        cap.stop();

        let system = FakeBackend::with(vec![]);
        let cap = Capture::start_with(system.clone(), Source::System, Instant::now(), None, Duration::from_millis(20));
        wait_for("no output", || cap.status().problem.as_deref() == Some("No sound output found."));
        *system.output.lock().unwrap() = Some(dev("speakers", "Speakers", true, false));
        wait_for("loopback", || system.opens() == ["speakers"]);
        cap.choose(Choice::Default); // the call track has no choice
        std::thread::sleep(Duration::from_millis(60));
        assert_eq!(system.opens(), ["speakers"]);
        cap.stop();
    }
}

