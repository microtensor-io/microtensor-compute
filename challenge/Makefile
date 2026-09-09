CC ?= gcc
NVCC ?= nvcc
BUILD ?= build
CFLAGS ?= -O2 -fPIC -Wall -Wextra -std=c11
NVCCFLAGS ?= -O3 -Xcompiler -fPIC
ARCHS ?= -gencode arch=compute_70,code=sm_70 \
         -gencode arch=compute_75,code=sm_75 \
         -gencode arch=compute_80,code=sm_80 \
         -gencode arch=compute_86,code=sm_86 \
         -gencode arch=compute_89,code=sm_89 \
         -gencode arch=compute_90,code=sm_90 \
         -gencode arch=compute_90,code=compute_90

.PHONY: all miner validator check clean

all: miner validator

$(BUILD):
	mkdir -p $(BUILD)

miner: $(BUILD) $(BUILD)/libmtchallenge.so

validator: $(BUILD) $(BUILD)/libmtverify.so

$(BUILD)/libmtchallenge.so: src/challenge.cu src/challenge.h src/core.h
	$(NVCC) $(NVCCFLAGS) $(ARCHS) -shared -o $@ src/challenge.cu

$(BUILD)/libmtverify.so: src/reference.c src/challenge.h src/core.h
	$(CC) $(CFLAGS) -shared -o $@ src/reference.c

$(BUILD)/selfcheck: src/reference.c src/selfcheck.c src/challenge.h src/core.h
	$(CC) $(CFLAGS) -o $@ src/reference.c src/selfcheck.c

check: $(BUILD) $(BUILD)/selfcheck
	$(BUILD)/selfcheck | tee $(BUILD)/selfcheck.out

clean:
	rm -rf $(BUILD)
