use anyhow::{ensure, Context, Result};
use dam::channel::{ChannelElement, EnqueueStatus, PeekResult};
use dam::simulation::{InitializationOptionsBuilder, ProgramBuilder, RunOptions};
use dam::structures::Time;
use dam::utility_contexts::FunctionContext;
use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const MAGIC: u32 = 0x44414d33;
// Width is per channel; DAM's dynamic type contract reports the exact bit count.
#[derive(Clone, Debug, Default, PartialEq)]
struct Bits {
    width: u32,
    words: Vec<u32>,
}
impl dam::types::DAMType for Bits {
    fn dam_size(&self) -> usize {
        self.width as usize
    }
}
#[derive(Clone, Deserialize)]
struct Partition {
    id: usize,
    rules: Vec<String>,
    #[serde(default)]
    skip_safe: bool,
    #[serde(default)]
    batch_safe: bool,
    #[serde(default)]
    unused_memories: Vec<String>,
    #[serde(default)]
    required_inputs: Vec<Vec<String>>,
}
#[derive(Clone, Deserialize)]
struct Channel {
    name: String,
    depth: usize,
    width: u32,
    producer: usize,
    consumer: usize,
}
#[derive(Clone, Deserialize)]
struct Manifest {
    schema: u32,
    #[serde(default)]
    top: String,
    protocol_version: u32,
    partitions: Vec<Partition>,
    channels: Vec<Channel>,
}
#[derive(Serialize)]
struct Stats {
    partition: usize,
    pid: u32,
    rules: Vec<String>,
    cycles: u64,
    sent: u64,
    received: u64,
    full_cycles: u64,
    pending_polls: u64,
    executed_cycles: u64,
    skipped_clock_cycles: u64,
    progress_advances: u64,
    wire_bytes: u64,
    startup_wall_seconds: f64,
    exchanges: u64,
    batched_clock_cycles: u64,
}
#[derive(Serialize)]
struct Report {
    passed: bool,
    workers: Vec<Stats>,
    events: Vec<(u64, String, String, Vec<u32>)>,
}

struct Worker {
    child: Child,
    socket: UnixStream,
    path: PathBuf,
    can_skip: bool,
    fired: bool,
    skipped: u64,
    next_cycle: u64,
    sparse: bool,
    batched: bool,
    request: Vec<u8>,
    response: Vec<u8>,
    wire_bytes: u64,
    startup_wall_seconds: f64,
}
impl Worker {
    fn spawn(
        executable: &Path,
        arguments: &[String],
        output: &Path,
        id: usize,
        names: &str,
        cycles: u64,
        sparse: bool,
        batched: bool,
        unused_memories: &str,
    ) -> Result<Self> {
        let timeout_seconds = std::env::var("DAM_WORKER_TIMEOUT_SECONDS")
            .unwrap_or_else(|_| "20".into())
            .parse::<u64>()
            .context("invalid DAM_WORKER_TIMEOUT_SECONDS")?;
        ensure!(timeout_seconds > 0, "worker timeout must be positive");
        let timeout = Duration::from_secs(timeout_seconds);
        let path = PathBuf::from(format!("/tmp/bsc-dam-{}-{id}.sock", std::process::id()));
        let listener = UnixListener::bind(&path)?;
        listener.set_nonblocking(true)?;
        let log = File::create(output.join(format!("worker-{id}.log")))?;
        let launch_start = Instant::now();
        let mut child = Command::new(executable)
            .args(arguments)
            .env(
                "DAM_TRANSPORT",
                if batched {
                    "batched"
                } else if sparse {
                    "sparse"
                } else {
                    "dense"
                },
            )
            .env("DAM_SOCKET", &path)
            .env("DAM_CHANNELS", names)
            .env("DAM_CYCLES", cycles.to_string())
            .env("DAM_UNUSED_MEMORIES", unused_memories)
            .env(
                "DAM_TRACE",
                if std::env::var("DAM_NO_TRACE").as_deref() == Ok("1") {
                    PathBuf::from("/dev/null")
                } else {
                    output.join(format!("worker-{id}.trace"))
                },
            )
            .env(
                "DAM_STATE_TRACE",
                if std::env::var("DAM_CHECK_STATE").as_deref() == Ok("1") {
                    output
                        .join(format!("worker-{id}.state"))
                        .to_string_lossy()
                        .into_owned()
                } else {
                    String::new()
                },
            )
            .stdin(Stdio::null())
            .stdout(log.try_clone()?)
            .stderr(log)
            .spawn()?;
        let start = Instant::now();
        let socket = loop {
            match listener.accept() {
                Ok((s, _)) => break s,
                Err(e) if e.kind() == std::io::ErrorKind::WouldBlock => {}
                Err(e) => {
                    let _ = child.kill();
                    let _ = child.wait();
                    let _ = fs::remove_file(&path);
                    return Err(e.into());
                }
            }
            if start.elapsed() > timeout || child.try_wait()?.is_some() {
                let _ = child.kill();
                let _ = child.wait();
                let _ = fs::remove_file(&path);
                anyhow::bail!("worker {id} failed during startup; inspect its log");
            }
            std::thread::sleep(Duration::from_millis(5));
        };
        socket.set_nonblocking(false)?;
        socket.set_read_timeout(Some(timeout))?;
        socket.set_write_timeout(Some(timeout))?;
        Ok(Self {
            child,
            socket,
            path,
            can_skip: false,
            fired: true,
            skipped: 0,
            next_cycle: 0,
            sparse,
            batched,
            request: Vec::new(),
            response: Vec::new(),
            wire_bytes: 0,
            startup_wall_seconds: launch_start.elapsed().as_secs_f64(),
        })
    }
    fn step(
        &mut self,
        cycle: u64,
        until: u64,
        inputs: &[(bool, Option<Vec<u32>>)],
        channels: &[Channel],
    ) -> Result<Vec<(bool, Vec<u32>, bool)>> {
        if self.sparse {
            return self.step_sparse(cycle, until, inputs, channels);
        }
        let mut request = vec![MAGIC, cycle as u32, inputs.len() as u32];
        for ((ready, value), c) in inputs.iter().zip(channels) {
            let n = (c.width as usize + 31) / 32;
            request.extend([c.width, *ready as u32, value.is_some() as u32]);
            match value {
                Some(v) => {
                    ensure!(v.len() == n, "payload length mismatch");
                    request.extend(v);
                }
                None => request.extend(std::iter::repeat(0).take(n)),
            }
        }
        let bytes: Vec<u8> = request.iter().flat_map(|v| v.to_le_bytes()).collect();
        self.wire_bytes += bytes.len() as u64;
        self.socket.write_all(&bytes)?;
        let mut bytes = vec![
            0u8;
            4 * (6 + channels
                .iter()
                .map(|c| 3 + (c.width as usize + 31) / 32)
                .sum::<usize>())
        ];
        self.socket
            .read_exact(&mut bytes)
            .context("reading Bluesim response")?;
        self.wire_bytes += bytes.len() as u64;
        let words: Vec<u32> = bytes
            .chunks_exact(4)
            .map(|b| u32::from_le_bytes(b.try_into().unwrap()))
            .collect();
        ensure!(
            words[0..3] == [MAGIC, cycle as u32, inputs.len() as u32],
            "cycle/protocol mismatch"
        );
        ensure!(words[3] <= 1 && words[5] <= 1, "bad worker status");
        self.can_skip = words[3] != 0;
        self.skipped = words[4] as u64;
        self.fired = words[5] != 0;
        ensure!(
            cycle >= self.next_cycle && self.skipped == cycle - self.next_cycle,
            "worker failed to acknowledge actual kernel skip"
        );
        self.next_cycle = cycle + 1;
        let mut offset = 6;
        let mut reply = Vec::new();
        for c in channels {
            let n = (c.width as usize + 31) / 32;
            let w = &words[offset..offset + 3 + n];
            ensure!(
                w[0] == c.width && w[1] <= 1 && w[2] <= 1,
                "invalid response geometry/mask"
            );
            let value = w[3..].to_vec();
            if c.width % 32 != 0 {
                ensure!(
                    value.last().unwrap() >> (c.width % 32) == 0,
                    "noncanonical payload"
                );
            }
            reply.push((w[1] != 0, value, w[2] != 0));
            offset += 3 + n;
        }
        Ok(reply)
    }
    // Version 4: worker-local geometry, length-delimited frames and no invalid
    // payloads. Reuse byte buffers; all framing bounds derive from the manifest.
    fn step_sparse(
        &mut self,
        cycle: u64,
        until: u64,
        inputs: &[(bool, Option<Vec<u32>>)],
        channels: &[Channel],
    ) -> Result<Vec<(bool, Vec<u32>, bool)>> {
        fn put(bytes: &mut Vec<u8>, word: u32) {
            bytes.extend_from_slice(&word.to_le_bytes());
        }
        fn get(bytes: &[u8], offset: &mut usize) -> Result<u32> {
            ensure!(*offset + 4 <= bytes.len(), "truncated response field");
            let value = u32::from_le_bytes(bytes[*offset..*offset + 4].try_into().unwrap());
            *offset += 4;
            Ok(value)
        }
        self.request.clear();
        let magic = MAGIC + if self.batched { 2 } else { 1 };
        for w in [0, magic, cycle as u32, channels.len() as u32] {
            put(&mut self.request, w);
        }
        if self.batched {
            put(&mut self.request, until as u32);
        }
        for ((ready, value), c) in inputs.iter().zip(channels) {
            for w in [c.width, *ready as u32, value.is_some() as u32] {
                put(&mut self.request, w);
            }
            if let Some(v) = value {
                ensure!(
                    v.len() == (c.width as usize + 31) / 32,
                    "payload length mismatch"
                );
                for &w in v {
                    put(&mut self.request, w);
                }
            }
        }
        let length = u32::try_from(self.request.len() - 4).context("request too large")?;
        self.request[..4].copy_from_slice(&length.to_le_bytes());
        self.socket.write_all(&self.request)?;
        let mut prefix = [0; 4];
        self.socket
            .read_exact(&mut prefix)
            .context("reading response length")?;
        let length = u32::from_le_bytes(prefix) as usize;
        let min = 4 * (6 + 3 * channels.len());
        let max = min
            + channels
                .iter()
                .map(|c| 4 * ((c.width as usize + 31) / 32))
                .sum::<usize>();
        ensure!(
            length >= min && length <= max && length % 4 == 0,
            "invalid response length"
        );
        self.response.resize(length, 0);
        self.socket
            .read_exact(&mut self.response)
            .context("reading Bluesim response")?;
        self.wire_bytes += (self.request.len() + 4 + length) as u64;
        let mut offset = 0;
        let b = &self.response;
        ensure!(get(b, &mut offset)? == magic, "protocol mismatch");
        let last = get(b, &mut offset)? as u64;
        ensure!(
            last >= cycle
                && last <= until
                && (self.batched || last == cycle)
                && get(b, &mut offset)? == channels.len() as u32,
            "cycle/protocol mismatch"
        );
        let can_skip = get(b, &mut offset)?;
        self.skipped = get(b, &mut offset)? as u64;
        let fired = get(b, &mut offset)?;
        ensure!(can_skip <= 1 && fired <= 1, "bad worker status");
        self.can_skip = can_skip != 0;
        self.fired = fired != 0;
        ensure!(
            cycle >= self.next_cycle && self.skipped == cycle - self.next_cycle,
            "worker failed to acknowledge actual kernel skip"
        );
        self.next_cycle = last + 1;
        let mut reply = Vec::with_capacity(channels.len());
        for c in channels {
            ensure!(get(b, &mut offset)? == c.width, "payload width mismatch");
            let enq = get(b, &mut offset)?;
            let deq = get(b, &mut offset)?;
            ensure!(enq <= 1 && deq <= 1, "invalid response handshake");
            let mut value = Vec::new();
            if enq != 0 {
                let n = (c.width as usize + 31) / 32;
                value.reserve(n);
                for _ in 0..n {
                    value.push(get(b, &mut offset)?);
                }
                if c.width % 32 != 0 {
                    ensure!(
                        value.last().unwrap() >> (c.width % 32) == 0,
                        "noncanonical payload"
                    );
                }
            }
            reply.push((enq != 0, value, deq != 0));
        }
        ensure!(offset == length, "response frame length mismatch");
        Ok(reply)
    }
}
impl Drop for Worker {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = fs::remove_file(&self.path);
    }
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    ensure!(
        args.len() == 5,
        "usage: runtime MANIFEST MODELS RESULTS CYCLES"
    );
    let manifest: Manifest = serde_json::from_slice(&fs::read(&args[1])?)?;
    ensure!(manifest.schema == 3, "unsupported manifest schema; rebuild with the annotation-only compiler");
    ensure!(manifest.protocol_version == 5, "unsupported transport protocol");
    // Compiler-validated explicit-context bundles use protocol v5.
    let transport = std::env::var("DAM_TRANSPORT");
    let batched = match transport.as_deref() {
        Ok("batched") => true,
        Err(_) => manifest.protocol_version == 5,
        _ => false,
    };
    ensure!(
        !batched || manifest.protocol_version == 5,
        "clock batching requires a compiler v5 bundle; relink this design first"
    );
    let batch_limit = std::env::var("DAM_BATCH_CYCLES")
        .unwrap_or_else(|_| "4096".into())
        .parse::<u64>()
        .context("invalid DAM_BATCH_CYCLES")?;
    ensure!(
        batch_limit > 0 && batch_limit <= u32::MAX as u64,
        "invalid batch limit"
    );
    let sparse = match transport.as_deref() {
        Ok("dense") => false,
        Ok("sparse") | Ok("batched") => true,
        Err(_) => match manifest.protocol_version {
            3 => false,
            4 | 5 => true,
            _ => anyhow::bail!("unsupported transport protocol"),
        },
        _ => anyhow::bail!("DAM_TRANSPORT must be dense, sparse or batched"),
    };
    let models = fs::canonicalize(&args[2])?;
    // Native compiler bundles have a known generated-library naming scheme.
    // Keep Tcl as the default for old bundles and interactive compatibility.
    let headless = std::env::var("DAM_HEADLESS").as_deref() == Ok("1");
    let loader = std::env::current_exe()?.with_file_name("bluesim-headless");
    if headless {
        ensure!(
            !manifest.top.is_empty() && loader.is_file(),
            "headless mode requires a top name and installed bluesim-headless loader"
        );
        for p in &manifest.partitions {
            ensure!(
                models
                    .join(format!("partition-{}-model.so", p.id))
                    .is_file(),
                "headless mode requires a native BSC bundle with generated model libraries"
            );
        }
    }
    fs::create_dir_all(&args[3])?;
    let results = fs::canonicalize(&args[3])?;
    let cycles: u64 = args[4].parse()?;
    ensure!(cycles > 0 && cycles <= u32::MAX as u64, "invalid cycles");
    let mut program = ProgramBuilder::default();
    let mut contexts: Vec<_> = manifest
        .partitions
        .iter()
        .map(|_| FunctionContext::new())
        .collect();
    let count = contexts.len();
    let mut outgoing: Vec<Vec<_>> = (0..count).map(|_| Vec::new()).collect();
    let mut incoming: Vec<Vec<_>> = (0..count).map(|_| Vec::new()).collect();
    for (index, c) in manifest.channels.iter().enumerate() {
        ensure!(
            c.producer < count && c.consumer < count && c.depth > 0,
            "invalid compiler manifest"
        );
        let (tx, rx) = program.bounded::<Bits>(c.depth);
        tx.attach_sender(&contexts[c.producer]);
        rx.attach_receiver(&contexts[c.consumer]);
        outgoing[c.producer].push((index, tx));
        incoming[c.consumer].push((index, rx));
    }
    let stats = Arc::new(Mutex::new(Vec::new()));
    let events = Arc::new(Mutex::new(Vec::new()));
    // Workers can reach the finite end at different host times. Retain their
    // endpoints until the whole simulation ends so a final-cycle enqueue is
    // not spuriously rejected just because its peer process finished first.
    let completed_endpoints = Arc::new(Mutex::new(Vec::new()));
    let jitter = std::env::var("DAM_HOST_JITTER").is_ok();
    let skip_clocks = std::env::var("DAM_SKIP_CLOCKS").as_deref() == Ok("1");
    let trace_events = std::env::var("DAM_NO_TRACE").as_deref() != Ok("1");
    for (id, mut context) in contexts.drain(..).enumerate() {
        let mut sends = std::mem::take(&mut outgoing[id]);
        let mut receives = std::mem::take(&mut incoming[id]);
        let indices: Vec<_> = manifest
            .channels
            .iter()
            .enumerate()
            .filter(|(_, c)| !sparse || c.producer == id || c.consumer == id)
            .map(|(i, _)| i)
            .collect();
        for (i, _) in sends.iter_mut() {
            *i = indices.iter().position(|g| g == i).unwrap();
        }
        for (i, _) in receives.iter_mut() {
            *i = indices.iter().position(|g| g == i).unwrap();
        }
        let channels: Vec<_> = indices
            .iter()
            .map(|i| manifest.channels[*i].clone())
            .collect();
        let names = channels
            .iter()
            .map(|c| format!("{}:{}", c.name, c.width))
            .collect::<Vec<_>>()
            .join(",");
        let partition = manifest.partitions[id].clone();
        ensure!(partition.id == id, "non-contiguous partition IDs");
        let (executable, arguments) = if headless {
            (
                loader.clone(),
                vec![
                    models
                        .join(format!("partition-{id}-model.so"))
                        .to_string_lossy()
                        .into_owned(),
                    manifest.top.clone(),
                    "0".into(),
                ],
            )
        } else {
            (models.join(format!("worker-{id}")), Vec::new())
        };
        let output = results.clone();
        let stats_out = stats.clone();
        let events_out = events.clone();
        let endpoints_out = completed_endpoints.clone();
        context.set_run(move |time| {
            let mut worker = Worker::spawn(
                &executable,
                &arguments,
                &output,
                id,
                &names,
                cycles,
                sparse,
                batched,
                &partition.unused_memories.join(","),
            )
            .unwrap();
            let mut record = Stats {
                partition: id,
                pid: worker.child.id(),
                rules: partition.rules.clone(),
                cycles,
                sent: 0,
                received: 0,
                full_cycles: 0,
                pending_polls: 0,
                executed_cycles: 0,
                skipped_clock_cycles: 0,
                progress_advances: 0,
                wire_bytes: 0,
                startup_wall_seconds: worker.startup_wall_seconds,
                exchanges: 0,
                batched_clock_cycles: 0,
            };
            let mut log = Vec::new();
            let required: Vec<Vec<usize>> = partition
                .required_inputs
                .iter()
                .map(|rs| {
                    rs.iter()
                        .map(|name| {
                            channels
                                .iter()
                                .position(|c| &c.name == name && c.consumer == id)
                                .expect("required input is not owned by worker")
                        })
                        .collect()
                })
                .collect();
            let mut previous_frame = None;
            let mut cycle = 0;
            while cycle < cycles {
                if jitter && (cycle + id as u64) % 7 == 0 {
                    std::thread::sleep(Duration::from_micros(100 * (id as u64 + 1)));
                }
                let mut frame = vec![(false, None); channels.len()];
                let mut earliest = vec![cycle; channels.len()];
                for (index, rx) in &receives {
                    match rx.peek() {
                        PeekResult::Something(e) => {
                            earliest[*index] = e.time.time().max(cycle).min(cycles - 1);
                            if e.time <= time.tick() {
                                frame[*index].1 = Some(e.data.words);
                            }
                        }
                        PeekResult::Nothing(t) => {
                            earliest[*index] = if t.is_infinite() {
                                cycles - 1
                            } else {
                                t.time().saturating_add(1).max(cycle).min(cycles - 1)
                            };
                        }
                        PeekResult::Closed => earliest[*index] = cycles - 1,
                    };
                }
                for (index, tx) in &sends {
                    loop {
                        match tx.enqueue_status(time) {
                            EnqueueStatus::Ready => {
                                frame[*index].0 = true;
                                break;
                            }
                            EnqueueStatus::Full => {
                                record.full_cycles += 1;
                                break;
                            }
                            EnqueueStatus::Pending => {
                                record.pending_polls += 1;
                                std::thread::yield_now()
                            }
                            EnqueueStatus::Closed => break,
                        }
                    }
                }
                if skip_clocks && partition.skip_safe && worker.can_skip && cycle >= 2 {
                    // Each required set is a conjunction; distinct rules are
                    // alternatives. Empty sets explicitly prevent this proof.
                    let input_bound = if required.len() == partition.rules.len() {
                        required
                            .iter()
                            .map(|rs| rs.iter().map(|i| earliest[*i]).max().unwrap_or(cycle))
                            .min()
                            .unwrap_or(cycle)
                    } else {
                        cycle
                    };
                    // A no-fire fixed point persists until a boundary changes.
                    // Ready outputs stay ready while this worker does not send;
                    // full outputs might receive credits, so conservatively stop.
                    let stable_bound = if !worker.fired
                        && previous_frame.as_ref() == Some(&frame)
                        && sends.iter().all(|(i, _)| frame[*i].0)
                    {
                        receives
                            .iter()
                            .filter(|(i, _)| frame[*i].1.is_none())
                            .map(|(i, _)| earliest[*i])
                            .min()
                            .unwrap_or(cycles - 1)
                    } else {
                        cycle
                    };
                    let target = input_bound.max(stable_bound).min(cycles - 1);
                    if target > cycle {
                        time.advance(Time::new(target));
                        cycle = target;
                        record.progress_advances += 1;
                        continue;
                    }
                }
                // Empty inputs cannot become valid before their DAM-proven
                // timestamp. Present heads remain unchanged until a dequeue.
                // A ready output remains ready until this producer enqueues;
                // a full output can receive credits at any following cycle.
                // The native bridge stops at the FIRST boundary transfer, so
                // all earlier clocks execute with the exact same snapshot.
                let until =
                    if batched && partition.batch_safe && sends.iter().all(|(i, _)| frame[*i].0) {
                        receives
                            .iter()
                            .filter(|(i, _)| frame[*i].1.is_none())
                            .map(|(i, _)| earliest[*i].saturating_sub(1).max(cycle))
                            .min()
                            .unwrap_or(cycles - 1)
                            .min(cycle.saturating_add(batch_limit - 1))
                            .min(cycles - 1)
                    } else {
                        cycle
                    };
                let start_cycle = cycle;
                let reply = worker.step(cycle, until, &frame, &channels).unwrap();
                cycle = worker.next_cycle - 1;
                if cycle > start_cycle {
                    time.advance(Time::new(cycle));
                }
                record.exchanges += 1;
                record.executed_cycles += cycle - start_cycle + 1;
                record.batched_clock_cycles += cycle - start_cycle;
                record.skipped_clock_cycles += worker.skipped;
                if skip_clocks && partition.skip_safe {
                    previous_frame = Some(frame.clone());
                }
                for (i, (enq, value, deq)) in reply.iter().enumerate() {
                    assert!(!enq || (channels[i].producer == id && frame[i].0));
                    assert!(!deq || (channels[i].consumer == id && frame[i].1.is_some()));
                    if *enq {
                        let tx = &sends.iter().find(|(j, _)| *j == i).unwrap().1;
                        tx.enqueue(
                            time,
                            ChannelElement::new(
                                time.tick() + 1,
                                Bits {
                                    width: channels[i].width,
                                    words: value.clone(),
                                },
                            ),
                        )
                        .unwrap();
                        record.sent += 1;
                        if trace_events {
                            log.push((
                                cycle,
                                channels[i].name.clone(),
                                "enq".to_string(),
                                value.clone(),
                            ));
                        }
                    }
                    if *deq {
                        let rx = &receives.iter().find(|(j, _)| *j == i).unwrap().1;
                        let e = rx.dequeue(time).unwrap();
                        assert_eq!(Some(&e.data.words), frame[i].1.as_ref());
                        record.received += 1;
                        if trace_events {
                            log.push((
                                cycle,
                                channels[i].name.clone(),
                                "deq".to_string(),
                                e.data.words,
                            ));
                        }
                    }
                }
                assert_eq!(
                    time.tick().time(),
                    cycle,
                    "channel operation skipped a hardware cycle"
                );
                time.incr_cycles(1);
                cycle += 1;
            }
            assert_eq!(record.executed_cycles + record.skipped_clock_cycles, cycles);
            assert!(worker.child.wait().unwrap().success());
            record.wire_bytes = worker.wire_bytes;
            stats_out.lock().unwrap().push(record);
            events_out.lock().unwrap().extend(log);
            endpoints_out.lock().unwrap().push((sends, receives));
        });
        program.add_child(context);
    }
    let executed = program
        .initialize(
            InitializationOptionsBuilder::default()
                .run_flavor_inference(false)
                .build()?,
        )?
        .run(RunOptions::default());
    ensure!(executed.passed(), "DAM execution failed");
    let mut workers = Arc::try_unwrap(stats).ok().unwrap().into_inner().unwrap();
    workers.sort_by_key(|s| s.partition);
    let mut events = Arc::try_unwrap(events).ok().unwrap().into_inner().unwrap();
    events.sort();
    let report = Report {
        passed: true,
        workers,
        events,
    };
    fs::write(
        results.join("dam-report.json"),
        serde_json::to_vec_pretty(&report)?,
    )?;
    println!(
        "DAM ran {} separate Bluesim worker processes for {} cycles",
        count, cycles
    );
    Ok(())
}
