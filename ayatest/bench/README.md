# uprobe 性能对比

这套基准比较 **目标程序在持续触发函数入口探针时的耗时**。不计入编译、加载、挂载、JIT 初始化和退出输出时间；不测 uretprobe、事件传输吞吐量或多线程扩展性。

现在还支持每次调用后 `nanosleep`，并单独保存启动和挂载准备耗时。热循环耗时与准备耗时分开汇总，见下文。

| 分组 | 探针实现 | 执行位置 |
| --- | --- | --- |
| baseline | 无探针 | 目标程序 |
| aya | Rust eBPF，per-CPU hash 计数 | 内核 |
| bpftrace | `@hits[0] = count()` | 内核 |
| bpftime | 同一 bpftrace 脚本，syscall-server + agent 预加载 | 用户态运行时 |

Aya 是开发框架，bpftrace 是追踪语言/工具，bpftime 是运行时，三者并非同一层级。这里比较的是三个具体配置的端到端开销，不能把结果直接解释为 Rust/C++ 语言性能，或 Aya 框架本身的开销。计数语义一致，但编译后的指令、Map 实现及安全机制不保证一致。

## 编译

在项目根目录执行：

```bash
bash bench/build.sh
```

使用现有 Cargo.lock。新增的 `aya-bench` 和 `bench-ebpf` 不使用 aya-log，不创建 ring buffer，避免把原示例的逐次日志成本混进结果。现有教学示例保持独立。

如果出现 `bpf-linker: LLVM ... Invalid record`，检查 `rustc -vV` 的 LLVM 版本和 `ldd "$(command -v bpf-linker)"` 是否匹配。本机 nightly 使用 LLVM 23，原 bpf-linker 链接 LLVM 22，可在项目内安装匹配版本：

```bash
LLVM_PREFIX=/usr/lib/llvm-23 LLVM_SYS_231_PREFIX=/usr/lib/llvm-23 cargo install bpf-linker \
  --version 0.11.1 --locked --no-default-features --features llvm-23 \
  --root bench/toolchain
bash bench/build.sh
```

此命令需要已安装 LLVM 23 开发文件；其他机器按实际版本匹配。构建脚本优先使用 `bench/toolchain/bin/bpf-linker`。

## 运行

先测无权限要求的基线：

```bash
python3 bench/run.py --modes baseline --iterations 10000000 --rounds 7
```

测试 Aya 和 bpftrace 需要内核 BPF/uprobes 权限，以及已安装的 bpftrace：

```bash
sudo python3 bench/run.py --modes baseline,aya,bpftrace
```

安装 bpftime 的方式参见[官方构建说明](https://github.com/eunomia-bpf/bpftime/blob/master/build-and-test.md)。完整三组比较：

```bash
sudo python3 bench/run.py \
  --bpftime-server /absolute/path/libbpftime-syscall-server.so \
  --bpftime-agent /absolute/path/libbpftime-agent.so \
  --iterations 1000000 --warmup 100000 --rounds 7
```

可通过 `--cpu N` 固定 CPU（默认当前允许集合中最小的 CPU），`--bpftrace /path/to/bpftrace` 选择版本，`--timeout 300` 延长每个进程的等待上限。输出目录默认使用时间戳，也可指定 `--output bench/results/my-run`，目录必须尚不存在。

## sleep 间隔与挂载准备耗时测试

修改后必须先运行 `bash bench/build.sh`，以更新 C 目标和 Aya 加载器。完整五种间隔、四个方案、7 轮：

```bash
sudo python3 bench/run.py \
  --modes baseline,aya,bpftrace,bpftime \
  --bpftime-server /home/wangyiwei/.bpftime/libbpftime-syscall-server.so \
  --bpftime-agent /home/wangyiwei/.bpftime/libbpftime-agent.so \
  --bpftimetool /home/wangyiwei/.bpftime/bpftimetool \
  --sleep-ns 0,200,500,1000,5000 \
  --iterations 1000000 --warmup 100000 \
  --sleep-iterations 10000 --sleep-warmup 1000 \
  --rounds 7 --cpu 0 --timeout 300
```

共 140 次测量。`--sleep-ns` 默认仍为 `0`，旧命令继续执行无 sleep 测试。200、500、1000、5000 的单位都是 ns，分别表示 200 ns、500 ns、1 μs、5 μs。

非零间隔时，预热和计时循环均执行 `bench_add` 后再 `nanosleep`，包括最后一次调用后的 sleep；计时循环包含 N 次函数调用和 N 次 sleep。收到 EINTR 时继续睡完剩余时间。0 间隔完全不调用 nanosleep，也不以 `nanosleep(0)` 替代。

`--iterations/--warmup` 用于零间隔；`--sleep-iterations/--sleep-warmup` 用于所有非零间隔，默认 10,000/1,000，避免真实 sleep 使 100 万次测试耗时过长。每种间隔内四个方案的次数完全相同，每轮随机打乱全部 `(间隔, 方案)` 组合。

请求 200 ns **不保证恰好 200 ns 后恢复执行**：定时器粒度、timer slack 和调度都可能延长实际等待。脚本保留系统 timer slack，不改变调度策略，并将实际查询到的 `timer_slack_ns` 写入 raw.json。此实验测的是“函数调用 + 相对 sleep + 调度”的整体墙钟耗时，不是精确的固定频率触发。要测固定的亚微秒间隔，需要另设计忙等实验，不能与真实 sleep 混为一组。[nanosleep 说明](https://man7.org/linux/man-pages/man2/nanosleep.2.html)、[timer slack 说明](https://man7.org/linux/man-pages/man2/PR_SET_TIMERSLACK.2const.html)

`summary.csv` 现在每种间隔分别汇总：`sleep_ns` 是请求值，`median_ns_per_call` 包含真实 sleep；`overhead_ns` 只减去**相同 sleep_ns 的 baseline**，不会减去零间隔基线。睡眠调度噪声可能使差值变小甚至为负，应保留并结合 min/max 判断。不同间隔的基准次数不同，CSV 中同时保存 iterations 和 warmup。

`attach_summary.csv` 单独保存准备耗时（单位 ns，终端转换为 ms）：

| 字段（汇总表加 median_/min_/max_ 前缀） | 测量边界 | 包含内容 |
| --- | --- | --- |
| `tracer_startup_ready_ns` | Python 启动 tracer 前 → 观察到 tracer/共享内存就绪 | 进程启动、加载，bpftrace 编译，以及就绪检查开销；bpftime 此时尚未加载目标 agent |
| `target_startup_ready_ns` | Python 启动目标前 → 目标 main 中预热前时间戳 | fork/exec、设置 affinity、动态链接；bpftime 还包含 agent 初始化、JIT/hook 安装 |
| `setup_to_target_ready_ns` | 上面两项相加 | 三组均覆盖到目标可开始测试的准备流程；排除中间人为等待的 0.2 秒、预热和计时循环 |
| `aya_load_ns` | Aya 对象加载前 → program.load 返回 | 对象/Map 加载和内核程序加载 |
| `aya_attach_ns` | Aya UProbe::attach 调用前 → 返回 | 符号解析和本次 attach API 操作；不含上面的 load |

`setup_over_baseline_ns` = 该组 setup 中位数 − 同间隔 baseline 目标启动中位数，是扣除普通进程启动后的准备成本估算。baseline 没有 tracer，其 tracer 时间为 0。其他方案的 `aya_*` 留空，绝不以 0 冒充已测值。

**跨三者比较的是启动/加载/挂载准备流程，不是统一的纯 attach API 时延。** 当前只有 Aya 能直接在本仓库内对 attach 调用计时。现有 bpftrace CLI 和 bpftime 预加载库没有暴露同口径的独立 attach 时间；要获得三者纯 attach 数据，需继续修改或插桩它们的加载器和 agent。不要把 Aya 的纯 attach 字段与另外两组总准备时间直接排名。

就绪检查每次间隔 5 ms；bpftrace 的 READY 还受异步输出影响，bpftime 还包含 bpftimetool 启动和共享内存导出。因此 tracer 准备时间属于 runner 观察值，不能据此解释微秒级差距，也不能认为两者观察延迟完全一致。目标启动终点则由 C 程序内部的 CLOCK_MONOTONIC 时间戳给出，与 Python monotonic_ns 使用同一时钟，不采用日志轮询到达时间。性能循环仍用 CLOCK_MONOTONIC_RAW。

原始数据继续写到根目录的 `raw.json`；日志按 `sleep-0ns/`、`sleep-200ns/` 等目录区分。新结构不会修改此前的历史结果。各次试验都重新启动 tracer/target，因此每种 sleep 都有对应的准备耗时样本；它们不是严格冷缓存启动测试。

退出采用握手：目标输出 BENCH 后阻塞等待（计时已结束），runner 先停止 tracer、读取计数，再关闭目标 stdin 让目标退出，避免提前释放 agent 资源。bpftime 停止时每隔 1 秒重发 SIGINT，最多等待 `--shutdown-timeout`（默认 30 秒）；信号次数写入 `shutdown_sigint_count`。超时会强制终止并将该样本标记 error，最多按 `--shutdown-retries`（默认 1）重跑该组合；重跑使用独立进程和共享内存，原日志保留在原位置，新日志在 retry 子目录。缺失计数、计数不一致等错误不会自动重试。只有 status=ok 的记录进入统计，报告应披露失败与重试数。

每次实验现在归档 `source/` 下的源码、依赖锁文件，以及 environment.json 中的二进制 SHA-256、CPU governor、SMT 邻居和内核 BPF JIT 开关。

为兼容 bpftrace 0.9.x，脚本使用 `-B none`，不使用旧版不支持的 `-q` 或 `-B line`。启动信息保存在日志中，不影响 READY 和计数解析。

**bpftime 分组尚未在本机实测**：需要所选 bpftime/bpftrace 版本支持 per-CPU hash、共享内存状态导出和退出时 Map 读取。缺失依赖、权限不足、挂载失败、计数错误或超时都会让脚本失败，不会作为性能结果。该组显式启用 JIT，关闭 kernel co-run；实际 LLVM/ubpf 后端取决于 bpftime 构建，比较时应另记其 commit、构建选项和 JIT 后端。

runner 每轮通过 `BPFTIME_GLOBAL_SHM_NAME` 为 bpftime 分配独立的共享内存名称，server 与 agent 使用相同名称，避免复用失败会话的状态。所用 bpftime 版本必须支持该变量（本机版本支持）。脚本不会删除其他会话的默认共享内存；正常退出由运行库回收本轮共享内存。

本机 bpftime 对 `BPFTIME_RUN_WITH_KERNEL` 和 `BPFTIME_DISABLE_JIT` 按环境变量是否存在判断，设为字符串 `false` 仍会开启对应开关。runner 会清除这两个变量，保证采用用户态模式且不禁用 JIT；每轮 bpftime 诊断信息保存在 `*.runtime.log`。

## 测量与结果

- 单线程目标用 `-O2` 编译，禁用 LTO；`bench_add` 禁止内联并包含编译器屏障和 16 个 NOP，保留真实调用及用户态 hook 空间。基线也执行完全相同的函数。
- 每轮随机排列分组，分组串行执行；每次启动新目标，先预热再用 `CLOCK_MONOTONIC_RAW` 计时。计时循环里不输出、不分配内存、不读取时钟。
- 探针每次只更新计数，结束时核对 `hits == warmup + iterations`；目标返回的 checksum 也必须正确。每次测试使用输出目录下独立的目标文件副本。
- `summary.csv` 输出各轮中位数、最小/最大值、额外 ns/call、减速倍数及 calls/s。额外开销 = 该组中位数 − baseline 中位数；减速倍数 = 两者之比。负差值保留，不会人为归零，应检查噪声。
- `raw.json` 保存每轮结果；各 `sleep-*ns/` 内 `*.target.log`、`*.tracer.log` 保存输出；`environment.json` 保存参数、CPU、内核和工具版本。

指标是整个循环的摊销 ns/call，包含目标函数、循环和探针开销，不是单次调用尾延迟。建议在空闲机器上增加轮数，使每轮至少持续数百毫秒，记录 CPU governor、turbo、内核 BPF JIT 设置；不要在不同机器或不同调用次数的独立运行间直接计算差值。仅 baseline 的运行用来检查脚本，正式比较会在同一轮实验中重新测 baseline。

没有探针的 bpftime agent 自身成本未单独分离：bpftime 组相对 baseline 的差值代表启用该方案后热循环的总变化。此版本没有加入空探针测试，因为必须先有可核验的计数证明每次调用确实触发探针。

参考：[bpftrace count() 语义](https://bpftrace.org/docs/0.22)、[bpftime 预加载与 JIT 配置](https://github.com/eunomia-bpf/bpftime/blob/master/usage.md)。

旧版 bpftrace 的 BEGIN 使用自身进程的 uprobe，只有 syscall-server 时不会触发。bpftime 分组因此不使用 BEGIN，而是通过 server 库同目录下的 bpftimetool export 检查目标事件已启用、已关联有效 BPF 程序，再启动 target。可用 --bpftimetool 指定工具路径；*.handlers.json 保存就绪状态，*.inspect.log 保存检查日志。最终仍严格核对触发计数。
