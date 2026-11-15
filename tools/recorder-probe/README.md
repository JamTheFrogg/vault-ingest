# recorder-probe

Phase 0 check for the built-in recorder. It records the default microphone and the system audio (WASAPI loopback) at the same time and writes two files into a folder you name:

- `mic.wav`: you
- `system.wav`: everyone else

Both are 16 kHz, mono, 16-bit. Both follow one shared clock: a track that falls behind it is padded with silence (Windows delivers no system audio while nothing is playing), and a track is never allowed to run ahead of it. That keeps the two files equal in length.

It is a test program, not the recorder the app will ship. The owner's step-by-step version of this page is `docs/phase-0-checks.md`, check B.

## Build (Windows)

The Rust toolchain must be installed (`docs/phase-0-checks.md`, last section). In PowerShell, from the repository folder:

```powershell
cd tools\recorder-probe
cargo build --release --locked
```

The program is then `tools\recorder-probe\target\release\recorder-probe.exe`.

## Run a 3-minute test during a real call

Join the call first, with a headset. Then:

```powershell
.\target\release\recorder-probe.exe C:\vi-phase0\probe 180
```

- First argument: the folder for the two files. It is created if missing. If it already holds a `mic.wav` or `system.wav` the program stops; use a new folder for each test.
- Second argument: seconds to record. Leave it out to record until you press Ctrl+C.

During the three minutes:

1. Talk, and let the other side talk.
2. Have at least 20 seconds in which nothing plays on the PC (the other side is quiet).
3. Around the two-minute mark, change the headset: unplug it and plug it back in, or pick another output and input device in Windows sound settings.

## What a good result looks like

The program prints one line per device it opens, one line per stream problem, and a summary at the end. Example (your numbers will differ):

```
Recording into C:\vi-phase0\probe. Ctrl+C stops.
[   0.1s] mic: <microphone name> (48000 Hz, 1 ch, f32)
[   0.2s] system: <headset name> (48000 Hz, 2 ch, f32)
[ 121.4s] system stream: <what Windows reported>
[ 121.8s] system: <new device name> (48000 Hz, 2 ch, f32)
mic.wav      2880000 samples   180.000 s  16000 Hz 1 ch 16-bit  silence padded 0.412 s  dropped 0.000 s  peak 61.0%
system.wav   2880000 samples   180.000 s  16000 Hz 1 ch 16-bit  silence padded 47.250 s  dropped 0.000 s  peak 38.2%
MATCH: both tracks have the same length.
```

| Check | Pass |
|---|---|
| Both tracks present | Both files exist, both `peak` values are above 0%, and when you play them `mic.wav` holds your voice and `system.wav` holds the others. |
| Equal length | The last line is `MATCH`, both durations equal the seconds you asked for (within 0.5 s), and `dropped` is under 0.1 s on both tracks. |
| Silence padded | `system.wav` shows `silence padded` roughly equal to the time nothing was playing. `mic.wav` shows well under 1 second. In the labelled transcript (`docs/phase-0-checks.md`, check B) questions and answers appear in the order they were spoken. |
| Headset change survived | After the change, both files still contain sound, and the last line is still `MATCH`. A `stream:` line followed by a new device line is expected, with a gap of a second or two in that track; no line at all is also fine if the sound continues. |

A `MISMATCH` line, a peak of 0.0%, a line saying a track `is given up`, or sound that stops after the headset change is a fail. Keep the console output either way.

## Limits

- The resampler is a simple averaging filter. It is good enough for speech and for Whisper; the app's recorder will use a proper resampler.
- A track is only padded when it is more than 0.1 s behind the clock, so sound in the two tracks can be up to 0.1 s apart in time.
- Audio that would put a track ahead of the clock is cut and counted as `dropped`. More than a few hundredths of a second means the timing rule needs work before the real recorder is built.
- After 10 restarts of one stream the program stops trying; the rest of that track is silence.
- Linux and macOS: the program compiles and records the microphone through cpal's default host. System audio works only where that host can open the output device as an input; this is untested.

## Tests

```powershell
cargo test --locked
```

The tests cover the resampler, the silence padding and the cap at the clock. They need no sound device.
