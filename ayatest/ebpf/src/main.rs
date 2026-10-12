// eBPF 在内核中执行，没有普通进程的标准库和 main 启动流程。
#![no_std]
#![no_main]

use aya_ebpf::{helpers::bpf_get_current_pid_tgid, macros::uprobe, programs::ProbeContext};
use aya_log_ebpf::info;

// 宏把函数放进 ELF 的 uprobe section，供用户态 Aya 识别并加载。
// probe_add 是探针名；要观测哪个可执行文件、哪个 C 函数，由用户态 attach 决定。
#[uprobe]
pub fn probe_add(ctx: ProbeContext) -> u32 {
    // 每次目标进程进入 add，内核执行一次本函数；这里不能 sleep 或无限等待。
    // 错误路径也直接结束探针，不使用 unwrap，避免引入 panic。
    let _ = try_probe_add(ctx);

    // 这个 0 是探针返回给内核的值，不是 C 函数 add 的返回值。
    // 本例只观察参数，不改变 add 的执行结果。
    0
}

fn try_probe_add(ctx: ProbeContext) -> Result<(), ()> {
    // ctx 包含触发时保存的寄存器状态。arg 的下标从 0 开始。
    // 本例的 C int 是 32 位，因此使用 i32；类型必须匹配目标函数的 ABI。
    // x86_64 Linux 的前两个整数参数通常在 RDI/RSI 中，Aya 封装了寄存器读取。
    let a: i32 = ctx.arg(0).ok_or(())?;
    let b: i32 = ctx.arg(1).ok_or(())?;

    // helper 返回：高 32 位是 TGID（通常说的进程 PID），低 32 位是线程 ID。
    let pid_tgid = bpf_get_current_pid_tgid();
    let pid = (pid_tgid >> 32) as u32;
    let tid = pid_tgid as u32;

    // no_std 环境不能 println!；此宏将记录写入 Map，用户态日志桥再打印。
    info!(&ctx, "pid={} tid={} add(a={}, b={})", pid, tid, a, b);

    // 此时 add 还没有执行完，因此这里不能取得它的返回值。
    // 学习返回值观测时，应再添加 #[uretprobe] 和 RetProbeContext。
    Ok(())
}

// no_std 二进制必须提供 panic handler。本例通过显式处理 Option 避免走到这里。
#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {}
}

// 向内核声明许可证，允许使用要求 GPL 兼容许可证的 eBPF helper。
#[unsafe(link_section = "license")]
#[unsafe(no_mangle)]
pub static LICENSE: [u8; 13] = *b"Dual MIT/GPL\0";
