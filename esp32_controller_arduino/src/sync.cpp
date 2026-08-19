// sync.cpp - Cross-task locking for state shared by loopTask and async_tcp

#include "sync.h"

SemaphoreHandle_t srtMutex = nullptr;
volatile uint32_t srtLockTimeouts = 0;

void srtSyncInit() {
    if (!srtMutex) {
        srtMutex = xSemaphoreCreateRecursiveMutex();
    }
}
