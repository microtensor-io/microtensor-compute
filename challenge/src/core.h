#ifndef MT_CHALLENGE_CORE_H
#define MT_CHALLENGE_CORE_H

#include <stdint.h>

#ifdef __CUDACC__
#define MT_HD __host__ __device__
#else
#define MT_HD
#endif

#define MT_CHALLENGE_VERSION 1
#define MT_GOLDEN 0x9E3779B97F4A7C15ULL
#define MT_FNV_OFFSET 0xcbf29ce484222325ULL
#define MT_FNV_PRIME 0x100000001b3ULL

MT_HD static inline uint64_t mt_mix64(uint64_t z) {
    z = (z ^ (z >> 30)) * 0xBF58476D1CE4E5B9ULL;
    z = (z ^ (z >> 27)) * 0x94D049BB133111EBULL;
    return z ^ (z >> 31);
}

MT_HD static inline uint64_t mt_seed_state(uint64_t seed, uint64_t cipher) {
    return mt_mix64(seed ^ 0xA5A5A5A5A5A5A5A5ULL) + mt_mix64(cipher * MT_GOLDEN);
}

MT_HD static inline uint32_t mt_element(uint64_t state, uint64_t index) {
    return (uint32_t)(mt_mix64(state + (index + 1ULL) * MT_GOLDEN) & 0xFFFFFFFFULL);
}

MT_HD static inline uint64_t mt_fnv_u32(uint64_t hash, uint32_t value) {
    for (int i = 0; i < 4; i++) {
        hash ^= (uint64_t)((value >> (8 * i)) & 0xFFu);
        hash *= MT_FNV_PRIME;
    }
    return hash;
}

MT_HD static inline uint64_t mt_fnv_u64(uint64_t hash, uint64_t value) {
    for (int i = 0; i < 8; i++) {
        hash ^= (value >> (8 * i)) & 0xFFULL;
        hash *= MT_FNV_PRIME;
    }
    return hash;
}

static inline uint64_t mt_fold_digest(const uint64_t *row_hashes, uint32_t n, uint64_t seed,
                                      uint64_t cipher, uint32_t rounds) {
    uint64_t digest = MT_FNV_OFFSET;
    for (uint32_t i = 0; i < n; i++) {
        digest = mt_fnv_u64(digest, row_hashes[i]);
    }
    digest = mt_fnv_u64(digest, seed);
    digest = mt_fnv_u64(digest, cipher);
    digest = mt_fnv_u32(digest, n);
    digest = mt_fnv_u32(digest, rounds);
    return digest;
}

#endif
