//! hook-client: the command the harness runs for a dispatched hook event (policy).
//!
//! Usage: hook-client <python> <hook-dispatch.py> <Event> [args...]
//!
//! Starting Python costs about 11 ms and the dispatcher's imports about 15 ms more, on
//! every hook event. This client starts in about 1 ms and hands the call to
//! hook-server.py, a warm Python process on this host that forks a child per call with
//! the dispatcher already loaded. The child runs hook-dispatch's run() as a cold start
//! would, with this process's stdin, environment, working directory and arguments, and
//! sends back its stdout, stderr and exit code.
//!
//! Every failure falls back to the cold path: no server, a server that is reloading, a
//! server loaded from another dispatcher or Python, or a broken reply before any output
//! was written. The fallback runs `<python> <hook-dispatch.py> <Event> [args...]` with the
//! same stdin, which is the command this client replaced. A client that finds no server
//! also starts one in the background, at most once per SPAWN_GAP_SECS, for the next call.
//!
//! Limits. A send to the server gives up after SOCKET_SECS on every event. A PreToolUse
//! call also waits at most SOCKET_SECS for the reply, then runs cold, and a cold run that
//! passes COLD_SECS is killed and the call fails open: a served child that never answered
//! once held tool calls for 12 and 24 minutes (2026-10-05). The other events keep an
//! unbounded reply wait. The server answers once, when every guard has finished, and their
//! guards may rightly run longer (SessionStart's self-heal has 120 s), so a limit there
//! would start a slow run a second time beside the first. Each wait that runs out adds a
//! line to ~/.local/state/agent-context/hook-client-expired.log.
//!
//! Standard library only, so every host builds it with rustc alone (hook-client-build.py).
//!
//! Protocol, version 1. Request: MAGIC, a big-endian u32 length, then that many bytes of
//! NUL-terminated fields (cwd, python, dispatcher, argument count, the arguments, then
//! every KEY=VALUE of the environment), then the hook payload until the write side
//! closes. Reply: frames of one tag byte and a big-endian u32 length: `o` stdout bytes,
//! `e` stderr bytes, `x` the exit code (one byte, ends the reply), `r` retry cold.

use std::env;
use std::ffi::OsString;
use std::fs::{self, OpenOptions};
use std::io::{self, ErrorKind, Read, Write};
use std::net::Shutdown;
use std::os::unix::ffi::{OsStrExt, OsStringExt};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{self, exit, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const MAGIC: &[u8] = b"AHS1";
const SPAWN_GAP_SECS: u64 = 10;
/// The exit code of a hook that could not start at all: a non-blocking error to the harness.
const CANNOT_RUN: i32 = 1;
/// The longest one send to the server may block, and the longest a bounded call waits
/// for the reply.
const SOCKET_SECS: u64 = 30;
/// The longest a bounded call's cold run may take before it is killed.
const COLD_SECS: u64 = 120;
/// The event whose reply wait and cold run are bounded (see Limits above).
/// home-settings-sync.py sets this event's harness timeout above the sum of the three.
const BOUNDED_EVENT: &str = "PreToolUse";

fn cache_dir() -> PathBuf {
    let home = env::var_os("HOME").unwrap_or_default();
    Path::new(&home).join(".cache").join("agent-context")
}

fn socket_path() -> PathBuf {
    env::var_os("HOOK_SERVER_SOCK")
        .map(PathBuf::from)
        .unwrap_or_else(|| cache_dir().join("hook-server.sock"))
}

/// Seconds since the epoch as an ISO 8601 UTC time (days to a civil date, Hinnant's method).
fn utc_stamp(secs: u64) -> String {
    let (days, rem) = (secs / 86400, secs % 86400);
    let z = days + 719468;
    let (era, doe) = (z / 146097, z % 146097);
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + u64::from(month <= 2);
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        year,
        month,
        day,
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

/// One call's event, its clock, and whether its reply wait and cold run are bounded.
struct Call<'a> {
    event: &'a OsString,
    started: Instant,
    bounded: bool,
}

impl Call<'_> {
    /// Record a wait that ran out: when, the event, this process, which wait, and the
    /// seconds since this client started.
    fn expired(&self, wait: &str) {
        let home = env::var_os("HOME").unwrap_or_default();
        let dir = Path::new(&home).join(".local").join("state").join("agent-context");
        let _ = fs::create_dir_all(&dir);
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |d| d.as_secs());
        let log = OpenOptions::new()
            .create(true)
            .append(true)
            .open(dir.join("hook-client-expired.log"));
        if let Ok(mut log) = log {
            let _ = writeln!(
                log,
                "{} event={} pid={} wait={} elapsed={:.1}",
                utc_stamp(now),
                self.event.to_string_lossy(),
                process::id(),
                wait,
                self.started.elapsed().as_secs_f64()
            );
        }
    }
}

fn timed_out(err: &io::Error) -> bool {
    matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut)
}

fn push_field(buf: &mut Vec<u8>, field: &[u8]) {
    buf.extend_from_slice(field);
    buf.push(0);
}

fn request(python: &OsString, dispatcher: &OsString, args: &[OsString]) -> Vec<u8> {
    let mut fields = Vec::new();
    let cwd = env::current_dir().map(|p| p.into_os_string()).unwrap_or_default();
    push_field(&mut fields, cwd.as_bytes());
    push_field(&mut fields, python.as_bytes());
    push_field(&mut fields, dispatcher.as_bytes());
    push_field(&mut fields, args.len().to_string().as_bytes());
    for arg in args {
        push_field(&mut fields, arg.as_bytes());
    }
    for (key, value) in env::vars_os() {
        let mut pair = key.into_vec();
        pair.push(b'=');
        pair.extend_from_slice(value.as_bytes());
        push_field(&mut fields, &pair);
    }
    let mut out = Vec::with_capacity(fields.len() + 8);
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&(fields.len() as u32).to_be_bytes());
    out.extend_from_slice(&fields);
    out
}

enum Outcome {
    /// The server ran the call; exit with this code.
    Done(i32),
    /// Nothing was written yet: run the call cold.
    Cold,
    /// The reply broke after output was written: fail open.
    Broken,
}

/// The outcome of a reply that stopped at `err`, logging it when it was the wait that ran out.
fn reply_lost(call: &Call, err: &io::Error, wrote: bool) -> Outcome {
    if timed_out(err) {
        call.expired("reply");
    }
    if wrote {
        Outcome::Broken
    } else {
        Outcome::Cold
    }
}

fn serve(sock: &Path, header: &[u8], payload: &[u8], call: &Call) -> Outcome {
    // std has no connect timeout for a Unix socket; a connect to a local path answers or
    // is refused at once, so this one stays unbounded.
    let mut stream = match UnixStream::connect(sock) {
        Ok(stream) => stream,
        Err(_) => return Outcome::Cold,
    };
    let limit = Some(Duration::from_secs(SOCKET_SECS));
    let _ = stream.set_write_timeout(limit);
    if call.bounded {
        let _ = stream.set_read_timeout(limit);
    }
    let sent = stream
        .write_all(header)
        .and_then(|_| stream.write_all(payload))
        .and_then(|_| stream.shutdown(Shutdown::Write));
    if let Err(err) = sent {
        if timed_out(&err) {
            call.expired("send");
        }
        return Outcome::Cold;
    }
    let mut wrote = false;
    let stdout = io::stdout();
    let stderr = io::stderr();
    loop {
        let mut head = [0u8; 5];
        if let Err(err) = stream.read_exact(&mut head) {
            return reply_lost(call, &err, wrote);
        }
        let len = u32::from_be_bytes([head[1], head[2], head[3], head[4]]) as usize;
        let mut body = vec![0u8; len];
        if let Err(err) = stream.read_exact(&mut body) {
            return reply_lost(call, &err, wrote);
        }
        match head[0] {
            b'o' => {
                let _ = stdout.lock().write_all(&body);
                wrote = true;
            }
            b'e' => {
                let _ = stderr.lock().write_all(&body);
                wrote = true;
            }
            b'x' => {
                let _ = stdout.lock().flush();
                return Outcome::Done(body.first().copied().unwrap_or(0) as i32);
            }
            b'r' if !wrote => return Outcome::Cold,
            _ => return if wrote { Outcome::Broken } else { Outcome::Cold },
        }
    }
}

/// Start hook-server.py beside the dispatcher, detached, unless one was started recently.
fn spawn_server(python: &OsString, dispatcher: &OsString) {
    let stamp = cache_dir().join("hook-server.spawn");
    let recent = fs::metadata(&stamp)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| SystemTime::now().duration_since(t).ok())
        .map_or(false, |age| age < Duration::from_secs(SPAWN_GAP_SECS));
    if recent {
        return;
    }
    let _ = fs::create_dir_all(cache_dir());
    if fs::write(&stamp, b"").is_err() {
        return;
    }
    let server = Path::new(dispatcher).with_file_name("hook-server.py");
    let _ = Command::new(python)
        .arg(server)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .process_group(0)
        .spawn();
}

fn run_cold(
    python: &OsString,
    dispatcher: &OsString,
    args: &[OsString],
    payload: Vec<u8>,
    call: &Call,
) -> i32 {
    let mut child = match Command::new(python)
        .arg(dispatcher)
        .args(args)
        .stdin(Stdio::piped())
        .spawn()
    {
        Ok(child) => child,
        Err(err) => {
            eprintln!("hook-client: cannot start {:?}: {}", python, err);
            return CANNOT_RUN;
        }
    };
    if let Some(mut stdin) = child.stdin.take() {
        // On its own thread: a child that never reads would hold a write of a payload
        // larger than the pipe here, past the limit below.
        thread::spawn(move || {
            let _ = stdin.write_all(&payload);
        });
    }
    if !call.bounded {
        return match child.wait() {
            Ok(status) => status.code().unwrap_or(CANNOT_RUN),
            Err(_) => CANNOT_RUN,
        };
    }
    let limit = Duration::from_secs(COLD_SECS);
    let began = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => return status.code().unwrap_or(CANNOT_RUN),
            Ok(None) if began.elapsed() < limit => thread::sleep(Duration::from_millis(5)),
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                call.expired("cold");
                eprintln!(
                    "hook-client: the cold {} run passed {} s and was killed",
                    call.event.to_string_lossy(),
                    COLD_SECS
                );
                return CANNOT_RUN;
            }
            Err(_) => return CANNOT_RUN,
        }
    }
}

fn main() {
    let started = Instant::now();
    let argv: Vec<OsString> = env::args_os().collect();
    if argv.len() < 4 {
        eprintln!("usage: hook-client <python> <hook-dispatch.py> <Event> [args...]");
        exit(CANNOT_RUN);
    }
    let (python, dispatcher, args) = (&argv[1], &argv[2], &argv[3..]);
    let call = Call {
        event: &args[0],
        started,
        bounded: args[0] == *BOUNDED_EVENT,
    };
    let mut payload = Vec::new();
    let _ = io::stdin().read_to_end(&mut payload);
    let sock = socket_path();
    match serve(&sock, &request(python, dispatcher, args), &payload, &call) {
        Outcome::Done(code) => exit(code),
        Outcome::Broken => exit(0),
        Outcome::Cold => {
            if env::var_os("HOOK_SERVER_SPAWN").map_or(true, |v| v != "0") {
                spawn_server(python, dispatcher);
            }
            exit(run_cold(python, dispatcher, args, payload, &call));
        }
    }
}
