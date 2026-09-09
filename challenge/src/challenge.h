#ifndef MT_CHALLENGE_H
#define MT_CHALLENGE_H

#include <stdint.h>

#define MT_OK 0
#define MT_ERR_ARGS -1
#define MT_ERR_ALLOC -2
#define MT_ERR_LAUNCH -3
#define MT_ERR_UNAVAILABLE -4
#define MT_ERR_DEVICE -5

#ifdef __cplusplus
extern "C" {
#endif

int mt_challenge_version(void);
const char *mt_challenge_build(void);
const char *mt_challenge_last_error(void);
int mt_challenge_solve(uint64_t seed, uint64_t cipher, uint32_t n, uint32_t rounds,
                       uint64_t *digest);
int mt_challenge_benchmark(uint32_t n, uint32_t iterations, double *gops, double *gbps,
                           double *elapsed_ms);
int mt_challenge_release(void);

#ifdef __cplusplus
}
#endif

#endif
