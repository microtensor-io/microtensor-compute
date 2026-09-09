#include <cuda_runtime.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "challenge.h"
#include "core.h"

#define TILE 16

static char last_error[256];

static void set_error(const char *message) {
    strncpy(last_error, message, sizeof(last_error) - 1);
    last_error[sizeof(last_error) - 1] = 0;
}

static int fail_cuda(const char *what, cudaError_t err) {
    snprintf(last_error, sizeof(last_error), "%s: %s", what, cudaGetErrorString(err));
    return MT_ERR_DEVICE;
}

__global__ void fill_kernel(uint32_t *out, uint64_t state, uint64_t cells) {
    uint64_t k = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    if (k < cells) {
        out[k] = mt_element(state, k);
    }
}

__global__ void matmul_kernel(const uint32_t *a, uint32_t *c, uint32_t n) {
    __shared__ uint32_t left[TILE][TILE];
    __shared__ uint32_t right[TILE][TILE];
    uint32_t row = blockIdx.y * TILE + threadIdx.y;
    uint32_t col = blockIdx.x * TILE + threadIdx.x;
    uint32_t acc = 0;
    for (uint32_t t = 0; t < n; t += TILE) {
        uint32_t lc = t + threadIdx.x;
        uint32_t rr = t + threadIdx.y;
        left[threadIdx.y][threadIdx.x] = (row < n && lc < n) ? a[(size_t)row * n + lc] : 0u;
        right[threadIdx.y][threadIdx.x] = (rr < n && col < n) ? a[(size_t)rr * n + col] : 0u;
        __syncthreads();
        for (int k = 0; k < TILE; k++) {
            acc += left[threadIdx.y][k] * right[k][threadIdx.x];
        }
        __syncthreads();
    }
    if (row < n && col < n) {
        c[(size_t)row * n + col] = acc;
    }
}

__global__ void row_hash_kernel(const uint32_t *a, uint64_t *rows, uint32_t n) {
    uint32_t i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) {
        uint64_t h = MT_FNV_OFFSET;
        for (uint32_t j = 0; j < n; j++) {
            h = mt_fnv_u32(h, a[(size_t)i * n + j]);
        }
        rows[i] = h;
    }
}

__global__ void stream_kernel(const uint32_t *src, uint32_t *dst, uint64_t words, uint32_t salt) {
    uint64_t k = (uint64_t)blockIdx.x * blockDim.x + threadIdx.x;
    uint64_t stride = (uint64_t)gridDim.x * blockDim.x;
    for (; k < words; k += stride) {
        dst[k] = src[k] ^ (uint32_t)k ^ salt;
    }
}

int mt_challenge_version(void) { return MT_CHALLENGE_VERSION; }

const char *mt_challenge_build(void) { return "miner-cuda"; }

const char *mt_challenge_last_error(void) { return last_error; }

int mt_challenge_solve(uint64_t seed, uint64_t cipher, uint32_t n, uint32_t rounds,
                       uint64_t *digest) {
    if (digest == NULL || n < 8 || n > 4096 || rounds < 1 || rounds > 16) {
        set_error("bad arguments");
        return MT_ERR_ARGS;
    }
    uint64_t cells = (uint64_t)n * n;
    uint32_t *a = NULL;
    uint32_t *c = NULL;
    uint64_t *rows = NULL;
    uint64_t *host_rows = NULL;
    cudaError_t err;
    int code = MT_OK;

    if ((err = cudaMalloc(&a, cells * sizeof(uint32_t))) != cudaSuccess) {
        return fail_cuda("cudaMalloc a", err);
    }
    if ((err = cudaMalloc(&c, cells * sizeof(uint32_t))) != cudaSuccess) {
        code = fail_cuda("cudaMalloc c", err);
        goto done;
    }
    if ((err = cudaMalloc(&rows, (size_t)n * sizeof(uint64_t))) != cudaSuccess) {
        code = fail_cuda("cudaMalloc rows", err);
        goto done;
    }
    host_rows = (uint64_t *)malloc((size_t)n * sizeof(uint64_t));
    if (host_rows == NULL) {
        set_error("host allocation failed");
        code = MT_ERR_ALLOC;
        goto done;
    }

    {
        uint64_t state = mt_seed_state(seed, cipher);
        unsigned int blocks = (unsigned int)((cells + 255) / 256);
        fill_kernel<<<blocks, 256>>>(a, state, cells);
        if ((err = cudaGetLastError()) != cudaSuccess) {
            code = fail_cuda("fill launch", err);
            goto done;
        }
    }
    {
        dim3 block(TILE, TILE);
        dim3 grid((n + TILE - 1) / TILE, (n + TILE - 1) / TILE);
        for (uint32_t r = 0; r < rounds; r++) {
            matmul_kernel<<<grid, block>>>(a, c, n);
            if ((err = cudaGetLastError()) != cudaSuccess) {
                code = fail_cuda("matmul launch", err);
                goto done;
            }
            uint32_t *swap = a;
            a = c;
            c = swap;
        }
    }
    row_hash_kernel<<<(n + 127) / 128, 128>>>(a, rows, n);
    if ((err = cudaGetLastError()) != cudaSuccess) {
        code = fail_cuda("row hash launch", err);
        goto done;
    }
    if ((err = cudaMemcpy(host_rows, rows, (size_t)n * sizeof(uint64_t), cudaMemcpyDeviceToHost)) !=
        cudaSuccess) {
        code = fail_cuda("cudaMemcpy rows", err);
        goto done;
    }
    *digest = mt_fold_digest(host_rows, n, seed, cipher, rounds);

done:
    if (a) cudaFree(a);
    if (c) cudaFree(c);
    if (rows) cudaFree(rows);
    free(host_rows);
    return code;
}

int mt_challenge_benchmark(uint32_t n, uint32_t iterations, double *gops, double *gbps,
                           double *elapsed_ms) {
    if (gops == NULL || gbps == NULL || elapsed_ms == NULL || n < 64 || n > 8192 ||
        iterations < 1 || iterations > 1024) {
        set_error("bad arguments");
        return MT_ERR_ARGS;
    }
    uint64_t cells = (uint64_t)n * n;
    uint32_t *a = NULL;
    uint32_t *c = NULL;
    uint32_t *src = NULL;
    uint32_t *dst = NULL;
    cudaEvent_t start, mid, stop;
    cudaError_t err;
    int code = MT_OK;
    uint32_t probe = 0;
    size_t free_bytes = 0, total_bytes = 0;
    uint64_t words = 64ULL * 1024 * 1024;
    float matmul_ms = 0.0f, stream_ms = 0.0f;
    const int passes = 8;

    if ((err = cudaEventCreate(&start)) != cudaSuccess) return fail_cuda("event", err);
    if ((err = cudaEventCreate(&mid)) != cudaSuccess) return fail_cuda("event", err);
    if ((err = cudaEventCreate(&stop)) != cudaSuccess) return fail_cuda("event", err);
    if ((err = cudaMemGetInfo(&free_bytes, &total_bytes)) != cudaSuccess) {
        return fail_cuda("cudaMemGetInfo", err);
    }
    while (words > 1024 * 1024 &&
           (words * 2 * sizeof(uint32_t) + cells * 2 * sizeof(uint32_t)) > free_bytes / 2) {
        words /= 2;
    }

    if ((err = cudaMalloc(&a, cells * sizeof(uint32_t))) != cudaSuccess) {
        return fail_cuda("cudaMalloc a", err);
    }
    if ((err = cudaMalloc(&c, cells * sizeof(uint32_t))) != cudaSuccess) {
        code = fail_cuda("cudaMalloc c", err);
        goto done;
    }
    if ((err = cudaMalloc(&src, words * sizeof(uint32_t))) != cudaSuccess) {
        code = fail_cuda("cudaMalloc src", err);
        goto done;
    }
    if ((err = cudaMalloc(&dst, words * sizeof(uint32_t))) != cudaSuccess) {
        code = fail_cuda("cudaMalloc dst", err);
        goto done;
    }

    fill_kernel<<<(unsigned int)((cells + 255) / 256), 256>>>(a, mt_seed_state(n, iterations), cells);
    fill_kernel<<<(unsigned int)((words + 255) / 256), 256>>>(src, mt_seed_state(words, 1), words);
    if ((err = cudaDeviceSynchronize()) != cudaSuccess) {
        code = fail_cuda("warm up", err);
        goto done;
    }

    {
        dim3 block(TILE, TILE);
        dim3 grid((n + TILE - 1) / TILE, (n + TILE - 1) / TILE);
        matmul_kernel<<<grid, block>>>(a, c, n);
        if ((err = cudaDeviceSynchronize()) != cudaSuccess) {
            code = fail_cuda("matmul warm up", err);
            goto done;
        }
        cudaEventRecord(start);
        for (uint32_t i = 0; i < iterations; i++) {
            matmul_kernel<<<grid, block>>>(a, c, n);
            if ((err = cudaMemcpy(&probe, c + (i % cells), sizeof(uint32_t),
                                  cudaMemcpyDeviceToHost)) != cudaSuccess) {
                code = fail_cuda("probe copy", err);
                goto done;
            }
        }
        cudaEventRecord(mid);
        for (int p = 0; p < passes; p++) {
            stream_kernel<<<4096, 256>>>(src, dst, words, (uint32_t)p);
            if ((err = cudaMemcpy(&probe, dst + (p % words), sizeof(uint32_t),
                                  cudaMemcpyDeviceToHost)) != cudaSuccess) {
                code = fail_cuda("probe copy", err);
                goto done;
            }
        }
        cudaEventRecord(stop);
        if ((err = cudaEventSynchronize(stop)) != cudaSuccess) {
            code = fail_cuda("event sync", err);
            goto done;
        }
        cudaEventElapsedTime(&matmul_ms, start, mid);
        cudaEventElapsedTime(&stream_ms, mid, stop);
    }

    {
        double ops = 2.0 * (double)n * (double)n * (double)n * (double)iterations;
        double bytes = 2.0 * (double)words * sizeof(uint32_t) * (double)passes;
        *gops = matmul_ms > 0.0f ? ops / ((double)matmul_ms / 1000.0) / 1e9 : 0.0;
        *gbps = stream_ms > 0.0f ? bytes / ((double)stream_ms / 1000.0) / 1e9 : 0.0;
        *elapsed_ms = (double)matmul_ms + (double)stream_ms;
    }

done:
    if (a) cudaFree(a);
    if (c) cudaFree(c);
    if (src) cudaFree(src);
    if (dst) cudaFree(dst);
    cudaEventDestroy(start);
    cudaEventDestroy(mid);
    cudaEventDestroy(stop);
    (void)probe;
    return code;
}

int mt_challenge_release(void) {
    cudaError_t err = cudaDeviceReset();
    if (err != cudaSuccess) return fail_cuda("cudaDeviceReset", err);
    return MT_OK;
}
