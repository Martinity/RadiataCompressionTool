/**
 * Cross-platform block device OS I/O API wrapper built specifically for PhysicalHandler.
 * Ensures that memory is sector aligned with VirtualAlloc/posix_memalign.
 * uint64_t used to ensure 64-bit alignment on all platforms.
 * Reads are stateless and thread-safe meaning that the python context manager overhead on every
 * physical read is avoided.
 */
#define _GNU_SOURCE
#include <stdlib.h>
#include <stdint.h>
#include <string.h>

#if defined(_WIN32)
    #include <windows.h>
#else
    #include <sys/types.h>
    #include <fcntl.h>
    #include <sys/ioctl.h>
    #include <unistd.h>
    #if defined(__APPLE__)
        #include <sys/disk.h>
    #else
        #include <linux/fs.h>
    #endif
#endif

#define SECTOR_SIZE 2048 // Since ISO 9660 alignment is 2048 bytes, can initialize NativeBlockDevice with a custom alignment

#if defined(_WIN32)
    #define EXPORT __declspec(dllexport)
#else
    #define EXPORT __attribute__((visibility("default")))
#endif

// Python handle struct
typedef struct {
#if defined(_WIN32)
    HANDLE handle;
#else
    int fd;
#endif
    int64_t size;
    uint32_t sector_size;
} NativeBlockDevice;

//------------------------------ Allocation ---------------------------------//

EXPORT void* alloc_aligned_buffer(size_t size, uint32_t alignment) {
    if (alignment == 0) alignment = SECTOR_SIZE;
#if defined(_WIN32)
    // Windows uses VirtualAlloc calculates page allocation from size
    // Inherintly aligned to page size
    return VirtualAlloc(NULL, size, MEM_COMMIT | MEM_RESERVE, PAGE_READWRITE);
#else
    void* ptr = NULL;
    if (posix_memalign(&ptr, alignment, size) != 0) return NULL;
    return ptr;
#endif
}

EXPORT void free_aligned_buffer(void* ptr) {
    if (!ptr) return;
#if defined(_WIN32)
    VirtualFree(ptr, 0, MEM_RELEASE);
#else
    free(ptr);
#endif
}

//------------------------------ Lifecycle ---------------------------------//

EXPORT NativeBlockDevice* open_device(const char* path, uint32_t sector_size) {
    if (!path) return NULL;
    NativeBlockDevice* device = (NativeBlockDevice*)calloc(1, sizeof(*device));
    if (!device) return NULL;
    if (sector_size == 0) sector_size = SECTOR_SIZE;
    device->sector_size = sector_size;

#if defined(_WIN32)
    device->handle = CreateFileA(
        path,
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE,
        NULL,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL | FILE_FLAG_OVERLAPPED, // Potential for FILE_FLAG_NO_BUFFERING
        NULL
    );
    if (device->handle == INVALID_HANDLE_VALUE) {
        free(device);
        return NULL;
    }
    LARGE_INTEGER li;
    if (GetFileSizeEx(device->handle, &li)) device->size = li.QuadPart;
    else device->size = 0;
#else
    device->fd = open(path, O_RDONLY | O_CLOEXEC);
    if (device->fd < 0) {
        free(device);
        return NULL;
    }
// Try to request block count/size, fallback to lseek
#if defined(__APPLE__)
    uint64_t block_count = 0;
    uint32_t block_size = 0;
    if (ioctl(device->fd, DKIOCGETBLOCKCOUNT, &block_count) == 0 &&
        ioctl(device->fd, DKIOCGETBLOCKSIZE,  &block_size)  == 0) {
        device->size = (int64_t)block_count * block_size;
    } else {
        off_t sz = lseek(device->fd, 0, SEEK_END);
        device->size = (sz >= 0) ? (int64_t)sz : 0;
        lseek(device->fd, 0, SEEK_SET);
    }
#else
    uint64_t bytes = 0;
    if (ioctl(device->fd, BLKGETSIZE64, &bytes) == 0) {
        device->size = (int64_t)bytes;
    } else {
        off_t sz = lseek(device->fd, 0, SEEK_END);
        device->size = (sz >= 0) ? (int64_t)sz : 0;
        lseek(device->fd, 0, SEEK_SET);
    }
#endif
#endif
    return device;
}

EXPORT void close_device(NativeBlockDevice* device) {
    if (!device) return;
#if defined(_WIN32)
    if (device->handle != INVALID_HANDLE_VALUE) CloseHandle(device->handle);
#else
    if (device->fd >= 0) close(device->fd);
#endif
    free(device);
}

//--------------------------------- Queries ----------------------------------------//

EXPORT int64_t get_device_size(NativeBlockDevice* device) {
    return device ? device->size : 0;
}

EXPORT uint32_t get_sector_size(NativeBlockDevice* device) {
    return device ? device->sector_size : SECTOR_SIZE;
}

//--------------------------------- Stateless Read ----------------------------------------//

// defines async read events for windows
#if defined(_WIN32)
__declspec(thread) HANDLE tls_read_event = NULL;
#endif

EXPORT int64_t read_sectors(NativeBlockDevice* device, uint64_t byte_offset, void* out_buffer, size_t byte_count) {
    if (!device || !out_buffer || byte_count == 0) return -1;

#if defined(_WIN32)
    // Create a read event if one doesn't exist
    if (!tls_read_event) {
        tls_read_event = CreateEvent(NULL, TRUE, FALSE, NULL);
        if (!tls_read_event) return -1;
    }
    // OVERLAPPED for atomic stateless windows reads
    // This part is trickier than the pread unix version since overlapped is synchronous without an event
    OVERLAPPED overlapped;
    memset(&overlapped, 0, sizeof(overlapped));
    overlapped.Offset     = (DWORD)(byte_offset & 0xFFFFFFFFu);
    overlapped.OffsetHigh = (DWORD)(byte_offset >> 32);
    overlapped.hEvent     = tls_read_event;

    DWORD bytes_read = 0;
    if (byte_count > MAXDWORD) return -1;
    ResetEvent(tls_read_event);
    BOOL ok = ReadFile(device->handle, out_buffer, (DWORD)byte_count, &bytes_read, &overlapped);
    if (!ok) {
        DWORD err = GetLastError();
        // Catch errors
        if (err != ERROR_IO_PENDING) return -1;
        if (!GetOverlappedResult(device->handle, &overlapped, &bytes_read, TRUE)) return -1;
    }
    // Do NOT close the event handle here, handle persists for the thread lifetime
    return (int64_t)bytes_read;
#else
    // pread for atomic stateless unix reads
    ssize_t result = pread(device->fd, out_buffer, (size_t)byte_count, (off_t)byte_offset);
    return (result < 0) ? -1 : (int64_t)result;
#endif
}

//---------------------------------- Convenience -----------------------------------//

// Accepts a pre-allocated aligned buffer to remove malloc/free overhead accross os levels
EXPORT int64_t read_slice(
    NativeBlockDevice* device,
    uint64_t offset,
    size_t size,
    void* aligned_buffer,    // Passed from python
    size_t buffer_capacity,  // Safety bounds
    size_t* skip)
{
    if (!device || !aligned_buffer || !skip || size == 0) return -1;
    // Calculate the requested slice from the sector read
    uint32_t sector_size    = device->sector_size ? device->sector_size : SECTOR_SIZE;
    uint64_t mask           = sector_size - 1;
    uint64_t aligned_offset = offset & ~mask;
    size_t aligned_skip     = (size_t)(offset & mask);
    size_t aligned_size     = (size + aligned_skip + mask) & ~(mask);

    if (aligned_offset >= (uint64_t)device->size) {
        *skip       = 0;
        return 0;
    }
    // Bounds clamping
    if ((aligned_offset >= device->size) || (aligned_size > (size_t)(device->size - aligned_offset)))
        aligned_size = (size_t)(device->size - aligned_offset);
    if (aligned_offset + aligned_size > (uint64_t)device->size)
        aligned_size = (size_t)(device->size - aligned_offset);
    // Capacity check
    if (aligned_size > buffer_capacity) return -1;
    int64_t n = read_sectors(device, aligned_offset, aligned_buffer, aligned_size);
    if (n < 0) return -1;
    *skip = aligned_skip;
    size_t available = 0;
    if ((size_t)n > aligned_skip) available = (size_t)n - aligned_skip;
    if (available > size) available = size;
    return (int64_t)available;
}
