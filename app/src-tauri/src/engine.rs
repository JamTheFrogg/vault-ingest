//! The Python engine (`vault-ingest serve`) as a child process: start, the ready line, stop, restart.

use std::collections::VecDeque;
use std::ffi::OsString;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant};

use serde::Serialize;

/// Names the Python interpreter to run the engine with; overrides the bundled engine and the repo's `.venv`.
pub const ENGINE_ENV: &str = "VAULT_INGEST_ENGINE";
const READY_TIMEOUT: Duration = Duration::from_secs(120);
const QUIT_GRACE: Duration = Duration::from_secs(10);
const STDERR_LINES: usize = 20;

#[derive(Clone, Debug, PartialEq, Serialize)]
#[serde(tag = "state", rename_all = "lowercase")]
pub enum Info {
    Starting,
    Ready { port: u16, token: String },
    Stopped { error: String },
}

#[derive(Debug, PartialEq)]
pub struct Launch {
    pub python: PathBuf,
    pub args: Vec<String>,
    pub bundled: bool,
}

/// Which interpreter runs the engine: `VAULT_INGEST_ENGINE`, else the bundled `engine/` folder, else the repo's `.venv`.
pub fn launch(override_python: Option<OsString>, resources: Option<&Path>, repo: &Path, parent_pid: u32) -> Launch {
    let serve = ["-m", "vault_ingest", "serve", "--parent-pid"].map(String::from);
    let mut args: Vec<String> = serve.into_iter().chain([parent_pid.to_string()]).collect();
    if let Some(python) = override_python.filter(|p| !p.is_empty()) {
        return Launch { python: python.into(), args, bundled: false };
    }
    if let Some(engine) = resources.map(|r| r.join("engine")) {
        let python = if cfg!(windows) { engine.join("python.exe") } else { engine.join("bin").join("python3") };
        if python.is_file() {
            args.insert(0, "-I".into()); // the bundled interpreter ignores PYTHON* variables and user site-packages
            return Launch { python, args, bundled: true };
        }
    }
    let venv = if cfg!(windows) { repo.join(".venv/Scripts/python.exe") } else { repo.join(".venv/bin/python") };
    let python = if venv.is_file() { venv } else { PathBuf::from(if cfg!(windows) { "python" } else { "python3" }) };
    Launch { python, args, bundled: false }
}

/// A line the engine sends to the shell: `@@ <command> [json]` (section 3 of the architecture plan).
pub fn parse_command(line: &str) -> Option<(String, serde_json::Value)> {
    let rest = line.trim().strip_prefix("@@ ")?;
    let json_at = rest.find('{').unwrap_or(rest.len());
    let name = rest[..json_at].split_whitespace().collect::<Vec<_>>().join(" "); // `popup show`, `config`
    let json = rest[json_at..].trim();
    let value = if json.is_empty() { serde_json::Value::Null } else { serde_json::from_str(json).ok()? };
    (!name.is_empty()).then_some((name, value))
}

/// The port from `@@ ready {"port": N}`; any other line is log text.
pub fn parse_ready(line: &str) -> Option<u16> {
    let json = line.trim().strip_prefix("@@ ready ")?;
    serde_json::from_str::<serde_json::Value>(json).ok()?.get("port")?.as_u64()?.try_into().ok()
}

fn new_token() -> String {
    let mut bytes = [0u8; 32];
    getrandom::fill(&mut bytes).expect("the system random source failed");
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

struct Running {
    child: Child,
    stdin: Option<ChildStdin>,
}

#[derive(Default)]
struct State {
    info: Option<Info>,
    generation: u64,
    running: Option<Running>,
}

pub type Handler = Arc<dyn Fn(&str, serde_json::Value) + Send + Sync>;

#[derive(Default, Clone)]
pub struct Engine {
    state: Arc<(Mutex<State>, Condvar)>,
    handler: Arc<Mutex<Option<Handler>>>,
}

impl Engine {
    /// Start the engine. `home` is the settings folder (VAULT_INGEST_HOME), set only for the bundled engine.
    pub fn start(&self, launch: Launch, cwd: Option<PathBuf>, home: Option<PathBuf>) {
        let (lock, changed) = &*self.state;
        let token = new_token();
        let mut command = Command::new(&launch.python);
        command.args(&launch.args).env("VAULT_INGEST_TOKEN", &token).env("PYTHONUTF8", "1")
            .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped());
        if let Some(dir) = cwd {
            command.current_dir(dir);
        }
        if let Some(dir) = home {
            command.env("VAULT_INGEST_HOME", dir);
        }
        #[cfg(windows)]
        {
            use std::os::windows::process::CommandExt;
            command.creation_flags(0x0800_0000); // CREATE_NO_WINDOW: no console window for the engine
        }

        let mut state = lock.lock().unwrap();
        state.generation += 1;
        let generation = state.generation;
        let mut child = match command.spawn() {
            Ok(child) => child,
            Err(e) => {
                state.info = Some(Info::Stopped { error: format!("Could not start {}: {e}", launch.python.display()) });
                changed.notify_all();
                return;
            }
        };
        let (stdout, stderr, stdin) = (child.stdout.take().unwrap(), child.stderr.take().unwrap(), child.stdin.take());
        state.info = Some(Info::Starting);
        state.running = Some(Running { child, stdin });
        drop(state);

        let tail = Arc::new(Mutex::new(VecDeque::new()));
        let err_tail = tail.clone();
        std::thread::spawn(move || {
            for line in BufReader::new(stderr).lines().map_while(Result::ok) {
                let mut lines = err_tail.lock().unwrap();
                if lines.len() == STDERR_LINES {
                    lines.pop_front();
                }
                lines.push_back(line);
            }
        });
        let shared = self.state.clone();
        let handler = self.handler.clone();
        std::thread::spawn(move || {
            // stdout is read to the end: a full pipe would block the engine
            for line in BufReader::new(stdout).lines().map_while(Result::ok) {
                if let Some(port) = parse_ready(&line) {
                    let (lock, changed) = &*shared;
                    let mut state = lock.lock().unwrap();
                    if state.generation == generation {
                        state.info = Some(Info::Ready { port, token: token.clone() });
                        changed.notify_all();
                    }
                    drop(state);
                }
                let Some((name, value)) = parse_command(&line) else { continue };
                let current = handler.lock().unwrap().clone();
                if let Some(handle) = current {
                    handle(&name, value); // `ready` too: the shell tells a new engine what it is recording
                }
            }
            let (lock, changed) = &*shared;
            let mut state = lock.lock().unwrap();
            if state.generation != generation {
                return; // stopped or restarted on purpose
            }
            let code = state.running.take().and_then(|mut r| r.child.wait().ok());
            std::thread::sleep(Duration::from_millis(200)); // let the stderr reader take the last lines
            let lines: Vec<String> = tail.lock().unwrap().iter().filter(|l| !l.trim().is_empty()).cloned().collect();
            let why = lines[lines.len().saturating_sub(3)..].join("\n");
            let code = code.and_then(|c| c.code()).map_or_else(|| "no exit code".into(), |n| format!("code {n}"));
            state.info = Some(Info::Stopped {
                error: if why.is_empty() { format!("It exited ({code}).") } else { format!("It exited ({code}): {why}") },
            });
            changed.notify_all();
        });
    }

    /// Called on the stdout reader thread for every `@@` line, `ready` included.
    pub fn set_handler(&self, handler: Handler) {
        *self.handler.lock().unwrap() = Some(handler);
    }

    /// One line to the engine's stdin; dropped when no engine runs (it learns the state when it starts).
    pub fn send(&self, line: &str) {
        let mut state = self.state.0.lock().unwrap();
        if let Some(stdin) = state.running.as_mut().and_then(|r| r.stdin.as_mut()) {
            let _ = stdin.write_all(format!("{}\n", line.trim_end()).as_bytes()).and_then(|_| stdin.flush());
        }
    }

    /// The engine's address, waiting while it starts.
    pub fn info(&self) -> Info {
        let (lock, changed) = &*self.state;
        let deadline = Instant::now() + READY_TIMEOUT;
        let mut state = lock.lock().unwrap();
        while matches!(state.info, None | Some(Info::Starting)) {
            let left = deadline.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Info::Stopped { error: "It did not get ready within two minutes.".into() };
            }
            state = changed.wait_timeout(state, left).unwrap().0;
        }
        state.info.clone().unwrap()
    }

    /// Ask the engine to quit (it stops `watch` and finishes the current write), then kill it if it does not.
    pub fn stop(&self) {
        let (lock, changed) = &*self.state;
        let mut state = lock.lock().unwrap();
        state.generation += 1;
        state.info = Some(Info::Stopped { error: "It was stopped.".into() });
        let running = state.running.take();
        changed.notify_all();
        drop(state);
        let Some(mut running) = running else { return };
        if let Some(mut stdin) = running.stdin.take() {
            let _ = stdin.write_all(b"quit\n");
        }
        let deadline = Instant::now() + QUIT_GRACE;
        while Instant::now() < deadline {
            if !matches!(running.child.try_wait(), Ok(None)) {
                return;
            }
            std::thread::sleep(Duration::from_millis(100));
        }
        let _ = running.child.kill();
        let _ = running.child.wait();
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn reads_the_ready_line_only() {
        assert_eq!(parse_ready("@@ ready {\"port\": 51181}"), Some(51181));
        assert_eq!(parse_ready("@@ ready {\"port\": 51181, \"token\": \"x\"}\r\n"), Some(51181));
        assert_eq!(parse_ready("2026-11-15 INFO @@ ready {\"port\": 1}"), None);
        assert_eq!(parse_ready("@@ ready {\"port\": 70000}"), None);
        assert_eq!(parse_ready("@@ ready not json"), None);
        assert_eq!(parse_ready("@@ popup show {}"), None);
    }

    #[test]
    fn reads_commands() {
        let cmd = |l: &str| parse_command(l);
        assert_eq!(cmd("@@ popup show {\"session\": \"m1\"}"), Some(("popup show".into(), serde_json::json!({"session": "m1"}))));
        assert_eq!(cmd("@@ popup hide"), Some(("popup hide".into(), serde_json::Value::Null)));
        assert_eq!(cmd("@@ record stop\r\n"), Some(("record stop".into(), serde_json::Value::Null)));
        assert_eq!(cmd("@@ config {\"a\": 1}"), Some(("config".into(), serde_json::json!({"a": 1}))));
        assert_eq!(cmd("@@ ready {\"port\": 5}").map(|c| c.0), Some("ready".into()));
        assert_eq!(cmd("@@ record start {broken"), None);
        assert_eq!(cmd("INFO @@ popup show {}"), None);
    }

    #[test]
    fn picks_the_interpreter() {
        let tmp = std::env::temp_dir().join(format!("vi-launch-{}", std::process::id()));
        let (resources, repo) = (tmp.join("res"), tmp.join("repo"));
        let bundled = if cfg!(windows) { resources.join("engine/python.exe") } else { resources.join("engine/bin/python3") };
        let venv = if cfg!(windows) { repo.join(".venv/Scripts/python.exe") } else { repo.join(".venv/bin/python") };
        let serve = |extra: &[&str]| -> Vec<String> {
            extra.iter().chain(&["-m", "vault_ingest", "serve", "--parent-pid", "7"]).map(|s| s.to_string()).collect()
        };

        let system = launch(None, Some(&resources), &repo, 7);
        assert!(!system.bundled && system.args == serve(&[]));
        assert!(system.python == Path::new("python") || system.python == Path::new("python3"));

        std::fs::create_dir_all(venv.parent().unwrap()).unwrap();
        std::fs::write(&venv, "").unwrap();
        assert_eq!(launch(None, Some(&resources), &repo, 7).python, venv);

        std::fs::create_dir_all(bundled.parent().unwrap()).unwrap();
        std::fs::write(&bundled, "").unwrap();
        assert_eq!(launch(None, Some(&resources), &repo, 7), Launch { python: bundled, args: serve(&["-I"]), bundled: true });

        let chosen = launch(Some("/opt/py/bin/python".into()), Some(&resources), &repo, 7);
        assert_eq!(chosen, Launch { python: "/opt/py/bin/python".into(), args: serve(&[]), bundled: false });
        assert!(launch(Some("".into()), Some(&resources), &repo, 7).bundled);
        std::fs::remove_dir_all(tmp).unwrap();
    }

    #[test]
    fn a_missing_interpreter_is_reported_not_waited_for() {
        let engine = Engine::default();
        engine.start(Launch { python: "/nonexistent/python".into(), args: vec![], bundled: false }, None, None);
        assert!(matches!(engine.info(), Info::Stopped { error } if error.starts_with("Could not start")));
    }

    #[cfg(unix)]
    #[test]
    fn ready_then_exit_then_restart() {
        let script = |body: &str| Launch { python: "sh".into(), args: vec!["-c".into(), body.into()], bundled: false };
        let engine = Engine::default();
        // reads the token from the environment like the real engine, then exits with a reason on stderr
        engine.start(script("test -n \"$VAULT_INGEST_TOKEN\" && echo '@@ ready {\"port\": 4242}'; read line; echo 'VAULT_PATH is not set.' >&2; exit 1"), None, None);
        let Info::Ready { port, token } = engine.info() else { panic!("not ready") };
        assert_eq!((port, token.len()), (4242, 64));
        engine.stop();                                          // sends "quit": the script's `read` returns
        assert_eq!(engine.info(), Info::Stopped { error: "It was stopped.".into() });

        engine.start(script("echo 'VAULT_PATH is not set.' >&2; exit 3"), None, None);
        assert_eq!(engine.info(), Info::Stopped { error: "It exited (code 3): VAULT_PATH is not set.".into() });
    }
}
