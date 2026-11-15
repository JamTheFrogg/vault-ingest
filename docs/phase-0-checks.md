# Phase 0 checks

Three checks on the Windows PC with the NVIDIA card, before the desktop app is built. Each takes under an hour once the downloads are done.

| | Check | Answers |
|---|---|---|
| A | Built-in model on the 8 GB card | Does today's code work against llama.cpp instead of LM Studio? Does the model fit? Are the notes good enough? Do screenshots work? |
| B | Recorder probe | Do microphone and system audio record as two equal-length tracks, and survive a headset change? |
| C | Engine bundle | Does Whisper run on the GPU from the Python that the app will ship? |

Paths in this guide are examples; substitute your own:

- This tool: `C:\path\to\vault-ingest`
- Test folder: `C:\vi-phase0` (everything from these checks goes here, so cleaning up is deleting one folder)

Everything marked **unverified** could not be confirmed on 14 Nov 2026 from the project's own page or by a test. Everything else was.

## Before you start

1. Create the test folder: `mkdir C:\vi-phase0`
2. Save the package versions that work on this PC today. In PowerShell, in the vault-ingest folder:

   ```powershell
   .\.venv\Scripts\python -m pip freeze > C:\vi-phase0\pip-freeze.txt
   ```

3. Start the builds for checks B and C. They run on GitHub while you do check A.
   1. Commit and push `tools/recorder-probe/`, `scripts/build_engine.py`, `requirements-app.txt` and `.github/workflows/phase0.yml`. The workflow shows up only once it is on the default branch.
   2. On GitHub open **Actions**, pick **Phase 0 kit**, click **Run workflow**.
   3. When both jobs are green, open the run. Under **Artifacts** there are two downloads: `recorder-probe-windows` and `engine-windows`.

<div style="page-break-before: always"></div>

## A. Built-in model on the 8 GB card

No code. You start llama.cpp's server by hand and point today's vault-ingest at it.

### A1. Download four files

| What | File | Size | Licence | Where |
|---|---|---|---|---|
| llama.cpp for Windows, CUDA 12.4 | `llama-b12380-bin-win-cuda-12.4-x64.zip` | 319 MB | MIT | <https://github.com/ggml-org/llama.cpp/releases/tag/b12380> |
| CUDA runtime for it | `cudart-llama-bin-win-cuda-12.4-x64.zip` | 391 MB | NVIDIA's terms (**unverified**) | same page |
| Model: Gemma 4 12B QAT | `gemma-4-12B-it-qat-UD-Q4_K_XL.gguf` | 6.72 GB | apache-2.0 | <https://huggingface.co/unsloth/gemma-4-12B-it-qat-GGUF/tree/main> |
| Its image projector | `mmproj-F16.gguf` | 175 MB | apache-2.0 | same page |

- On a newer llama.cpp release the names follow the same pattern: `llama-<tag>-bin-win-cuda-12.4-x64.zip` and `cudart-llama-bin-win-cuda-12.4-x64.zip` (the second has no tag in its name). All releases: <https://github.com/ggml-org/llama.cpp/releases>. There is also a `cuda-13.5` pair; use 12.4, the one the plan names.
- On Hugging Face, click the download arrow next to each file name.
- Whether Gemma 4 12B reads images in llama.cpp is **unverified**: llama.cpp's list of tested image models has Gemma 4 E2B, E4B, 26B and 31B, not 12B. This check finds out.

The lighter model, only needed if the 12B fails step A4 or A6:

| What | File | Size | Licence | Where |
|---|---|---|---|---|
| Model: Gemma 4 E4B | `gemma-4-E4B-it-Q4_K_M.gguf` | 4.98 GB | apache-2.0 | <https://huggingface.co/unsloth/gemma-4-E4B-it-GGUF/tree/main> |
| Its image projector | `mmproj-F16.gguf` | 990 MB | apache-2.0 | same page |

The two projectors have the same file name and are different files. Keep each model in its own folder.

### A2. Unpack

1. Unzip **both** zip files into `C:\vi-phase0\llama`. `llama-server.exe` and `cublas64_12.dll` must end up side by side in that folder.
2. Put the two 12B files into `C:\vi-phase0\models\12b`.

### A3. Free the graphics card

1. Pick a time when no call is expected. The background watcher keeps running with its old settings and would load LM Studio's model if a call arrived.
2. Check that nothing is being processed:

   ```powershell
   .\.venv\Scripts\vault-ingest status
   ```

3. If LM Studio has a chat model loaded, unload it: in LM Studio click **Eject**, or run `lms unload <model id>`.
4. Note the memory in use before you start: `nvidia-smi`

### A4. Start llama-server

In a PowerShell window (it stays open; the server logs into it):

```powershell
cd C:\vi-phase0\llama
.\llama-server.exe -m C:\vi-phase0\models\12b\gemma-4-12B-it-qat-UD-Q4_K_XL.gguf --mmproj C:\vi-phase0\models\12b\mmproj-F16.gguf -c 8192 -ngl auto -np 1 --host 127.0.0.1 --port 8080 --alias gemma-4-12b
```

| Part | Meaning |
|---|---|
| `-m` | the model file |
| `--mmproj` | the image projector; the screenshot step needs it |
| `-c 8192` | context size, the same as `LOCAL_CONTEXT` in vault-ingest |
| `-ngl auto` | llama-server puts as many layers on the GPU as fit and keeps about 1 GB free |
| `-np 1` | one request at a time, as vault-ingest does with LM Studio |
| `--host 127.0.0.1 --port 8080` | only this PC can reach it. Stated on purpose: this release's default port is 9931. |
| `--alias gemma-4-12b` | the model name vault-ingest will see and write into the notes |

Then:

1. In the server window find the line `load_tensors: offloaded N/M layers to GPU` and write down N and M. N equal to M means the whole model is on the card.
2. Open <http://127.0.0.1:8080/health> in a browser. It must show `{"status": "ok"}`. While the model is still loading it shows "Loading model".
3. Run `nvidia-smi` again and write down used and total memory.

If N is smaller than M, stop the server (Ctrl+C) and start it once more with `-ngl all` instead of `-ngl auto`. That forces every layer onto the card. Then watch **Task Manager > Performance > GPU > Shared GPU memory** during step A6: if it grows while the model writes, the model does not fit.

### A5. Point vault-ingest at it

1. Save a copy of your settings **outside** the repository (the file holds your API key):

   ```powershell
   copy .env C:\vi-phase0\env-before.txt
   ```

2. Open `.env` and set these four lines. Change the existing lines if they are there; each name must appear once.

   ```
   LLM_MODE=local
   LOCAL_BASE_URL=http://127.0.0.1:8080/v1
   LOCAL_MANAGE_MODELS=false
   LOCAL_MODEL=auto
   ```

   `LOCAL_MANAGE_MODELS=false` stops vault-ingest from calling LM Studio's `lms` to load and unload models.

### A6. Run the existing commands

In a second PowerShell window, in the vault-ingest folder:

1. Connection test. It must end with `LLM answered: Test (ok)` and the model name `gemma-4-12b`.

   ```powershell
   .\.venv\Scripts\vault-ingest check
   ```

2. Speed and memory. Write down `tokens/sec`, `ttft` and the `dedicated VRAM` line.

   ```powershell
   .\.venv\Scripts\vault-ingest bench
   ```

3. Pick one finished call that has screenshots. Copy that client's whole folder to `C:\vi-phase0\before\` so you can compare afterwards.
4. Redo that call. Do **not** add `--from-audio`: Whisper would need the memory the chat model is holding.

   ```powershell
   .\.venv\Scripts\vault-ingest reprocess "Clients\<Client>\Transcripts\raw\<file>.txt"
   ```

### A7. What to look at

| Question | Where to look | Good |
|---|---|---|
| Does it fit? | The `offloaded N/M` line; `nvidia-smi` while it writes; Shared GPU memory in Task Manager | N = M, and Shared GPU memory stays flat |
| How fast? | `tokens/sec` from `bench`; the lines ending in `tok/s` in `_System\ingest.log`; how long `reprocess` took in total | Compare with the same call in LM Studio. No pass mark is set; send the numbers. |
| Note quality? | The new call note next to the copy in `C:\vi-phase0\before\`. Its header must say `generated_by: local/gemma-4-12b`. | Names, decisions and open items are right; nothing invented; right language |
| Do screenshots work? | The `## Screenshots` section of the note; `_System\ingest.log` | Each screenshot has a description. The log line `Screenshots are attached without descriptions` means the model refused images; `Could not describe screenshot` means one image failed. |

If the 12B does not fit or cannot read the screenshots, repeat A4 to A7 with the E4B files in `C:\vi-phase0\models\e4b` and `--alias gemma-4-e4b`.

### A8. Put everything back

1. Stop llama-server: Ctrl+C in its window. `nvidia-smi` shows the memory free again.
2. Restore the settings and confirm your usual model answers:

   ```powershell
   copy C:\vi-phase0\env-before.txt .env
   .\.venv\Scripts\vault-ingest check
   ```

3. Run the same `reprocess` command from A6 again. The call is rewritten by your usual model. The wording will not be identical to the original; the exact original is in `C:\vi-phase0\before\`, and the profile as it was before each change is in `_System\backups`.
4. Delete `C:\vi-phase0\env-before.txt`. It holds your API key.

<div style="page-break-before: always"></div>

## B. Recorder probe

A small program that records the microphone and the system audio at the same time into `mic.wav` and `system.wav`.

### B1. Get the program

Either:

- **Download:** from the workflow run, download `recorder-probe-windows`, unzip it, and put `recorder-probe.exe` into `C:\vi-phase0`.
- **Build it yourself** (needs Rust, see the last section):

  ```powershell
  cd tools\recorder-probe
  cargo build --release --locked
  copy target\release\recorder-probe.exe C:\vi-phase0
  ```

Windows may warn that the program is unrecognized. It is unsigned; click *More info → Run anyway*.

### B2. The 3-minute test

1. Join a real call, with a headset on.
2. Start the probe. The folder must be new for each test; the program refuses to overwrite a recording.

   ```powershell
   C:\vi-phase0\recorder-probe.exe C:\vi-phase0\probe1 180
   ```

3. Talk, and let the other side talk.
4. Have at least 20 seconds in which nothing plays on the PC.
5. Around the two-minute mark, change the headset: unplug it and plug it back in, or pick another output and input device in Windows sound settings.
6. After 180 seconds the program stops by itself and prints a summary. Copy the whole console output into `C:\vi-phase0\probe1\console.txt`.

### B3. What to check

| Check | Pass |
|---|---|
| Both tracks present | Both `peak` values are above 0%. Played back, `mic.wav` holds your voice and `system.wav` holds the others. |
| Equal length | The last line is `MATCH`. Both durations are 180 s (within 0.5 s). `dropped` is under 0.1 s on both lines. |
| Silence padded | On the `system.wav` line, `silence padded` is roughly the time nothing was playing. On the `mic.wav` line it is well under 1 second, plus the gap of the headset change. |
| Headset change survived | Both files still have sound after the change. A `stream:` line followed by a new device line is expected. |

A `MISMATCH` line, a peak of 0.0%, a line saying a track `is given up`, or sound that stops after the headset change is a fail.

### B4. See the labelled transcript

1. Stop llama-server from check A if it is still running. Whisper needs the graphics card.
2. Transcribe the microphone file. vault-ingest finds `system.wav` beside it and transcribes both. Nothing is added to the vault.

   ```powershell
   .\.venv\Scripts\vault-ingest transcribe C:\vi-phase0\probe1\mic.wav --out C:\vi-phase0\probe1\transcript.txt
   ```

3. Open `transcript.txt`. Your lines start with your name (`ME_NAME` in `.env`, `Me` if unset), the others' lines with `Other`, in the order things were said:

   ```
   [00:00:12] Me: ...
   [00:00:19] Other: ...
   ```

Dropping the folder into the recordings folder does not work yet: the watcher there only looks for `audio.mp4`. That change belongs to the recorder phase.

<div style="page-break-before: always"></div>

## C. Engine bundle

The app will ship its own Python with vault-ingest inside. This check runs Whisper from that Python, with the CUDA libraries downloaded the way the app will do it on first run.

### C1. Get the engine

1. From the workflow run, download `engine-windows`. Download size is **unverified** (the same folder built on Linux is 634 MB unpacked).
2. Right-click the zip, **Properties**, tick **Unblock** if the box is there, OK. Then unzip it to `C:\vi-phase0\engine`, so that `C:\vi-phase0\engine\python.exe` exists.
3. Confirm you are running the bundled copy, not your `.venv`. Both printed paths must start with `C:\vi-phase0\engine`.

   ```powershell
   cd C:\vi-phase0\engine
   .\python.exe -I -c "import sys, vault_ingest; print(sys.executable); print(vault_ingest.__file__)"
   ```

### C2. Download the CUDA libraries

They are not in the bundle on purpose. About 1.25 GB:

```powershell
.\python.exe -m pip install --target C:\vi-phase0\pylibs nvidia-cublas-cu12==12.9.2.10 nvidia-cudnn-cu12==9.28.1.4
$env:PYTHONPATH = "C:\vi-phase0\pylibs"
```

- These are the newest versions with a Windows build on PyPI (<https://pypi.org/project/nvidia-cublas-cu12/>, <https://pypi.org/project/nvidia-cudnn-cu12/>). Whether they work on your card is **unverified**. If C3 fails, look up the two `nvidia-` lines in `C:\vi-phase0\pip-freeze.txt` and install those versions into a new folder instead.
- `$env:PYTHONPATH` lasts only for this PowerShell window. Run C3 in the same window.

### C3. Run Whisper from the bundle

Stop llama-server from check A first. `--env` points at your normal settings file, so the vault and the language are your usual ones.

1. Load test, no audio needed. It prints one line per attempt.

   ```powershell
   .\python.exe -m vault_ingest --env C:\path\to\vault-ingest\.env doctor gpu
   ```

2. Transcribe one recording (any audio file from a past call). Nothing is added to the vault.

   ```powershell
   .\python.exe -m vault_ingest --env C:\path\to\vault-ingest\.env transcribe "C:\path\to\recording.m4a" --out C:\vi-phase0\transcript-c.txt
   ```

   While it runs, run `nvidia-smi` in another window.

### C4. What proves Whisper ran on the GPU

| Evidence | Pass |
|---|---|
| `doctor gpu` | The line `cuda/float16: worked (exit 0)` |
| Last status line of `transcribe` | `<seconds>s — faster-whisper/large-v3/<language> on cuda/float16` |
| `nvidia-smi` during the run | Memory in use goes up by a few GB while it transcribes (a `python.exe` process is usually listed too) |

`on cpu/int8` in the status line means the GPU libraries did not load and Whisper fell back to the processor. That is a fail for this check; send the `doctor gpu` output.

To put things back: close the PowerShell window. Nothing outside `C:\vi-phase0` was changed, apart from log lines in `_System\ingest.log` and `_System\transcribe.log`.

<div style="page-break-before: always"></div>

## Results to send back

| Check | Pass criteria | Send back |
|---|---|---|
| A. Built-in model | `check` answers; `offloaded N/M` with N = M and Shared GPU memory flat; notes acceptable to you; screenshots described | The `offloaded N/M layers to GPU` line. `nvidia-smi` used / total before the server, after loading, and while writing. The full `bench` output. Every `ingest.log` line from the `reprocess` run that ends in `tok/s` or mentions `screenshot`. Total `reprocess` time. Your verdict on the note in one sentence. Which model: 12B or E4B. |
| B. Recorder probe | `MATCH`; both peaks above 0%; `dropped` under 0.1 s; sound after the headset change; transcript labelled `Me` / `Other` in the right order | `console.txt` (the whole console output). What you did for the headset change and when. The first 20 lines of `transcript.txt`. |
| C. Engine bundle | Both paths inside `C:\vi-phase0\engine`; `cuda/float16: worked`; status line ends in `on cuda/float16` | The two printed paths. The full `doctor gpu` output. The last status line of `transcribe` and the length of the recording. The size of the `engine-windows` download. |
| All | | `C:\vi-phase0\pip-freeze.txt` |

<div style="page-break-before: always"></div>

## Installing Rust on Windows

Only needed to build the recorder probe yourself, and later for the app.

1. Download and run `rustup-init.exe` from <https://www.rust-lang.org/tools/install> (64-bit: <https://static.rust-lang.org/rustup/dist/x86_64-pc-windows-msvc/rustup-init.exe>).
2. If it says the Visual Studio C++ build tools are missing, let it install them. Doing it by hand: <https://visualstudio.microsoft.com/downloads/>, workload **Desktop development with C++**.
3. Accept the default installation (stable, `x86_64-pc-windows-msvc`).
4. Open a new PowerShell window and check: `cargo --version`

Details: <https://rust-lang.github.io/rustup/installation/windows-msvc.html>
