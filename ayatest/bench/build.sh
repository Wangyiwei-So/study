#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# Prefer the optional project-local linker; do not replace the user's toolchain.
if [[ -x "$PWD/bench/toolchain/bin/bpf-linker" ]]; then
    export PATH="$PWD/bench/toolchain/bin:$PATH"
fi
mkdir -p bench/build
gcc -O2 -g -Wall -Wextra -Werror -fno-lto -fno-pie -no-pie bench/target.c -o bench/build/target
cargo build --release --locked --bin aya-bench
(cd ebpf && cargo +nightly build --release --locked -Z build-std=core --bin bench-ebpf)
