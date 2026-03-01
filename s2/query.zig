//! query.zig — S2/H3 offline reverse geocoder, Zig runtime (RGEO0001 format).
//!
//! Reads an RGEO0001 binary blob (produced by builder.py) and answers
//! reverse-geocode queries with no heap allocations.
//!
//! The binary uses H3 cells encoded as compact uint32 values in a two-level
//! block binary search structure:
//!   L10 (coarse, res-6):  enc6 = (h3_uint64 >> 27) & 0x1FFFFFF
//!   L12 (fine,   res-7):  enc7 = (h3_uint64 >> 24) & 0xFFFFFFF
//!
//! H3 cell computation (latlng → cell ID) must be performed by the caller.
//! Typical usage from Zig with the h3 C library, or from JavaScript with h3-js.
//!
//! API (library):
//!   const gc = try ReverseGeocoder.init(data_slice);
//!   const admin_id = gc.lookupByEnc(enc6, enc7);  // ?u16, null = ocean
//!
//! WASM exports (compiled with -target wasm32-freestanding):
//!   geocoder_init(ptr, len) i32           — 0 on success, -1 on bad file
//!   geocoder_lookup(enc6, enc7) i32       — admin_id (0–65533) or -1 (ocean)
//!
//! JavaScript caller example:
//!   const cell7 = h3.latLngToCell(lat, lon, 7);
//!   const cell6 = h3.cellToParent(cell7, 6);
//!   const enc7  = encodeRes7(h3.cellToParent(cell7, 6));  // see encoding below
//!   const enc6  = encodeRes6(cell6);
//!   // encodeRes6(cellStr) = ((parseInt(cellStr,16) >> 27n) & 0x1FFFFFFn) | 0
//!   // encodeRes7(cellStr) = ((parseInt(cellStr,16) >> 24n) & 0xFFFFFFFn) | 0

const std = @import("std");

// ---------------------------------------------------------------------------
// Constants (must match builder.py exactly)
// ---------------------------------------------------------------------------

const MAGIC              = "RGEO0001";
const RECORDS_PER_BLOCK: usize = 10;
const RECORD_SIZE:        usize = 6;   // u32 cell_id + u16 admin_id
const BLOCK_SIZE:         usize = 64;
const HEADER_SIZE:        usize = 64;

// ---------------------------------------------------------------------------
// Read helpers (little-endian, unaligned)
// ---------------------------------------------------------------------------

inline fn rdU16(data: []const u8, off: usize) u16 {
    return std.mem.readInt(u16, data[off..][0..2], .little);
}
inline fn rdU32(data: []const u8, off: usize) u32 {
    return std.mem.readInt(u32, data[off..][0..4], .little);
}

// ---------------------------------------------------------------------------
// ReverseGeocoder
// ---------------------------------------------------------------------------

pub const ReverseGeocoder = struct {
    data: []const u8,

    l10_blocks_off:  usize,
    l10_dir_off:     usize,
    l10_block_count: usize,
    l12_blocks_off:  usize,
    l12_dir_off:     usize,
    l12_block_count: usize,

    pub const Error = error{ BadMagic, BadVersion };

    /// Initialise from a raw RGEO0001 binary slice (no allocation).
    pub fn init(data: []const u8) Error!ReverseGeocoder {
        if (data.len < HEADER_SIZE) return Error.BadMagic;
        if (!std.mem.eql(u8, data[0..8], MAGIC)) return Error.BadMagic;
        const version = rdU32(data, 8);
        if (version != 1) return Error.BadVersion;

        // Header layout (see builder.py write_binary_file):
        //  [0:8]  magic       [8:12]  version
        //  [12:16] l10_count  [16:20] l12_count
        //  [20:24] l10_dir_off [24:28] l12_dir_off
        //  [28:32] admin_off  [32:36] name_off
        //  [36:64] padding
        const l10_count    = rdU32(data, 12);
        const l12_count    = rdU32(data, 16);
        const l10_dir_off  = rdU32(data, 20);
        const l12_dir_off  = rdU32(data, 24);

        const l10_block_count = (l10_count + RECORDS_PER_BLOCK - 1) / RECORDS_PER_BLOCK;
        const l12_block_count = (l12_count + RECORDS_PER_BLOCK - 1) / RECORDS_PER_BLOCK;

        // L10 block array: starts right after the 64-byte header
        const l10_blocks_off = HEADER_SIZE;
        // L12 block array: starts right after the L10 directory
        const l12_blocks_off = l10_dir_off + l10_block_count * 4;

        return .{
            .data            = data,
            .l10_blocks_off  = l10_blocks_off,
            .l10_dir_off     = l10_dir_off,
            .l10_block_count = l10_block_count,
            .l12_blocks_off  = l12_blocks_off,
            .l12_dir_off     = l12_dir_off,
            .l12_block_count = l12_block_count,
        };
    }

    /// Reverse-geocode using pre-encoded H3 cell keys.
    ///   enc6 = (h3_cell_uint64 >> 27) & 0x1FFFFFF   (H3 res-6, 25 bits)
    ///   enc7 = (h3_cell_uint64 >> 24) & 0xFFFFFFF   (H3 res-7, 28 bits)
    /// Returns admin_id or null for ocean / unclassified.
    pub fn lookupByEnc(self: *const ReverseGeocoder, enc6: u32, enc7: u32) ?u16 {
        // L10 coarse lookup (res-6 cell)
        if (blockSearch(self.data, enc6, self.l10_blocks_off, self.l10_dir_off, self.l10_block_count)) |id|
            return id;
        // L12 fine lookup (res-7 cell)
        return blockSearch(self.data, enc7, self.l12_blocks_off, self.l12_dir_off, self.l12_block_count);
    }
};

// -- Block binary search (shared by both tables) -----------------------------

fn blockSearch(data: []const u8, cell_id: u32, blocks_off: usize, dir_off: usize, block_count: usize) ?u16 {
    if (block_count == 0) return null;

    // upper_bound: first block with dir[block] > cell_id
    var lo: usize = 0;
    var hi: usize = block_count;
    while (lo < hi) {
        const mid = lo + (hi - lo) / 2;
        if (rdU32(data, dir_off + mid * 4) <= cell_id) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if (lo == 0) return null;
    const block_idx = lo - 1;

    const block_start = blocks_off + block_idx * BLOCK_SIZE;
    for (0..RECORDS_PER_BLOCK) |i| {
        const rec_off  = block_start + i * RECORD_SIZE;
        const rec_cell = rdU32(data, rec_off);
        if (rec_cell == 0 and i > 0) break;  // padding sentinel
        if (rec_cell > cell_id) break;
        if (rec_cell == cell_id) return rdU16(data, rec_off + 4);
    }
    return null;
}

// ---------------------------------------------------------------------------
// H3 encoding helpers (mirror of builder.py / s2.js)
// ---------------------------------------------------------------------------

/// Encode an H3 res-6 cell integer to a compact uint32 key.
/// Equivalent to JavaScript: ((BigInt(h3Int) >> 27n) & 0x1FFFFFFn) | 0
pub fn encodeRes6(h3_int: u64) u32 {
    return @truncate((h3_int >> 27) & 0x1FFFFFF);
}

/// Encode an H3 res-7 cell integer to a compact uint32 key.
/// Equivalent to JavaScript: ((BigInt(h3Int) >> 24n) & 0xFFFFFFFn) | 0
pub fn encodeRes7(h3_int: u64) u32 {
    return @truncate((h3_int >> 24) & 0xFFFFFFF);
}

// ---------------------------------------------------------------------------
// WASM / flat-C exports
// ---------------------------------------------------------------------------

var _geocoder: ?ReverseGeocoder = null;

pub export fn geocoder_init(ptr: usize, len: usize) i32 {
    const data: [*]const u8 = @ptrFromInt(ptr);
    _geocoder = ReverseGeocoder.init(data[0..len]) catch return -1;
    return 0;
}

/// Returns admin_id (0–65533) or -1 for ocean / error.
/// enc6 and enc7 must be pre-computed by the JS caller using h3-js.
pub export fn geocoder_lookup(enc6: u32, enc7: u32) i32 {
    const gc = _geocoder orelse return -1;
    return if (gc.lookupByEnc(enc6, enc7)) |id| @intCast(id) else -1;
}

// ---------------------------------------------------------------------------
// Native CLI entry point
// ---------------------------------------------------------------------------

pub fn main() !void {
    const args = try std.process.argsAlloc(std.heap.page_allocator);
    defer std.process.argsFree(std.heap.page_allocator, args);

    if (args.len < 5) {
        std.debug.print("Usage: query <enc6> <enc7> [s2_geo.bin]\n", .{});
        std.debug.print("  enc6/enc7 are pre-encoded H3 cell IDs (decimal integers)\n", .{});
        return;
    }
    const enc6 = try std.fmt.parseInt(u32, args[1], 0);
    const enc7 = try std.fmt.parseInt(u32, args[2], 0);
    const path = if (args.len >= 4) args[3] else "s2_geo.bin";

    const file = try std.fs.cwd().openFile(path, .{});
    defer file.close();
    const data = try file.readToEndAlloc(std.heap.page_allocator, 256 * 1024 * 1024);
    defer std.heap.page_allocator.free(data);

    const gc = try ReverseGeocoder.init(data);
    if (gc.lookupByEnc(enc6, enc7)) |id| {
        std.debug.print("admin_id: {d}\n", .{id});
    } else {
        std.debug.print("ocean / unclassified\n", .{});
    }
}
