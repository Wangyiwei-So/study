use anyhow::{Context, Result, bail};
use aya::{
    Ebpf,
    maps::PerCpuHashMap,
    programs::{UProbe, uprobe::UProbeScope},
};
use std::{
    env,
    io::{self, Write},
    time::Instant,
};

#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    let args: Vec<_> = env::args().collect();
    if args.len() != 3 {
        bail!("usage: aya-bench <BPF ELF> <target>");
    }
    let target = std::fs::canonicalize(&args[2])?;
    let load_start = Instant::now();
    let mut bpf = Ebpf::load_file(&args[1]).context("load benchmark BPF object")?;
    let probe: &mut UProbe = bpf
        .program_mut("bench_count")
        .context("bench_count missing")?
        .try_into()?;
    probe.load()?;
    let load_ns = load_start.elapsed().as_nanos();
    let attach_start = Instant::now();
    probe.attach("bench_add", target, UProbeScope::AllProcesses)?;
    let attach_ns = attach_start.elapsed().as_nanos();
    println!("AYA_TIMING {{\"load_ns\":{load_ns},\"attach_ns\":{attach_ns}}}");
    println!("READY");
    io::stdout().flush()?;
    tokio::signal::ctrl_c().await?;
    let hits = PerCpuHashMap::<_, u64, u64>::try_from(bpf.map("HITS").context("HITS missing")?)?;
    let total: u64 = hits.get(&0, 0)?.iter().sum();
    println!("HITS {total}");
    Ok(())
}
