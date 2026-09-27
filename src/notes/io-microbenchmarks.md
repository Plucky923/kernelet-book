# I/O microbenchmarks

*Working material behind [Zero-copy I/O](../blueprint/asterinas-mode/zero-copy-io.md): the unit costs it labels **measured here**. Host: Intel Xeon E3-1270 v6 (4 cores, 8 threads), Linux 6.8.0, `gcc -O2`, one run, 2026-09-10. The script is reproduced at the end.*

## Results

| what | cost |
|---|---|
| `memcpy` 64 B, hot | 1.8 ns |
| `memcpy` 1,500 B, hot / cold | 13.6 ns (110 GB/s) / 231 ns |
| `memcpy` 4 KiB, hot / cold | 33 ns (124 GB/s) / 457 ns (9.0 GB/s) |
| `memcpy` 64 KiB, hot / cold | 1.16 µs (57 GB/s) / 6.4 µs (10.2 GB/s) |
| `memcpy` 2 MiB, hot / cold | 61 µs (34.5 GB/s) / 156 µs (13.4 GB/s) |
| `mmap` + touch + `munmap` of 4 KiB, no other threads | 1.30 µs + 1.77 µs + 1.92 µs |
| the same with three other threads of the process spinning on other CPUs | 1.32 µs + 1.82 µs + **2.90 µs** (the shootdown) |
| `mprotect` pair on 4 KiB, none / three other threads | 2.15 µs / **3.97 µs** |
| `mprotect` pair on 2 MiB of 4 KiB pages, none / three | 31.5 µs / 33.0 µs |
| futex ping-pong across two CPUs, one round trip | **5.81 µs** (two wakeups, two sleeps) |

"Cold" copies walk a 512 MiB working set so that neither source nor destination is in cache. The 2 MiB `mprotect` pair edits 512 leaf entries; a 2 MiB large-page entry would cost the 4 KiB figure. The futex round trip is the kernelet design's unit for "one thread wakeup": about 3 µs each way.

## The script

```c
// Microbenchmarks for the zero-copy device model: copy cost per size,
// map/unmap with and without TLB shootdown, and a thread wakeup round trip.
// Build: gcc -O2 -pthread bench.c -o bench
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <time.h>
#include <pthread.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <linux/futex.h>
#include <unistd.h>
#include <sched.h>

static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}

static void bench_memcpy(size_t sz, int iters){
    char *a=aligned_alloc(4096,sz), *b=aligned_alloc(4096,sz);
    memset(a,1,sz); memset(b,2,sz);
    // warm
    for(int i=0;i<4;i++) memcpy(b,a,sz);
    double t0=now();
    for(int i=0;i<iters;i++){ memcpy(b,a,sz); __asm__ volatile("" ::: "memory"); }
    double dt=now()-t0;
    printf("memcpy %8zu B: %8.1f ns/copy, %6.2f GB/s (%d iters)\n", sz, dt/iters*1e9, sz*(double)iters/dt/1e9, iters);
    free(a); free(b);
}

// cold copy: source and destination not in cache (walk a large buffer)
static void bench_memcpy_cold(size_t sz, int iters){
    size_t total=(size_t)512<<20; // 512 MiB working set
    char *pool=mmap(NULL,total,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_POPULATE,-1,0);
    memset(pool,1,total);
    size_t n=total/sz/2; if(n<2)n=2;
    double t0=now();
    for(int i=0;i<iters;i++){ size_t k=(i% n); memcpy(pool+(2*k+1)*sz, pool+(2*k)*sz, sz); }
    double dt=now()-t0;
    printf("memcpy cold %8zu B: %8.1f ns/copy, %6.2f GB/s\n", sz, dt/iters*1e9, sz*(double)iters/dt/1e9);
    munmap(pool,total);
}

static volatile int stop=0;
static void* spinner(void* arg){ int cpu=(intptr_t)arg; cpu_set_t s; CPU_ZERO(&s); CPU_SET(cpu,&s); pthread_setaffinity_np(pthread_self(),sizeof s,&s);
    volatile char *p=arg?NULL:NULL; (void)p; while(!stop){ __asm__ volatile("pause"); } return NULL; }

// map a region, touch it, unmap it: with N other threads of this process running on other CPUs
// (so the unmap needs TLB shootdown IPIs to them), or none.
static void bench_map_unmap(size_t sz, int nthreads, int iters){
    pthread_t th[8]; stop=0;
    for(int i=0;i<nthreads;i++) pthread_create(&th[i],NULL,spinner,(void*)(intptr_t)(i+1));
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(0,&s); pthread_setaffinity_np(pthread_self(),sizeof s,&s);
    usleep(20000);
    double tm=0,tt=0,tu=0;
    for(int i=0;i<iters;i++){
        double t0=now();
        char *p=mmap(NULL,sz,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);
        double t1=now();
        for(size_t o=0;o<sz;o+=4096) p[o]=1;
        double t2=now();
        munmap(p,sz);
        double t3=now();
        tm+=t1-t0; tt+=t2-t1; tu+=t3-t2;
    }
    stop=1; for(int i=0;i<nthreads;i++) pthread_join(th[i],NULL);
    printf("map/touch/unmap %8zu B, %d other threads: mmap %6.0f ns, touch %7.0f ns, munmap %6.0f ns\n", sz, nthreads, tm/iters*1e9, tt/iters*1e9, tu/iters*1e9);
}

// mprotect on an already-touched region with other threads running: pure shootdown cost
static void bench_mprotect(size_t sz, int nthreads, int iters){
    pthread_t th[8]; stop=0;
    for(int i=0;i<nthreads;i++) pthread_create(&th[i],NULL,spinner,(void*)(intptr_t)(i+1));
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(0,&s); pthread_setaffinity_np(pthread_self(),sizeof s,&s);
    usleep(20000);
    char *p=mmap(NULL,sz,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_POPULATE,-1,0);
    for(size_t o=0;o<sz;o+=4096) p[o]=1;
    double t0=now();
    for(int i=0;i<iters;i++){ mprotect(p,sz,PROT_READ); mprotect(p,sz,PROT_READ|PROT_WRITE); }
    double dt=now()-t0;
    stop=1; for(int i=0;i<nthreads;i++) pthread_join(th[i],NULL);
    munmap(p,sz);
    printf("mprotect pair %8zu B, %d other threads: %7.0f ns per pair\n", sz, nthreads, dt/iters*1e9);
}

// futex ping-pong between two threads on different CPUs: one wakeup + one sleep each way
static int f1=0,f2=0;
static void* pong(void* a){ cpu_set_t s; CPU_ZERO(&s); CPU_SET(1,&s); pthread_setaffinity_np(pthread_self(),sizeof s,&s);
    int iters=(intptr_t)a;
    for(int i=0;i<iters;i++){
        while(__atomic_load_n(&f1,__ATOMIC_ACQUIRE)!=i+1) syscall(SYS_futex,&f1,FUTEX_WAIT_PRIVATE,i,NULL,NULL,0);
        __atomic_store_n(&f2,i+1,__ATOMIC_RELEASE); syscall(SYS_futex,&f2,FUTEX_WAKE_PRIVATE,1,NULL,NULL,0);
    } return NULL; }
static void bench_futex(int iters){
    cpu_set_t s; CPU_ZERO(&s); CPU_SET(0,&s); pthread_setaffinity_np(pthread_self(),sizeof s,&s);
    pthread_t t; f1=f2=0; pthread_create(&t,NULL,pong,(void*)(intptr_t)iters);
    usleep(20000);
    double t0=now();
    for(int i=0;i<iters;i++){
        __atomic_store_n(&f1,i+1,__ATOMIC_RELEASE); syscall(SYS_futex,&f1,FUTEX_WAKE_PRIVATE,1,NULL,NULL,0);
        while(__atomic_load_n(&f2,__ATOMIC_ACQUIRE)!=i+1) syscall(SYS_futex,&f2,FUTEX_WAIT_PRIVATE,i,NULL,NULL,0);
    }
    double dt=now()-t0; pthread_join(t,NULL);
    printf("futex round trip (2 wakeups, 2 sleeps, cross-CPU): %7.0f ns\n", dt/iters*1e9);
}

int main(void){
    printf("cpus online: %ld\n", sysconf(_SC_NPROCESSORS_ONLN));
    bench_memcpy(64,2000000); bench_memcpy(1500,1000000); bench_memcpy(4096,500000); bench_memcpy(65536,50000); bench_memcpy(2<<20,2000);
    bench_memcpy_cold(4096,20000); bench_memcpy_cold(65536,4000); bench_memcpy_cold(2<<20,100);
    bench_map_unmap(4096,0,20000); bench_map_unmap(4096,3,20000);
    bench_map_unmap(2<<20,0,2000); bench_map_unmap(2<<20,3,2000);
    bench_mprotect(4096,0,20000); bench_mprotect(4096,3,20000); bench_mprotect(2<<20,0,2000); bench_mprotect(2<<20,3,2000);
    bench_futex(20000);
    return 0;
}
```

The cold 1,500-byte copy was measured separately with the same 512 MiB working set:

```c
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/mman.h>
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec*1e-9;}
int main(void){ size_t sz=1500, total=(size_t)512<<20; char *pool=mmap(NULL,total,PROT_READ|PROT_WRITE,MAP_PRIVATE|MAP_ANONYMOUS|MAP_POPULATE,-1,0); memset(pool,1,total);
 size_t stride=4096, n=total/stride/2; int iters=100000; double t0=now();
 for(int i=0;i<iters;i++){ size_t k=i%n; memcpy(pool+(2*k+1)*stride, pool+(2*k)*stride, sz); }
 double dt=now()-t0; printf("memcpy cold 1500 B: %.0f ns/copy\n", dt/iters*1e9); return 0; }
```

## Raw output

```
cpus online: 8
memcpy       64 B:      1.8 ns/copy,  35.55 GB/s (2000000 iters)
memcpy     1500 B:     13.6 ns/copy, 110.67 GB/s (1000000 iters)
memcpy     4096 B:     33.0 ns/copy, 124.30 GB/s (500000 iters)
memcpy    65536 B:   1157.1 ns/copy,  56.64 GB/s (50000 iters)
memcpy  2097152 B:  60786.1 ns/copy,  34.50 GB/s (2000 iters)
memcpy cold     4096 B:    457.0 ns/copy,   8.96 GB/s
memcpy cold    65536 B:   6424.5 ns/copy,  10.20 GB/s
memcpy cold  2097152 B: 156293.8 ns/copy,  13.42 GB/s
map/touch/unmap     4096 B, 0 other threads: mmap   1302 ns, touch    1772 ns, munmap   1922 ns
map/touch/unmap     4096 B, 3 other threads: mmap   1322 ns, touch    1816 ns, munmap   2902 ns
map/touch/unmap  2097152 B, 0 other threads: mmap   2078 ns, touch  753696 ns, munmap  86862 ns
map/touch/unmap  2097152 B, 3 other threads: mmap   2398 ns, touch  791028 ns, munmap  94733 ns
mprotect pair     4096 B, 0 other threads:    2147 ns per pair
mprotect pair     4096 B, 3 other threads:    3974 ns per pair
mprotect pair  2097152 B, 0 other threads:   31460 ns per pair
mprotect pair  2097152 B, 3 other threads:   32956 ns per pair
futex round trip (2 wakeups, 2 sleeps, cross-CPU):    5814 ns
```
