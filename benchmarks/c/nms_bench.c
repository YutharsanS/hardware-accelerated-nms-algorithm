/*
 * NMS in portable C, timed inside C (plan.md Phase E, E.4).
 *
 * A Python -> C call costs about 1 us, which would swamp a ~0.2 us answer, so each call
 * reads the clock itself and hands its own duration back. Built as a shared library and
 * called through ctypes by benchmarks/targets/cpu.py; the call overhead falls outside the
 * timed region.
 *
 * nms_scalar mirrors models/nms/model.py::nms_sequential exactly: the frozen record fields,
 * the 21-bit sort key K = score << 5 | (31 - index), the clamps, and the integer predicate
 * (I << 8) >= T_INT * U. It must return the golden model's keep_mask bit for bit.
 *
 * No hand-written SIMD: the compiler vectorises what it can at -O3 with the host's -march or
 * -mcpu, and the flags are recorded with every result.
 */

#define _POSIX_C_SOURCE 199309L /* clock_gettime under -std=c11 */

#include <stdint.h>
#include <time.h>

#define N 32
#define INDEX_W 5
#define K_SHIFT 8
#define T_INT 128

enum { X, Y, A, B, SCORE, FIELDS };

static inline uint64_t now_ns(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000u + (uint64_t)ts.tv_nsec;
}

static inline int32_t clamp_sub(int32_t hi, int32_t lo)
{
    return hi > lo ? hi - lo : 0;
}

static inline int32_t max32(int32_t a, int32_t b) { return a > b ? a : b; }
static inline int32_t min32(int32_t a, int32_t b) { return a < b ? a : b; }

/* (I << K_SHIFT) >= T_INT * U, in 64 bits: LHS reaches 2^32 and RHS 2^33. */
static inline int suppresses(const int32_t *k, const int32_t *c, int64_t area_k, int64_t area_c)
{
    int64_t w = clamp_sub(min32(k[A], c[A]), max32(k[X], c[X]));
    int64_t h = clamp_sub(min32(k[B], c[B]), max32(k[Y], c[Y]));
    int64_t inter = w * h;
    int64_t uni = area_k + area_c - inter;
    return (inter << K_SHIFT) >= (int64_t)T_INT * uni;
}

static uint32_t nms_sequential(const int32_t *rec, uint32_t present)
{
    int32_t order[N];
    int32_t key[N];
    int64_t area[N];

    for (int i = 0; i < N; i++) {
        const int32_t *r = rec + i * FIELDS;
        key[i] = (r[SCORE] << INDEX_W) | (N - 1 - i);
        area[i] = (int64_t)clamp_sub(r[A], r[X]) * clamp_sub(r[B], r[Y]);
        order[i] = i;
    }
    /* Insertion sort, descending on the strict total order K: no ties, so no instability. */
    for (int i = 1; i < N; i++) {
        int32_t s = order[i];
        int j = i - 1;
        while (j >= 0 && key[order[j]] < key[s]) {
            order[j + 1] = order[j];
            j--;
        }
        order[j + 1] = s;
    }

    uint32_t valid = present;
    uint32_t keep = 0;
    for (int rank = 0; rank < N; rank++) {
        int32_t slot = order[rank];
        if (!((valid >> slot) & 1u))
            continue;
        keep |= 1u << slot;
        valid &= ~(1u << slot);
        for (int later = rank + 1; later < N; later++) {
            int32_t other = order[later];
            if (((valid >> other) & 1u)
                && suppresses(rec + slot * FIELDS, rec + other * FIELDS, area[slot], area[other]))
                valid &= ~(1u << other);
        }
    }
    return keep;
}

/*
 * One NMS call on 32 records of five int32 fields (x, y, a, b, score), timed around the
 * call only. Writes the duration in nanoseconds to *ns and returns keep_mask.
 */
uint32_t nms_scalar(const int32_t *rec, uint32_t present, uint64_t *ns)
{
    uint64_t t0 = now_ns();
    uint32_t keep = nms_sequential(rec, present);
    uint64_t t1 = now_ns();
    *ns = t1 - t0;
    return keep;
}

/* The cost of the two clock reads alone, the floor under every nms_scalar figure. */
uint64_t timer_overhead_ns(int reps)
{
    uint64_t best = UINT64_MAX;
    for (int i = 0; i < reps; i++) {
        uint64_t t0 = now_ns();
        uint64_t t1 = now_ns();
        if (t1 - t0 < best)
            best = t1 - t0;
    }
    return best;
}
