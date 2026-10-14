# Aya uprobe 入门示例

性能对比见 [bench/README.md](bench/README.md)：独立测试 Aya、bpftrace 和 bpftime 的计数型 uprobe 开销。

本例观察 `uprobe_demo.c` 中每秒执行一次的 `int add(int a, int b)`，打印调用进程、线程和两个参数。代码包含中文注释，建议先读 `ebpf/src/main.rs`，再读 `src/main.rs`。

## 三个程序的分工

| 文件 | 执行位置 | 作用 |
| --- | --- | --- |
| `uprobe_demo.c` | 用户态 | 被观测的目标，调用 `add(10, 20)` |
| `ebpf/src/main.rs` | 内核中的 eBPF 执行环境 | 在函数入口读取参数，写入日志 Map |
| `src/main.rs` | 用户态 | 加载字节码、挂载探针、读取日志，直到 Ctrl+C |

执行顺序是：C 程序调用 add → 触发 uprobe → 探针读取参数并记录日志 → C 程序继续执行。用户态加载器异步读取日志，所以两个终端的输出不保证严格同步。

## 环境与版本

- x86_64 Linux。当前配置按 x86_64 调用约定读取参数。
- 使用 Aya 0.14.0、aya-log 0.3.0、aya-ebpf 0.2.1、aya-log-ebpf 0.2.0；两个 Cargo.lock 固定解析后的依赖。
- 本例的日志库使用 BPF ring buffer，需要支持它的 Linux 内核（通常为 5.8 或更新版本），以及可加载 BPF、挂载 uprobe 的权限。容器内 root 不一定具备宿主机所需能力。
- Rust 用户态依赖要求至少 1.87；eBPF 构建需要 nightly、rust-src 和 bpf-linker。

缺少工具时安装：

```bash
rustup toolchain install nightly --component rust-src
cargo install bpf-linker --locked
```

## 编译

在项目根目录执行：

```bash
# 保留符号和函数调用，方便按 add 符号挂载；不要 strip 这个文件。
cc -g -O0 -Wall -Wextra uprobe_demo.c -o uprobe_demo

# 必须进入 ebpf 目录，这样 Cargo 才会读取 ebpf/.cargo/config.toml。
# bpfel 是小端 eBPF 字节码目标；-Z build-std=core 为它编译 core。
(cd ebpf && cargo +nightly build --release -Z build-std=core)

# 编译用户态加载器。
cargo build
```

## 运行

终端一（项目根目录）：

```bash
./uprobe_demo
```

终端二（项目根目录）：

```bash
sudo ./target/debug/ayatest \
  ./ebpf/target/bpfel-unknown-none/release/uprobe-ebpf \
  ./uprobe_demo
```

预计每秒看到一条类似日志，数字 PID/TID 因运行而异：

```text
pid=12345 tid=12345 add(a=10, b=20)
```

默认跟踪所有执行这个目标文件的进程。如需只跟踪终端一打印的 PID，把它作为最后一个参数，例如：

```bash
sudo ./target/debug/ayatest \
  ./ebpf/target/bpfel-unknown-none/release/uprobe-ebpf \
  ./uprobe_demo 12345
```

两个终端分别按 Ctrl+C 停止。退出加载器会自动卸载它持有的探针，不会终止 C 程序。不要在目标运行或探针挂载期间重新编译覆盖目标文件；修改 C 代码后先停止两者，再重新编译和启动。

## 阅读时关注这些点

1. `#[uprobe]` 标记探针入口，并不指定目标 C 函数；关联是在用户态 `attach("add", ...)` 时建立的。
2. `ctx.arg(0)` 和 `ctx.arg(1)` 读取调用约定中保存的参数。这里的 `i32` 对应本例 C 的 `int`，不是所有函数都能照搬相同类型。
3. uprobe 在函数入口触发，这时没有返回值。探针返回的 `0` 不等于 `add` 的返回值 `30`。观察返回值需要 uretprobe。
4. `Ebpf::load_file` 解析对象并创建 Map，`program.load()` 提交程序给 verifier，`program.attach()` 才开始观测目标。
5. 本例使用 Aya 0.14 的三个参数挂载接口。不要混用旧教程中的 `attach(Some("add"), 0, path, pid)`。

若报 `Operation not permitted`，检查实际 BPF 权限；若 ring buffer Map 创建失败，检查内核版本。找不到符号时用 `nm ./uprobe_demo` 检查 `add`，确认路径指向 C 可执行文件而不是 `.c` 源码。

参考：[Aya 开发环境](https://aya-rs.dev/book/start/development/)、[UProbe API](https://docs.rs/aya/0.14.0/aya/programs/uprobe/struct.UProbe.html)、[aya-log 日志接收](https://docs.rs/aya-log/0.3.0/aya_log/)。
