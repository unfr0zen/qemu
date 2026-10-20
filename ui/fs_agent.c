/* Live filesystem access on the gen 3's dedicated UART2. */
#define _DEFAULT_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <poll.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/mman.h>
#include <unistd.h>

#define LIMIT (16 * 1024 * 1024)
#define CHUNK 1024

static int input, output;
static volatile uint32_t *uart;

static int receive(void *buf, size_t len)
{
    char *p = buf;
    if (uart) {
        unsigned int idle = 0;
        while (len) {
            if (uart[0x98 / 4] & 1) {
                *p++ = uart[0];
                len--;
                idle = 0;
            } else {
                if (++idle >= 10000) return -1;
                usleep(1000);
            }
        }
        return 0;
    }
    while (len) {
        struct pollfd fd = { .fd = input, .events = POLLIN };
        int ready;
        do { ready = poll(&fd, 1, 10000); } while (ready < 0 && errno == EINTR);
        if (ready <= 0) return -1;
        ssize_t n = read(input, p, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        len -= n;
    }
    return 0;
}

static int send_bytes(const void *buf, size_t len)
{
    const char *p = buf;
    if (uart) {
        while (len--) uart[0x40 / 4] = (unsigned char)*p++;
        return 0;
    }
    while (len) {
        ssize_t n = write(output, p, len);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return -1;
        p += n;
        len -= n;
    }
    return 0;
}

static uint32_t number(const unsigned char *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | p[3];
}

static void put_number(unsigned char *p, uint32_t n)
{
    p[0] = n >> 24; p[1] = n >> 16; p[2] = n >> 8; p[3] = n;
}

static void reply(int error, const void *data, size_t len)
{
    unsigned char header[12] = "NFS1";
    if (error) { data = strerror(error); len = strlen(data); }
    put_number(header + 4, error);
    put_number(header + 8, len);
    if (!send_bytes(header, sizeof(header))) send_bytes(data, len);
}

static int read_file(const char *path, unsigned char *data, size_t *len)
{
    struct stat st;
    int fd = open(path, O_RDONLY | O_NONBLOCK);
    if (fd < 0) return errno;
    int error = 0;
    if (fstat(fd, &st)) error = errno;
    else if (!S_ISREG(st.st_mode)) error = EINVAL;
    *len = 0;
    while (!error) {
        ssize_t n = read(fd, data + *len, LIMIT + 1 - *len);
        if (n < 0 && errno == EINTR) continue;
        if (n < 0) { error = errno; break; }
        if (!n) break;
        *len += n;
        if (*len > LIMIT) error = EFBIG;
    }
    close(fd);
    return error;
}

static int write_file(const char *path, const unsigned char *data,
                      size_t len, uint32_t mode)
{
    char temp[4096], parent[4096];
    struct stat st;
    int exists = lstat(path, &st) == 0;
    if (exists && !S_ISREG(st.st_mode)) return EINVAL;
    if (snprintf(temp, sizeof(temp), "%s.mcp-XXXXXX", path) >= (int)sizeof(temp))
        return ENAMETOOLONG;
    strcpy(parent, path);
    char *slash = strrchr(parent, '/');
    slash[slash == parent ? 1 : 0] = '\0';
    int dir = open(parent, O_RDONLY | O_DIRECTORY);
    if (dir < 0) return errno;
    int fd = mkstemp(temp);
    if (fd < 0) { int error = errno; close(dir); return error; }
    int error = 0;
    size_t done = 0;
    while (done < len) {
        ssize_t n = write(fd, data + done, len - done);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { error = n < 0 ? errno : EIO; break; }
        done += n;
    }
    if (!error && exists && fchown(fd, st.st_uid, st.st_gid)) error = errno;
    if (!error && fchmod(fd, mode == UINT32_MAX ?
                        (exists ? st.st_mode & 0777 : 0644) : mode)) error = errno;
    if (!error && fsync(fd)) error = errno;
    if (close(fd) && !error) error = errno;
    if (!error && rename(temp, path)) error = errno;
    if (!error && fsync(dir)) error = errno;
    unlink(temp);
    close(dir);
    return error;
}

int main(int argc, char **argv)
{
    int stdio = argc == 3 && !strcmp(argv[1], "--stdio");
    const char *mode = argc == 2 ? argv[1] : stdio ? argv[2] : "";
    if (strcmp(mode, "ro") && strcmp(mode, "rw")) return 2;
    int writable = !strcmp(mode, "rw");
    input = STDIN_FILENO; output = STDOUT_FILENO;
    if (!stdio) {
        int fd = open("/dev/mem", O_RDWR | O_SYNC);
        if (fd < 0) { perror("/dev/mem"); return 1; }
        uart = mmap(NULL, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0x021e8000);
        close(fd);
        if (uart == MAP_FAILED) { perror("UART2 mmap"); return 1; }
        /* The stock UART2 driver uses SDMA, which this QEMU does not model.
         * Poll the dedicated UART directly; filesystem calls use the kernel. */
        uart[0x80 / 4] = 1;
        uart[0x84 / 4] = 7;
    }
    unsigned char *data = malloc(LIMIT + 1);
    if (!data) return 1;
    for (;;) {
        unsigned char header[16], byte;
        unsigned int matched = 0;
        while (matched < 4) {
            if (receive(&byte, 1)) {
                if (stdio) return 0;
                matched = 0;
                continue;
            }
            matched = byte == (unsigned char)"NFS1"[matched] ? matched + 1 :
                      byte == 'N' ? 1 : 0;
        }
        if (receive(header, sizeof(header))) continue;
        uint32_t op = number(header), plen = number(header + 4);
        uint32_t len = number(header + 8), permissions = number(header + 12);
        if (!plen || plen >= 4000 || len > LIMIT) { reply(EINVAL, NULL, 0); continue; }
        char path[4096];
        if (receive(path, plen)) continue;
        path[plen] = '\0';
        size_t done = 0;
        while (done < len) {
            size_t count = len - done < CHUNK ? len - done : CHUNK;
            if (receive(data + done, count) || send_bytes(".", 1)) break;
            done += count;
        }
        if (done != len) continue;
        int error = 0;
        size_t result_len = 0;
        if (path[0] != '/' || memchr(path, '\0', plen)) error = EINVAL;
        else if (op == 1 && !len) error = read_file(path, data, &result_len);
        else if (op == 2) {
            if (!writable) error = EROFS;
            else if (permissions != UINT32_MAX && permissions > 0777) error = EINVAL;
            else error = write_file(path, data, len, permissions);
        } else error = EINVAL;
        reply(error, data, result_len);
    }
}
