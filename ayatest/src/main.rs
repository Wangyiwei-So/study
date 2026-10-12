use std::{env, num::NonZeroU32, path::PathBuf};

use anyhow::{Context, Result, bail};
use aya::{
    Ebpf,
    programs::{UProbe, uprobe::UProbeScope},
};
use aya_log::EbpfLogger;
use tokio::io::{Interest, unix::AsyncFd};

// 这是运行在用户态的普通 Rust 程序，可以使用 std、文件系统和异步运行时。
// 真正的探针代码在 ebpf/src/main.rs；两者编译成不同架构的 ELF 文件。
#[tokio::main(flavor = "current_thread")]
async fn main() -> Result<()> {
    // 内核里的 info! 会经由 aya-log 转发给这里的日志输出器。
    // 默认显示 info；也可以通过 RUST_LOG 环境变量调整日志级别。
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args: Vec<String> = env::args().collect();
    if !(3..=4).contains(&args.len()) {
        bail!("用法: {} <eBPF ELF 路径> <C 可执行文件路径> [PID]", args[0]);
    }
    let ebpf_path = PathBuf::from(&args[1]);
    let target = std::fs::canonicalize(&args[2]).context("找不到 C 可执行文件，请先编译")?;

    // 不传 PID：监控所有执行这个文件中 add 函数的进程。
    // 传入 PID：只监控指定进程。这里拒绝 0，避免混淆其特殊含义。
    let scope = match args.get(3) {
        Some(pid) => {
            UProbeScope::OneProcess(pid.parse::<NonZeroU32>().context("PID 必须是正整数")?)
        }
        None => UProbeScope::AllProcesses,
    };

    // 1. 解析 eBPF ELF、创建 Map 并做重定位；此时探针尚未挂载。
    // 运行时读取 ELF 可以把两个编译步骤分开，便于理解和单独修改。
    let mut bpf = Ebpf::load_file(&ebpf_path).context("读取 eBPF ELF 或创建 Map 失败")?;

    // 2. 接收内核日志。Aya 0.3 的日志桥需要主动 flush，init 本身不会启动读取任务。
    // AsyncFd 等待日志 Map 可读，避免持续轮询占用 CPU。
    let logger = EbpfLogger::init(&mut bpf).context("初始化 eBPF 日志失败")?;
    let mut logger = AsyncFd::with_interest(logger, Interest::READABLE)?;

    // 3. probe_add 是 Rust 探针的名字，不是被观测的 C 函数名。
    // try_into 将通用 Program 转换成 UProbe，确保使用正确的加载/挂载接口。
    let program: &mut UProbe = bpf
        .program_mut("probe_add")
        .context("ELF 中没有 probe_add 程序")?
        .try_into()?;
    // load 将字节码提交给内核，内核 verifier 会在这里检查程序的安全性。
    program.load().context("加载 uprobe 失败")?;

    // 4. add 才是 C 文件里的目标符号。Aya 从 ELF 符号表解析其文件偏移，
    // 因此无需手工计算进程中的地址，也无需为了本例关闭 ASLR。
    // 注意：Aya 0.14 使用这三个参数，与旧版 attach 的参数形式不同。
    program
        .attach("add", &target, scope)
        .context("挂载 add 失败，请检查符号、PID 和权限")?;
    println!(
        "已挂载 {}:add，范围 {scope:?}；按 Ctrl+C 退出。",
        target.display()
    );

    // 5. 持续处理日志，直到 Ctrl+C。必须保持 bpf 存活，否则链接会被释放。
    let shutdown = tokio::signal::ctrl_c();
    tokio::pin!(shutdown);
    loop {
        tokio::select! {
            ready = logger.readable_mut() => {
                let mut guard = ready.context("等待 eBPF 日志失败")?;
                guard.get_inner_mut().flush();
                guard.clear_ready();
            }
            result = &mut shutdown => {
                result.context("等待 Ctrl+C 失败")?;
                break;
            }
        }
    }
    // 无需修改 C 程序来移除探针；释放 Ebpf 会自动解除本程序持有的挂载。
    drop(bpf);
    Ok(())
}
