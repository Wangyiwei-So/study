#include <stdio.h>
#include <unistd.h>

/* Keep a separate function so a uprobe can attach to this symbol. */
__attribute__((noinline))
int add(int a, int b)
{
    return a + b;
}

int main(void)
{
    setvbuf(stdout, NULL, _IOLBF, 0);
    printf("PID: %ld\n", (long)getpid());

    for (;;) {
        int result = add(10, 20);
        printf("add(10, 20) = %d\n", result);
        sleep(1);
    }
}
