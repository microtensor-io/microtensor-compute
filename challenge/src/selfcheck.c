#include <inttypes.h>
#include <stdio.h>

#include "challenge.h"

int main(void) {
    const uint64_t cases[][4] = {
        {1, 2, 64, 2},
        {0xdeadbeefULL, 0x1234ULL, 96, 3},
        {7, 11, 128, 1},
    };
    for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
        uint64_t digest = 0;
        int code = mt_challenge_solve(cases[i][0], cases[i][1], (uint32_t)cases[i][2],
                                      (uint32_t)cases[i][3], &digest);
        if (code != MT_OK) {
            fprintf(stderr, "solve failed: %d %s\n", code, mt_challenge_last_error());
            return 1;
        }
        printf("%" PRIu64 " %" PRIu64 " %" PRIu64 " %" PRIu64 " %016" PRIx64 "\n", cases[i][0],
               cases[i][1], cases[i][2], cases[i][3], digest);
    }
    return 0;
}
