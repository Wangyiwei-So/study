#define _GNU_SOURCE
#include <errno.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <sys/prctl.h>

/* The memory barrier prevents call elimination; NOPs allow userspace hooking
 * even though the useful function body is very short. Never build with LTO. */
__attribute__((noinline, noclone, aligned(16)))
uint64_t bench_add(uint64_t a, uint64_t b)
{
    __asm__ volatile(".rept 16; nop; .endr" ::: "memory");
    return a + b;
}

static uint64_t ns(void)
{
    struct timespec t;
    if (clock_gettime(CLOCK_MONOTONIC_RAW, &t)) { perror("clock_gettime"); exit(1); }
    return (uint64_t)t.tv_sec * 1000000000ULL + t.tv_nsec;
}

static uint64_t number(const char *s, int allow_zero)
{
    char *end;
    errno = 0;
    unsigned long long n = strtoull(s, &end, 10);
    if (errno || !*s || *end || *s == '-' || (!allow_zero && !n) || n > 1000000000000ULL) {
        fprintf(stderr, "invalid iteration count: %s\n", s); exit(2);
    }
    return n;
}

static uint64_t run(uint64_t n, uint64_t sleep_ns)
{
    uint64_t sum = 0;
    if (!sleep_ns) {
        for (uint64_t i = 0; i < n; ++i) sum += bench_add(i, 1);
    } else {
        const struct timespec request = {
            .tv_sec = sleep_ns / 1000000000ULL,
            .tv_nsec = sleep_ns % 1000000000ULL,
        };
        for (uint64_t i = 0; i < n; ++i) {
            sum += bench_add(i, 1);
            struct timespec remaining = request;
            while (nanosleep(&remaining, &remaining)) {
                if (errno != EINTR) { perror("nanosleep"); exit(1); }
            }
        }
    }
    return sum;
}

int main(int argc, char **argv)
{
    if (argc < 3 || argc > 5 || (argc == 5 && strcmp(argv[4], "--wait-exit"))) {
        fprintf(stderr, "usage: %s iterations warmup [sleep_ns [--wait-exit]]\n", argv[0]); return 2;
    }
    uint64_t n = number(argv[1], 0), warm = number(argv[2], 0);
    uint64_t sleep_ns = argc >= 4 ? number(argv[3], 1) : 0;
    // Python monotonic_ns() uses this same Linux clock. Agent initialization
    // has completed before main; record this endpoint before warmup and timing.
    struct timespec ready;
    if (clock_gettime(CLOCK_MONOTONIC, &ready)) { perror("clock_gettime"); return 1; }
    printf("TARGET_READY {\"monotonic_ns\":%" PRIu64 ",\"timer_slack_ns\":%ld}\n",
           (uint64_t)((uint64_t)ready.tv_sec * 1000000000ULL + ready.tv_nsec),
           (long)prctl(PR_GET_TIMERSLACK, 0, 0, 0, 0));
    fflush(stdout);
    uint64_t sum = run(warm, sleep_ns);
    uint64_t start = ns();
    sum += run(n, sleep_ns);
    uint64_t elapsed = ns() - start;
    printf("BENCH {\"iterations\":%" PRIu64 ",\"warmup\":%" PRIu64
           ",\"elapsed_ns\":%" PRIu64 ",\"checksum\":%" PRIu64
           ",\"sleep_ns\":%" PRIu64 "}\n",
           n, warm, elapsed, sum, sleep_ns);
    fflush(stdout);
    // Keep agent/shared resources alive until the tracer has read its maps.
    // This handshake is entirely outside the measurement and warmup regions.
    if (argc == 5) (void)getchar();
    return 0;
}
