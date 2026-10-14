#![no_std]
#![no_main]

use aya_ebpf::{
    macros::{map, uprobe},
    maps::PerCpuHashMap,
    programs::ProbeContext,
};

// Same logical operation as bpftrace's @hits[0] = count().
#[map]
static HITS: PerCpuHashMap<u64, u64> = PerCpuHashMap::with_max_entries(4096, 0);

#[uprobe]
pub fn bench_count(_ctx: ProbeContext) -> u32 {
    if let Some(value) = HITS.get_ptr_mut(0) {
        // A single-threaded target, no concurrent users of this per-CPU value.
        unsafe {
            *value += 1;
        }
    } else {
        let _ = HITS.insert(0, 1, 0);
    }
    0
}

#[panic_handler]
fn panic(_info: &core::panic::PanicInfo) -> ! {
    loop {}
}

#[unsafe(link_section = "license")]
#[unsafe(no_mangle)]
pub static LICENSE: [u8; 13] = *b"Dual MIT/GPL\0";
