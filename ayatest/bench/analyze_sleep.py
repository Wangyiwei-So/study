#!/usr/bin/env python3
"""Audit a completed sleep benchmark and generate Markdown/CSV/PNG/PDF artifacts.

Usage: bench/.analysis-venv/bin/python bench/analyze_sleep.py <results-directory>
Requires numpy and matplotlib only for this report generator, not the runner.
"""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import statistics

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

MODES = ["baseline", "aya", "bpftrace", "bpftime"]
COLORS = ["#64748b", "#2563eb", "#d97706", "#059669"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    args = parser.parse_args()
    source = args.results.resolve()
    env = json.loads((source / "environment.json").read_text())
    config = env["arguments"]
    rows = json.loads((source / "raw.json").read_text())
    valid = [r for r in rows if r["status"] == "ok"]
    failed = [r for r in rows if r["status"] != "ok"]
    sleeps = [int(x) for x in config["sleep_ns"].split(",")]
    rounds = config["rounds"]
    assert len(valid) == len(sleeps) * len(MODES) * rounds, "incomplete experiment"
    groups = {}
    warnings = 0
    for delay in sleeps:
        for mode in MODES:
            group = sorted([r for r in valid if r["sleep_ns"] == delay and r["mode"] == mode], key=lambda r:r["round"])
            assert [r["round"] for r in group] == list(range(1, rounds + 1))
            groups[delay, mode] = group
    for r in valid:
        n, warm = r["iterations"], r["warmup"]
        assert r["checksum"] == (n*(n+1)//2 + warm*(warm+1)//2) % 2**64
        assert r["elapsed_ns"] > 0
        stem = source / ("sleep-%dns" % r["sleep_ns"])
        if r.get("attempt", 1) > 1:
            stem /= "%02d-%s-retry%d" % (r["round"], r["mode"], r["attempt"])
        stem /= "%02d-%s" % (r["round"], r["mode"])
        target = stem.with_suffix(".target.log").read_text()
        measured = json.loads(re.search(r"^BENCH (.*)$", target, re.M)[1])
        for k in ("iterations", "warmup", "sleep_ns", "elapsed_ns", "checksum"):
            assert measured[k] == r[k]
        if r["mode"] != "baseline":
            tracer = stem.with_suffix(".tracer.log").read_text()
            pattern = r"^HITS (\d+)$" if r["mode"] == "aya" else r"^@hits\[0\]:\s*(\d+)\s*$"
            assert int(re.search(pattern, tracer, re.M)[1]) == r["hits"] == n + warm
            warnings += int("Error closing perf event" in tracer)
        if r["mode"] == "bpftime":
            handlers=json.loads(stem.with_suffix(".handlers.json").read_text())
            maps=[v for v in handlers.values() if v.get("type")=="bpf_map_handler" and v.get("name")=="@hits"]
            assert len(maps)==1 and maps[0]["attr"]["map_type"]==5
            runtime=stem.with_suffix(".runtime.log").read_text()
            assert "Attach successfully" in runtime
            assert "Using kernel eBPF runtime and maps" not in runtime
        assert r["setup_to_target_ready_ns"] == r["tracer_startup_ready_ns"] + r["target_startup_ready_ns"]
    out = Path(__file__).resolve().parent / "reports" / source.name
    out.mkdir(parents=True, exist_ok=True)
    relative = os.path.relpath(source, out)
    rng = np.random.default_rng(20261013)
    indices = rng.integers(0, rounds, size=(10000, rounds))
    values = {key: np.array([r["ns_per_call"] for r in group]) for key,group in groups.items()}
    boot = {key: np.median(v[indices], axis=1) for key,v in values.items()}
    stats = []
    for delay in sleeps:
        base = np.median(values[delay, "baseline"])
        for mode in MODES:
            v = values[delay, mode]
            lo, hi = np.percentile(boot[delay, mode], [2.5,97.5])
            dlo, dhi = np.percentile(boot[delay, mode]-boot[delay, "baseline"], [2.5,97.5])
            stats.append(dict(sleep_ns=delay, mode=mode, n=len(v), median_ns=float(np.median(v)),
                              min_ns=float(v.min()), max_ns=float(v.max()),
                              cv_percent=float(v.std(ddof=1)/v.mean()*100),
                              median_ci_low_ns=float(lo), median_ci_high_ns=float(hi),
                              overhead_ns=float(np.median(v)-base),
                              overhead_ci_low_ns=float(dlo), overhead_ci_high_ns=float(dhi)))
    with (out/"statistics.csv").open("w") as stream:
        writer=csv.DictWriter(stream,fieldnames=list(stats[0])); writer.writeheader(); writer.writerows(stats)

    plt.rcParams.update({"font.size":10,"axes.spines.top":False,"axes.spines.right":False})
    fig, axes = plt.subplots(1, 2, figsize=(12,4.6))
    jitter = np.linspace(-.13,.13,rounds)
    for k,(mode,color) in enumerate(zip(MODES,COLORS)):
        v=values[0,mode]/1000
        axes[0].scatter(k+jitter,v,color=color,s=20,alpha=.65)
        axes[0].hlines(np.median(v),k-.22,k+.22,color=color,linewidth=3)
    axes[0].set_xticks(range(4),MODES); axes[0].set_ylabel("Wall time per iteration (us)")
    axes[0].set_title("No sleep: rounds and median"); axes[0].set_ylim(bottom=0)
    delays=[s for s in sleeps if s]
    for mode,color in zip(MODES,COLORS):
        med=[np.median(values[d,mode])/1000 for d in delays]
        lower=[np.percentile(boot[d,mode],2.5)/1000 for d in delays]
        upper=[np.percentile(boot[d,mode],97.5)/1000 for d in delays]
        axes[1].errorbar(range(len(delays)),med,yerr=[np.array(med)-lower,np.array(upper)-med],
                         label=mode,color=color,marker="o",capsize=3)
    axes[1].set_xticks(range(len(delays)),[str(d) for d in delays])
    axes[1].set_xlabel("Requested nanosleep (ns)"); axes[1].set_ylabel("Call + sleep wall time (us)")
    axes[1].set_title("Sleep: median and bootstrap 95% interval"); axes[1].legend()
    for ax in axes: ax.grid(axis="y",alpha=.2)
    fig.tight_layout()
    for suffix in ("png","pdf"): fig.savefig(out/("latency."+suffix),dpi=180)
    plt.close(fig)

    fig,ax=plt.subplots(figsize=(9,4.5))
    setupstats=[]
    for i,(mode,color) in enumerate(zip(MODES,COLORS)):
        group=[r for r in valid if r["mode"]==mode]
        total=np.array([r["setup_to_target_ready_ns"] for r in group])/1e6
        ax.scatter(i+rng.uniform(-.18,.18,len(group)),total,color=color,s=15,alpha=.55)
        ax.hlines(np.median(total),i-.25,i+.25,color=color,linewidth=3)
        setupstats.append((mode,len(group),np.median([r["tracer_startup_ready_ns"] for r in group])/1e6,
                           np.median([r["target_startup_ready_ns"] for r in group])/1e6,
                           np.median(total),np.min(total),np.max(total)))
    ax.set_xticks(range(4),MODES); ax.set_ylabel("Startup-to-ready time (ms)")
    ax.set_title("Preparation time including loading / compilation / agent initialization")
    ax.grid(axis="y",alpha=.2); ax.set_ylim(bottom=0); fig.tight_layout()
    for suffix in ("png","pdf"): fig.savefig(out/("setup."+suffix),dpi=180)
    plt.close(fig)

    lookup={(s["sleep_ns"],s["mode"]):s for s in stats}
    med=lambda d,m:lookup[d,m]["median_ns"]
    main_table="\n".join("| %d | %s | %.3f | [%.3f, %.3f] | %.3f | %.2f%% |" %
        (s["sleep_ns"],s["mode"],s["median_ns"]/1000,s["median_ci_low_ns"]/1000,
         s["median_ci_high_ns"]/1000,s["overhead_ns"]/1000,s["cv_percent"]) for s in stats)
    diff_table="\n".join("| %d | %s | %.3f | [%.3f, %.3f] |" %
        (s["sleep_ns"],s["mode"],s["overhead_ns"]/1000,s["overhead_ci_low_ns"]/1000,
         s["overhead_ci_high_ns"]/1000) for s in stats if s["mode"]!="baseline")
    contrasts=[]
    for delay in sleeps:
        for left,right in (("bpftrace","aya"),("aya","bpftime"),("bpftrace","bpftime")):
            lo,hi=np.percentile(boot[delay,left]-boot[delay,right],[2.5,97.5])
            contrasts.append("| %d | %s − %s | %.3f | [%.3f, %.3f] |" %
                             (delay,left,right,(med(delay,left)-med(delay,right))/1000,lo/1000,hi/1000))
    contrast_table="\n".join(contrasts)
    setup_table="\n".join("| %s | %d | %.3f | %.3f | %.3f | %.3f–%.3f |" % s for s in setupstats)
    sleep_ratios="\n".join("| %d | %.3f | %.3f | %.3f |" %
        (d,med(d,"aya")/med(d,"bpftime"),med(d,"bpftrace")/med(d,"bpftime"),med(d,"baseline")/1000)
        for d in sleeps)
    sleepy_speedups=[med(d,m)/med(d,"bpftime") for d in sleeps if d for m in ("aya","bpftrace")]
    sleepy_inconclusive=[]
    for d in sleeps:
        if d:
            lo,hi=np.percentile(boot[d,"bpftrace"]-boot[d,"aya"],[2.5,97.5])
            if lo <= 0 <= hi:
                sleepy_inconclusive.append(d)
    aya_attach=[r["aya_attach_ns"]/1e6 for r in valid if r["mode"]=="aya"]
    repeated=[r for r in valid if r.get("shutdown_sigint_count",0)>1]
    baseline_range=(min(values[0,"baseline"])*config["iterations"]/1e6,
                    max(values[0,"baseline"])*config["iterations"]/1e6)
    settings=env.get("system_settings",{})
    model=re.search(r"model name\s*:\s*(.*)",env["cpuinfo"])[1]
    report=f'''# uprobe 睡眠间隔与挂载准备耗时实验报告

数据批次：`{source.name}`。开始时间：{env.get("started_at","未记录")}。本报告由原始日志和数据自动核验后生成；短测、之前中断的批次均未并入正式统计。

## 1. 核心结果

无 sleep 时，Aya、bpftrace、bpftime 的每次调用耗时中位数分别为 **{med(0,"aya")/1000:.3f} μs、{med(0,"bpftrace")/1000:.3f} μs、{med(0,"bpftime")/1000:.3f} μs**。bpftime 的吞吐量分别为两个内核方案的 **{med(0,"aya")/med(0,"bpftime"):.2f} 倍、{med(0,"bpftrace")/med(0,"bpftime"):.2f} 倍**。

加入 nanosleep 后，应比较“调用 + 实际等待 + 调度”的总耗时，而不能将 `sleep_ns` 直接当作真实调用间隔。实际 baseline 如下，远大于请求的 200–5000 ns：

| 请求 sleep/ns | Aya 总耗时 / bpftime | bpftrace 总耗时 / bpftime | baseline 每轮迭代/μs |
| --- | ---: | ---: | ---: |
{sleep_ratios}

这些比值同时等于 bpftime 相对对应方案的吞吐量比。sleep 组接近 1 的比值表示总循环被等待成本主导，不表示三个探针自身开销完全相同。小差值必须结合下面的置信区间及波动看待。

本批次非零 sleep 组中，bpftime 相对两个内核方案的吞吐量优势约为 **{(min(sleepy_speedups)-1)*100:.1f}%–{(max(sleepy_speedups)-1)*100:.1f}%**，明显小于无 sleep 时的倍数优势。Aya 与 bpftrace 在请求 sleep 为 **{', '.join(map(str,sleepy_inconclusive))} ns** 的组间差值区间跨过 0，这些组不宜给二者排出确定的高低名次。

![调用耗时对比](latency.png)

左图每个点是一轮批量测量的均值，横线是各轮中位数；右图是中位数及 bootstrap 95% 区间。它们不是单次函数调用的 p50/p99 延迟。

## 2. 环境与实验设计

| 项目 | 记录 |
| --- | --- |
| CPU | {model} |
| 内核 | {env["uname"][2]}，{env["uname"][4]} |
| 目标 CPU | 逻辑 CPU {config["cpu"]} |
| SMT 邻居 | {settings.get('/sys/devices/system/cpu/cpu%d/topology/thread_siblings_list'%config['cpu'],'未记录')} |
| governor | {settings.get('/sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor'%config['cpu'],'未记录')} |
| 内核 BPF JIT 开关 | {settings.get('/proc/sys/net/core/bpf_jit_enable','未记录')} |
| Intel no_turbo | {settings.get('/sys/devices/system/cpu/intel_pstate/no_turbo','未记录')}（0 表示未禁用 turbo） |
| 初始 loadavg | {settings.get('/proc/loadavg','未记录')} |
| bpftrace | {env['versions']['bpftrace']} |
| Aya / aya-ebpf | 归档 Cargo.lock：0.14.0 / 0.2.1 |
| 轮数 | 每组合 {rounds} 轮，5 种间隔 × 4 方案，共 {len(valid)} 次有效测量 |
| 无 sleep | 测量 {config['iterations']:,} 次，预热 {config['warmup']:,} 次 |
| 非零 sleep | 每种测量 {config['sleep_iterations']:,} 次，预热 {config['sleep_warmup']:,} 次 |
| timer slack | {sorted(set(r['timer_slack_ns'] for r in valid))} ns，保留系统设置 |
| 随机顺序 | 每轮随机打乱 20 个组合，seed={config['seed']} |

测试未修改系统 governor、turbo 或调度策略；CPU 固定但未独占，也未隔离 SMT 邻居。环境仍可能受其他任务和动态频率影响。Rust 版本在 sudo 环境可能查不到，按 environment.json 的真实记录保留；已归档加载器、对象、bpftime 库的 SHA-256，但安装库的精确源码提交和实际 JIT 后端未得到独立确认。

目标 C 函数 `bench_add` 是禁止内联、禁止克隆的简单加法，包含 16 个 NOP 和编译器屏障，以保留真实调用和用户态 hook 所需指令空间。目标用 GCC -O2、禁用 LTO/PIE 构建。三组探针均对固定键计数，不在每次触发时输出日志。

- baseline 无探针；Aya 使用内核 uprobe 和 Rust per-CPU hash；bpftrace 使用内核 uprobe 与 `@hits[0] = count()`。
- bpftime 复用 bpftrace 的计数脚本，server 预加载到 tracer，agent 预加载到目标，在用户态执行 hook。每次试验使用独立共享内存名称。
- 非零 sleep 的每次迭代先调用函数，再请求一次 nanosleep；最后一次迭代也 sleep。EINTR 后继续等待剩余时间，零间隔完全不调用 nanosleep。
- 预热结束后，以 CLOCK_MONOTONIC_RAW 在整个循环前后计时；`elapsed_ns / iterations` 是一次迭代的摊销墙钟耗时。编译、加载、挂载、预热和退出清理均不在该区间内。
- 目标输出 BENCH 后保持存活，等待 tracer 读取计数并停止，再退出；等待发生在计时结束后，不增加报告中的循环耗时。

相比原来 7 轮，每组合增加到 15 轮；每轮正式调用和预热数也加倍。样本更多能帮助观察波动，但不消除系统性偏差。

## 3. 每种 sleep 的完整性能结果

单位为 μs/迭代。额外开销 = 本组中位数 − **相同 sleep 的 baseline 中位数**；没有用无 sleep baseline 去扣减睡眠组。

| 请求 sleep/ns | 方案 | 中位数/μs | 中位数 95% 区间/μs | 额外开销/μs | 批次变异系数 |
| --- | --- | ---: | ---: | ---: | ---: |
{main_table}

## 4. 差值的不确定性

下面直接对“组中位数 − baseline 中位数”给出区间，避免仅因两个中位数不同就断言探针更快或更慢。

| 请求 sleep/ns | 方案 | 差值/μs | 差值 95% 区间/μs |
| --- | --- | ---: | ---: |
{diff_table}

方法：按轮号配对，重采样 {rounds} 个轮号 10,000 次；每次分别算该组与 baseline 的中位数并相减，取 2.5%/97.5% 分位点。中位数区间也采用同一组轮号重采样。随机种子为 20261013，详细统计保存在 [statistics.csv](statistics.csv)。

三个探针方案之间的直接差值如下，正值表示右边方案耗时更低；区间同样按轮号配对重采样：

| 请求 sleep/ns | 对比 | 中位数之差/μs | 差值 95% 区间/μs |
| --- | --- | ---: | ---: |
{contrast_table}

这是有限样本的 percentile bootstrap 近似，没有做多重比较校正，且假设轮次近似可交换；随机顺序不能保证相邻轮次完全独立。区间跨 0 的差值不应下确定性结论。睡眠组的差值包含探针对唤醒、CPU 频率、缓存和调度状态的间接影响，不能直接称为纯 uprobe 执行成本。

## 5. 挂载相关耗时

三方统一可观察的是**启动到可测试的准备流程**。不把它冒充纯 attach API 时延。

1. tracer-startup-ready：启动 tracer 前至 runner 观察到就绪，包含进程启动、加载、bpftrace 编译及检查延迟。bpftime 用 bpftimetool 检查共享内存。
2. target-startup-ready：启动目标前至目标 main 中的 CLOCK_MONOTONIC 时间戳，包含进程创建、affinity、动态链接；bpftime 还包含 agent 初始化、JIT/hook 安装。
3. total-ready：上述两项逐样本相加，排除人为等待的 0.2 秒、预热和测量。baseline 仅包含目标启动。

各 sleep 组执行的准备操作相同，下面合并每方案 {rounds*len(sleeps)} 个样本描述其分布；各 sleep 分组数据仍在原始 attach_summary.csv 中。这不是独立的冷启动实验，缓存和机器状态随运行变化。

| 方案 | 样本数 | tracer-ready 中位数/ms | target-ready 中位数/ms | total-ready 中位数/ms | total-ready 范围/ms |
| --- | ---: | ---: | ---: | ---: | ---: |
{setup_table}

中位数不满足可加性，所以表中两个分量的中位数之和可能不等于 total 中位数。

![准备耗时分布](setup.png)

Aya 额外在 `UProbe::attach` 调用周围直接计时：中位数 **{statistics.median(aya_attach):.3f} ms**，范围 **{min(aya_attach):.3f}–{max(aya_attach):.3f} ms**，包括符号解析和 attach API 内部操作、不包括前面的对象/程序加载。bpftrace 和 bpftime 没有同口径的独立 attach API 时间，故不能拿此值与它们的 total-ready 排名。

tracer 就绪轮询间隔为 5 ms；bpftrace READY 受异步输出影响，bpftime 导出还需启动检查工具。这些观察开销不相同，足以影响毫秒级排名。target-ready 终点取自目标内部时间戳，不采用日志读到的时刻。以上所有准备时间均不是精确测得的内核 attach syscall 成本。

## 6. 为什么存在这些差异

**无 sleep 时，内核触发路径通常是主要差异来源。** Aya 和 bpftrace 都通过内核 uprobe 执行探针，需要断点异常处理、上下文处理、执行 BPF/Map 操作并恢复目标指令；这不意味着每次都调度切换到另一个进程。bpftime 使用进程内二进制重写 hook，避免每次走内核 uprobe 的陷入路径。这个机制与观察到的性能差异相符，但没有 profile，不能把全部差值或某个固定纳秒数归因于用户态/内核态转换。[Linux 5.4 uprobes 源码](https://github.com/torvalds/linux/blob/v5.4/kernel/events/uprobes.c)、[bpftime 设计说明](https://eunomia.dev/bpftime/documents/how-it-works/)

**sleep 后，等待与唤醒成本占据更大的比重。** nanosleep 的参数虽以 ns 表示，却不保证严格按请求值恢复执行。定时器粒度、timer slack 和任务重新获得 CPU 都会延长墙钟等待；本次 timer slack 已实测归档。因此 200 ns 和 500 ns 两组实际耗时接近、甚至受噪声影响发生顺序交叉，都不能证明 sleep API 精确到了这个分辨率。[nanosleep 手册](https://man7.org/linux/man-pages/man2/nanosleep.2.html)、[timer slack 手册](https://man7.org/linux/man-pages/man2/PR_SET_TIMERSLACK.2const.html)

**Aya 与 bpftrace 的细小差异需要谨慎归因。** 它们共享内核探针路径，但生成的指令与 Map 更新实现未保证完全相同；编译器、加载器细节和系统状态都可能产生差异。加载器使用 Rust 或 C++ 并不能直接解释热循环的差值，bpftrace 也不是每次命中都解释一遍脚本。

**睡眠组的额外差值可能比无 sleep 更大。** 每次睡眠使线程离开连续热循环，CPU 频率、空闲状态、缓存温度和重新调度都会变化。探针会改变 sleep 被请求的时间点，进而影响下一次唤醒；因此“带探针睡眠组 − baseline 睡眠组”并不保证等于连续热循环中测得的探针成本。报告保留这些真实差值，但没有硬件计数器或调度轨迹，不能将增量具体归因于某一种机制。

**准备时间与触发成本是不同指标。** bpftrace 的编译/加载、bpftime agent 的初始化/JIT/hook 都主要发生在开始阶段，热循环快不代表准备也最快。准备时间在真实应用中通常被后续许多次调用摊销。

## 7. 正确性、退出稳定性与限制

已逐项核对全部 {len(valid)} 个有效样本的 target 日志、checksum、计时和配置；全部 {sum(r['mode']!='baseline' for r in valid)} 个探针样本最终计数等于 iterations + warmup。缺失或错误计数不会被当作性能结果。

- 正式批次失败尝试数：**{len(failed)}**；有效样本中重试后成功的数量：**{sum(r.get('attempt',1)>1 for r in valid)}**。失败记录保留在 raw.json，不计入统计。
- 有效样本中需要重发 SIGINT 的数量：**{len(repeated)}**，最多发送 **{max(r.get('shutdown_sigint_count',0) for r in valid)}** 次。这个动作只发生在计时结束后。
- tracer 日志中包含 perf-event 关闭警告的有效样本：**{warnings}**。计数仍正确且进程返回码通过 runner 校验，但不能据此声称 bpftime 的生命周期兼容性没有问题。
- 之前中断批次的目标虽然完成计时，但没有最终计数，不能事后恢复成有效探针样本；本报告没有使用它们。

无 sleep baseline 的单轮计时长度范围为 **{baseline_range[0]:.2f}–{baseline_range[1]:.2f} ms**，仍比探针组短；对应基线倍率容易受频率和调度变化影响。更多轮次提升了分布观察，但没有将所有负载变成相同持续时间。全实验也没有覆盖多线程、多键 Map、真实业务函数、参数采集、堆栈、事件传输或 uretprobe。

不能把本次速度倍数推导为应用整体加速。实际收益取决于探针调用频率、业务本身耗时、并发和等待比例。

## 8. 复现与证据

```bash
sudo python3 bench/run.py \\
  --modes baseline,aya,bpftrace,bpftime \\
  --bpftime-server /home/wangyiwei/.bpftime/libbpftime-syscall-server.so \\
  --bpftime-agent /home/wangyiwei/.bpftime/libbpftime-agent.so \\
  --bpftimetool /home/wangyiwei/.bpftime/bpftimetool \\
  --sleep-ns 0,200,500,1000,5000 \\
  --iterations {config['iterations']} --warmup {config['warmup']} \\
  --sleep-iterations {config['sleep_iterations']} --sleep-warmup {config['sleep_warmup']} \\
  --rounds {rounds} --cpu {config['cpu']} --seed {config['seed']} \\
  --timeout {config['timeout']:g} --shutdown-timeout {config['shutdown_timeout']:g} --shutdown-retries {config['shutdown_retries']}
```

- [全部原始结果]({relative}/raw.json)、[性能汇总]({relative}/summary.csv)、[准备耗时汇总]({relative}/attach_summary.csv)。
- [环境、二进制哈希和配置]({relative}/environment.json)、[运行时源码快照]({relative}/source/)。
- [统计明细](statistics.csv)、[耗时图 PDF](latency.pdf)、[准备耗时图 PDF](setup.pdf)。
- 生成报告：`bench/.analysis-venv/bin/python bench/analyze_sleep.py {source}`。
'''
    (out/"report.md").write_text(report)
    audit=dict(valid=len(valid),failed=len(failed),count_verified=sum(r["mode"]!="baseline" for r in valid),
               shutdown_warning_samples=warnings,repeat_signal_samples=len(repeated),
               max_shutdown_signals=max(r.get("shutdown_sigint_count",0) for r in valid),
               numpy=np.__version__,matplotlib=matplotlib.__version__,
               bootstrap_samples=10000,bootstrap_seed=20261013)
    (out/"audit.json").write_text(json.dumps(audit,indent=2))
    print(json.dumps(audit,indent=2)); print(out/"report.md")


if __name__=="__main__":
    main()
