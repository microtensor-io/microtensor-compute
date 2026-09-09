# challenge

The GPU challenge and its throughput benchmark, compiled rather than shipped as source.

Two builds of one library:

- `make miner` produces `build/libmtchallenge.so` with nvcc. It solves the challenge on the GPU and runs the throughput benchmark in the same process. The validator uploads this file into a session and runs it there.
- `make validator` produces `build/libmtverify.so` with a C compiler only. It solves the challenge on the CPU so the validator can verify an answer in process without paying the benchmark cost. The benchmark entry point returns unavailable.

`make check` builds the CPU reference into a self check binary and prints digests for fixed parameters. `scripts/challenge_vector.py --compare` recomputes them in Python and must agree.

The challenge is an integer matrix square, repeated `rounds` times, over a matrix filled from a counter based generator seeded by the seed and the cipher. Every operation wraps modulo 2^32, so the digest is bit exact on any hardware. Row hashes are folded in order with FNV-1a together with the seed, the cipher, the size and the round count.

The benchmark reports integer matrix throughput in GOPS and streaming bandwidth in GB/s. Every iteration ends with a four byte device to host copy, so a GPU reached over a network pays a round trip per iteration and shows it.
