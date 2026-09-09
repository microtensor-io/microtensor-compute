#include <stdlib.h>
#include <string.h>

#include "challenge.h"
#include "core.h"

static char last_error[256];

static void set_error(const char *message) {
    strncpy(last_error, message, sizeof(last_error) - 1);
    last_error[sizeof(last_error) - 1] = 0;
}

int mt_challenge_version(void) { return MT_CHALLENGE_VERSION; }

const char *mt_challenge_build(void) { return "validator-reference"; }

const char *mt_challenge_last_error(void) { return last_error; }

int mt_challenge_solve(uint64_t seed, uint64_t cipher, uint32_t n, uint32_t rounds,
                       uint64_t *digest) {
    if (digest == NULL || n < 8 || n > 4096 || rounds < 1 || rounds > 16) {
        set_error("bad arguments");
        return MT_ERR_ARGS;
    }
    size_t cells = (size_t)n * (size_t)n;
    uint32_t *a = (uint32_t *)malloc(cells * sizeof(uint32_t));
    uint32_t *c = (uint32_t *)malloc(cells * sizeof(uint32_t));
    uint64_t *rows = (uint64_t *)malloc((size_t)n * sizeof(uint64_t));
    if (a == NULL || c == NULL || rows == NULL) {
        free(a);
        free(c);
        free(rows);
        set_error("allocation failed");
        return MT_ERR_ALLOC;
    }
    uint64_t state = mt_seed_state(seed, cipher);
    for (size_t k = 0; k < cells; k++) {
        a[k] = mt_element(state, (uint64_t)k);
    }
    for (uint32_t r = 0; r < rounds; r++) {
        for (uint32_t i = 0; i < n; i++) {
            for (uint32_t j = 0; j < n; j++) {
                uint32_t acc = 0;
                for (uint32_t k = 0; k < n; k++) {
                    acc += a[(size_t)i * n + k] * a[(size_t)k * n + j];
                }
                c[(size_t)i * n + j] = acc;
            }
        }
        uint32_t *swap = a;
        a = c;
        c = swap;
    }
    for (uint32_t i = 0; i < n; i++) {
        uint64_t h = MT_FNV_OFFSET;
        for (uint32_t j = 0; j < n; j++) {
            h = mt_fnv_u32(h, a[(size_t)i * n + j]);
        }
        rows[i] = h;
    }
    *digest = mt_fold_digest(rows, n, seed, cipher, rounds);
    free(a);
    free(c);
    free(rows);
    return MT_OK;
}

int mt_challenge_benchmark(uint32_t n, uint32_t iterations, double *gops, double *gbps,
                           double *elapsed_ms) {
    (void)n;
    (void)iterations;
    (void)gops;
    (void)gbps;
    (void)elapsed_ms;
    set_error("benchmark is only available in the miner build");
    return MT_ERR_UNAVAILABLE;
}

int mt_challenge_release(void) { return MT_OK; }
