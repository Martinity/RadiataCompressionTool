/*
 * radiata_compressor.c — Native C kernels for the tri-Ace PS2 SLZ/SLE compressor.
 *
 * Mirrors the byte-level behaviour of RadiCompressor in
 * core/handlers/compression_container.py, so native output is bit-identical
 * to the pure-Python implementation. Covers LZSS de/compression (modes 1-3)
 * and SLE en/decryption.
 *
 * Built on demand by core/native/compressor_loader.py:
 *   cc -O2 -shared -fPIC -o libradiata_compressor.dylib radiata_compressor.c
 *
 * Functions return the number of bytes written, or -1 on error.
 */

#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>

/* SLE scramble key (matches RadiCompressor.SCRAMBLE_KEY). */
static const uint8_t SLE_KEY[16] = {
    0x66, 0x66, 0x54, 0x42, 0xB3, 0x79, 0xF0, 0xC7,
    0xE7, 0xD5, 0x1E, 0x4B, 0x7B, 0xA4, 0x1C, 0x7D
};

/* ======================================================================
 * Decompression
 *
 *   src      — compressed payload (the bytes AFTER the 16-byte header)
 *   src_len  — length of that payload
 *   dst      — caller-allocated output buffer
 *   dst_cap  — output capacity == header decompressed_size (or a smaller
 *              cap for header-sniffing, mirroring decompress(get_header=True))
 *   mode     — SLZ header mode: 1 = LZSS8, 2 = LZSS8+RLE, 3 = LZSS16
 *
 * Termination: sized (out_pos == dst_cap) OR offset==0 sentinel, matching
 * the Python loop. Negative back-references read 0x00 (ring-buffer zero
 * init) in every mode, exactly as the Python does.
 * ====================================================================== */

static int64_t decompress_8bit(
    const uint8_t *src, size_t src_len,
    uint8_t *dst, size_t dst_cap, int extended_mode)
{
    size_t out_pos = 0;
    size_t in_pos = 0;

    while (out_pos < dst_cap && in_pos < src_len) {
        uint8_t flag_byte = src[in_pos++];

        for (int bit = 0; bit < 8; bit++) {
            if (out_pos >= dst_cap)
                return (int64_t)out_pos;
            if (in_pos >= src_len)
                return (int64_t)out_pos;

            if ((flag_byte >> bit) & 1) {
                /* Literal */
                dst[out_pos++] = src[in_pos++];
            } else {
                /* Back-reference / RLE */
                if (in_pos + 1 >= src_len)
                    return (int64_t)out_pos;

                uint8_t byte1 = src[in_pos];
                uint8_t byte2 = src[in_pos + 1];
                in_pos += 2;

                if (extended_mode && byte2 >= 0xF0) {
                    /* RLE */
                    uint32_t length;
                    uint8_t fill;
                    if (byte2 > 0xF0) {          /* short RLE */
                        length = (byte2 & 0x0F) + 3;
                        fill = byte1;
                    } else {                     /* long RLE (byte2 == 0xF0) */
                        length = (uint32_t)byte1 + 0x13;
                        if (in_pos >= src_len)
                            return (int64_t)out_pos;
                        fill = src[in_pos++];
                    }
                    for (uint32_t i = 0; i < length && out_pos < dst_cap; i++)
                        dst[out_pos++] = fill;
                } else {
                    /* LZSS */
                    uint32_t length_code = (byte2 >> 4) & 0x0F;
                    uint32_t offset = ((uint32_t)(byte2 & 0x0F) << 8) | byte1;
                    uint32_t length = length_code + 3;

                    if (offset == 0)
                        return (int64_t)out_pos;  /* end-of-stream sentinel */

                    int64_t target = (int64_t)out_pos - (int64_t)offset;
                    for (uint32_t k = 0; k < length && out_pos < dst_cap; k++) {
                        int64_t srcidx = target + (int64_t)k;
                        dst[out_pos++] = (srcidx < 0) ? 0x00 : dst[srcidx];
                    }
                }
            }
        }
    }
    return (int64_t)out_pos;
}

static int64_t decompress_16bit(
    const uint8_t *src, size_t src_len,
    uint8_t *dst, size_t dst_cap)
{
    size_t out_pos = 0;
    size_t in_pos = 0;

    while (out_pos < dst_cap && in_pos < src_len) {
        if (in_pos + 1 >= src_len)
            return (int64_t)out_pos;
        uint16_t flags = (uint16_t)src[in_pos] | ((uint16_t)src[in_pos + 1] << 8);
        in_pos += 2;

        for (int bit = 0; bit < 16; bit++) {
            if (out_pos >= dst_cap)
                return (int64_t)out_pos;
            if (in_pos >= src_len)
                return (int64_t)out_pos;

            if ((flags >> bit) & 1) {
                /* Literal halfword */
                dst[out_pos++] = src[in_pos++];
                if (out_pos < dst_cap && in_pos < src_len)
                    dst[out_pos++] = src[in_pos++];
            } else {
                if (in_pos + 1 >= src_len)
                    return (int64_t)out_pos;
                uint8_t byte1 = src[in_pos];
                uint8_t byte2 = src[in_pos + 1];
                in_pos += 2;

                uint32_t length_code = (byte2 >> 4) & 0x0F;
                uint32_t offset = (((uint32_t)(byte2 & 0x0F) << 8) | byte1) * 2;
                uint32_t length = (length_code + 2) * 2;

                if (offset == 0)
                    return (int64_t)out_pos;  /* end-of-stream sentinel */

                int64_t target = (int64_t)out_pos - (int64_t)offset;
                for (uint32_t k = 0; k < length && out_pos < dst_cap; k++) {
                    int64_t srcidx = target + (int64_t)k;
                    dst[out_pos++] = (srcidx < 0) ? 0x00 : dst[srcidx];
                }
            }
        }
    }
    return (int64_t)out_pos;
}

int64_t radiata_decompress(
    const uint8_t *src, size_t src_len,
    uint8_t *dst, size_t dst_cap, int mode)
{
    switch (mode) {
        case 1:  return decompress_8bit(src, src_len, dst, dst_cap, 0);
        case 2:  return decompress_8bit(src, src_len, dst, dst_cap, 1);
        case 3:  return decompress_16bit(src, src_len, dst, dst_cap);
        default: return -1;  /* STORE (0) is handled in Python */
    }
}

/* ======================================================================
 * SLE decryption — in-place on the payload (bytes after the header).
 * Matches RadiCompressor._unscramble_slz_payload:
 *   mod starts at 3; out = ((b - mod) & 0xFF) ^ key[i % 16]; mod += 3.
 * ====================================================================== */

void sle_unscramble(uint8_t *buf, size_t len)
{
    uint8_t mod = 0x03;
    for (size_t i = 0; i < len; i++) {
        uint8_t modified = (uint8_t)(buf[i] - mod);
        buf[i] = (uint8_t)(modified ^ SLE_KEY[i & 0x0F]);
        mod = (uint8_t)(mod + 0x03);
    }
}

/* SLE encryption (inverse of sle_unscramble); matches _scramble_slz_payload. */
void sle_scramble(uint8_t *buf, size_t len)
{
    uint8_t mod = 0x03;
    for (size_t i = 0; i < len; i++) {
        uint8_t scrambled = (uint8_t)(buf[i] ^ SLE_KEY[i & 0x0F]);
        buf[i] = (uint8_t)(scrambled + mod);
        mod = (uint8_t)(mod + 0x03);
    }
}

/* ======================================================================
 * Compression
 *
 * Bit-for-bit port of RadiCompressor.compress's inner loop. The caller
 * (Python wrapper) handles the 16-byte header, SLE scrambling, and the
 * word-align padding of the input (so `src`/`src_len` are already padded,
 * exactly as the Python loop sees self.data/n).
 *
 *   src/src_len — input bytes (word-align-padded by caller for mode 3)
 *   dst/dst_cap — output buffer for the token stream (header excluded)
 *   mode        — 1 = LZSS8, 2 = LZSS8+RLE, 3 = LZSS16
 *
 * Returns the payload length written, or -1 on bad mode / output overflow /
 * allocation failure (caller then falls back to Python).
 * ====================================================================== */

#define HASH_BITS 15
#define HASH_SIZE (1 << HASH_BITS)

typedef struct {
    int window_size;
    int literal_size;
    int flag_bits;
    int length_base;
    int min_match;
    int max_match;
    int rle_enabled;
    int word_aligned;
    int rle_short_min;
    int rle_short_max;
    int rle_long_min;
    int rle_long_max;
} mode_params;

static int get_mode_params(int mode, mode_params *m)
{
    memset(m, 0, sizeof(*m));
    switch (mode) {
        case 1:
            m->window_size = 4096; m->literal_size = 1; m->flag_bits = 8;
            m->length_base = 3; m->min_match = 3; m->max_match = 18;
            return 1;
        case 2:
            m->window_size = 4096; m->literal_size = 1; m->flag_bits = 8;
            m->length_base = 3; m->min_match = 3; m->max_match = 17;
            m->rle_enabled = 1; m->rle_short_min = 4; m->rle_short_max = 18;
            m->rle_long_min = 19; m->rle_long_max = 274;
            return 1;
        case 3:
            m->window_size = 8192; m->literal_size = 2; m->flag_bits = 16;
            m->length_base = 2; m->min_match = 4; m->max_match = 34;
            m->word_aligned = 1;
            return 1;
        default:
            return 0;
    }
}

static int hash3(const uint8_t *d, size_t pos)
{
    return ((d[pos] << 10) ^ (d[pos + 1] << 5) ^ d[pos + 2]) & (HASH_SIZE - 1);
}

static void update_hash(const uint8_t *d, size_t pos, size_t n,
                        int32_t *head, int32_t *prev, int window_size)
{
    if (pos + 2 < n) {
        int h = hash3(d, pos);
        prev[pos % (size_t)window_size] = head[h];
        head[h] = (int32_t)pos;
    }
}

static void find_best_match(const uint8_t *d, size_t pos, size_t n,
                            int32_t *head, int32_t *prev, const mode_params *m,
                            int *out_len, int *out_off)
{
    *out_len = 0;
    *out_off = 0;

    size_t avail = n - pos;
    int max_length = (avail < (size_t)m->max_match) ? (int)avail : m->max_match;
    if (max_length < m->min_match)
        return;

    int step = m->word_aligned ? 2 : 1;
    int max_offset = m->window_size - step;
    int h = hash3(d, pos);
    int64_t candidate = head[h];
    int best_length = 0, best_offset = 0;
    int chain_limit = 64;
    uint8_t first_byte = d[pos];

    while (candidate >= 0 && chain_limit > 0) {
        int64_t offset = (int64_t)pos - candidate;
        if (offset > max_offset)
            break;
        if (offset < step) {
            candidate = prev[candidate % m->window_size];
            chain_limit--;
            continue;
        }
        if (m->word_aligned && (offset % 2 != 0)) {
            candidate = prev[candidate % m->window_size];
            chain_limit--;
            continue;
        }
        if (d[candidate] == first_byte) {
            int ml = 0;
            while (ml < max_length && d[candidate + ml] == d[pos + ml])
                ml++;
            if (m->word_aligned)
                ml -= ml % 2;
            if (ml > best_length) {
                best_length = ml;
                best_offset = (int)offset;
                if (best_length == max_length)
                    break;
            }
        }
        candidate = prev[candidate % m->window_size];
        chain_limit--;
    }

    *out_len = best_length;
    *out_off = best_offset;
}

int64_t radiata_compress(const uint8_t *src, size_t src_len,
                         uint8_t *dst, size_t dst_cap, int mode)
{
    mode_params m;
    if (!get_mode_params(mode, &m))
        return -1;

    const uint8_t *d = src;
    size_t n = src_len;

    int32_t *head = (int32_t *)malloc(sizeof(int32_t) * HASH_SIZE);
    int32_t *prev = (int32_t *)malloc(sizeof(int32_t) * (size_t)m.window_size);
    if (!head || !prev) {
        free(head); free(prev);
        return -1;
    }
    memset(head, 0xFF, sizeof(int32_t) * HASH_SIZE);                  /* all -1 */
    memset(prev, 0xFF, sizeof(int32_t) * (size_t)m.window_size);

    size_t out = 0;
    uint32_t flag_bits = 0;
    int flag_count = 0;
    uint8_t token_buf[64];
    int token_len = 0;

#define BAIL() do { free(head); free(prev); return -1; } while (0)
#define FLUSH() do { \
        size_t need = (size_t)(m.flag_bits == 16 ? 2 : 1) + (size_t)token_len; \
        if (out + need > dst_cap) BAIL(); \
        dst[out++] = (uint8_t)(flag_bits & 0xFF); \
        if (m.flag_bits == 16) dst[out++] = (uint8_t)((flag_bits >> 8) & 0xFF); \
        memcpy(dst + out, token_buf, (size_t)token_len); out += (size_t)token_len; \
        flag_bits = 0; flag_count = 0; token_len = 0; \
    } while (0)

    size_t i = 0;
    while (i < n) {
        if (flag_count == m.flag_bits)
            FLUSH();

        int rle_triggered = 0;
        if (m.rle_enabled) {
            int run_length = 1;
            size_t max_rle_check = i + (size_t)m.rle_long_max;
            if (max_rle_check > n)
                max_rle_check = n;
            for (size_t j = i + 1; j < max_rle_check; j++) {
                if (d[j] == d[i]) run_length++;
                else break;
            }
            if (run_length >= m.rle_short_min) {
                rle_triggered = 1;
                flag_count++;  /* flag bit 0 (reference) */
                uint8_t fill = d[i];
                if (run_length <= m.rle_short_max) {
                    token_buf[token_len++] = fill;
                    token_buf[token_len++] =
                        (uint8_t)(0xF0 | (run_length - m.length_base));
                } else {
                    token_buf[token_len++] = (uint8_t)(run_length - m.rle_long_min);
                    token_buf[token_len++] = 0xF0;
                    token_buf[token_len++] = fill;
                }
                for (int k = 0; k < run_length; k++)
                    update_hash(d, i + (size_t)k, n, head, prev, m.window_size);
                i += (size_t)run_length;
            }
        }

        if (!rle_triggered) {
            int best_length, best_offset;
            find_best_match(d, i, n, head, prev, &m, &best_length, &best_offset);
            if (best_length >= m.min_match) {
                flag_count++;  /* flag bit 0 (reference) */
                int length = best_length, offset = best_offset;
                if (m.word_aligned) { length /= 2; offset /= 2; }
                int length_code = length - m.length_base;
                token_buf[token_len++] = (uint8_t)(offset & 0xFF);
                token_buf[token_len++] =
                    (uint8_t)(((length_code & 0x0F) << 4) | ((offset >> 8) & 0x0F));
                for (int k = 0; k < best_length; k++)
                    update_hash(d, i + (size_t)k, n, head, prev, m.window_size);
                i += (size_t)best_length;
            } else {
                flag_bits |= (uint32_t)1 << flag_count;  /* flag bit 1 (literal) */
                flag_count++;
                int lit = m.literal_size;
                if (i + (size_t)lit > n)
                    lit = (int)(n - i);
                for (int k = 0; k < lit; k++)
                    token_buf[token_len++] = d[i + (size_t)k];
                update_hash(d, i, n, head, prev, m.window_size);
                i += (size_t)lit;
            }
        }
    }

    if (flag_count > 0)
        FLUSH();

    /* End-of-stream sentinel: 0x00 flag [+0x00 for 16-bit] + offset/len 0. */
    {
        size_t need = (size_t)(m.flag_bits == 16 ? 2 : 1) + 2;
        if (out + need > dst_cap) BAIL();
        dst[out++] = 0x00;
        if (m.flag_bits == 16) dst[out++] = 0x00;
        dst[out++] = 0x00;
        dst[out++] = 0x00;
    }

#undef FLUSH
#undef BAIL
    free(head);
    free(prev);
    return (int64_t)out;
}
