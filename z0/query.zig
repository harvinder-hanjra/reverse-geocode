//! query.zig — Z0 offline reverse geocoder, Zig runtime.
//!
//! Reads an RGEO0002 binary blob (produced by builder.zig) and answers
//! reverse-geocode queries with no heap allocations.
//!
//! API (library):
//!   const gc = try ReverseGeocoder.init(data_slice);
//!   const admin_id = gc.lookup(lat, lon);   // ?u16, null = ocean
//!
//! WASM exports (compiled with -target wasm32-freestanding):
//!   geocoder_init(ptr, len) i32   — 0 on success, -1 on bad file
//!   geocoder_lookup(lat, lon) i32 — admin_id (0–65533) or -1 (ocean)

const std = @import("std");

// ---------------------------------------------------------------------------
// Constants (must match builder.zig exactly)
// ---------------------------------------------------------------------------

const MAGIC           = "RGEO0002";
const GRID_COLS: usize = 1440;
const GRID_ROWS: usize = 720;
const GRID_CELL_DEG: f64 = 0.25;
const SENTINEL_BOUNDARY: u16 = 0xFFFF;
const SENTINEL_OCEAN:    u16 = 0xFFFE;
const BLOCK_SIZE:    usize = 64;
const BLOCK_RECORDS: usize = 10;
const RECORD_SIZE:   usize = 6;   // u32 morton + u16 admin_id
const MORTON_STEPS:  f64   = 4096.0;

// ---------------------------------------------------------------------------
// Morton code
// ---------------------------------------------------------------------------

fn mortonSpread(v: u32) u32 {
    var x: u32 = v & 0xFFF;
    x = (x | (x << 8)) & 0x00FF00FF;
    x = (x | (x << 4)) & 0x0F0F0F0F;
    x = (x | (x << 2)) & 0x33333333;
    x = (x | (x << 1)) & 0x55555555;
    return x;
}

fn mortonEncode(lat: f64, lon: f64) u32 {
    const lq: u32 = @intFromFloat(@min(MORTON_STEPS - 1, @max(0.0, (lat + 90.0)  / 180.0 * MORTON_STEPS)));
    const aq: u32 = @intFromFloat(@min(MORTON_STEPS - 1, @max(0.0, (lon + 180.0) / 360.0 * MORTON_STEPS)));
    return mortonSpread(lq) | (mortonSpread(aq) << 1);
}

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

    bitmap_off:          usize,
    rank_off:            usize,
    values_off:          usize,
    land_cell_count:     usize,
    morton_block_off:    usize,
    morton_dir_off:      usize,
    morton_record_count: usize,
    morton_block_count:  usize,
    admin_off:           usize,

    pub const Error = error{ BadMagic, BadVersion };

    /// Initialise from a raw RGEO0002 binary slice (no allocation).
    pub fn init(data: []const u8) Error!ReverseGeocoder {
        if (data.len < 64) return Error.BadMagic;
        if (!std.mem.eql(u8, data[0..8], MAGIC)) return Error.BadMagic;
        const version = rdU32(data, 8);
        if (version != 1) return Error.BadVersion;

        // Header layout (see builder.zig writeBinaryFile):
        //  [0:8]  magic        [8:12]  version       [12:16] timestamp
        //  [16:20] bitmap_off  [20:24] rank_off       [24:28] values_off
        //  [28:32] land_cells  [32:36] morton_blk_off [36:40] morton_dir_off
        //  [40:44] m_rec_count [44:48] m_blk_count    [48:52] admin_off
        //  [52:56] name_off    [56:64] reserved
        return .{
            .data                = data,
            .bitmap_off          = rdU32(data, 16),
            .rank_off            = rdU32(data, 20),
            .values_off          = rdU32(data, 24),
            .land_cell_count     = rdU32(data, 28),
            .morton_block_off    = rdU32(data, 32),
            .morton_dir_off      = rdU32(data, 36),
            .morton_record_count = rdU32(data, 40),
            .morton_block_count  = rdU32(data, 44),
            .admin_off           = rdU32(data, 48),
        };
    }

    /// Reverse-geocode (lat, lon). Returns admin_id or null for ocean.
    pub fn lookup(self: *const ReverseGeocoder, lat: f64, lon: f64) ?u16 {
        const la = std.math.clamp(lat, -90.0,  90.0);
        const lo = std.math.clamp(lon, -180.0, 180.0);

        // Layer 0: 0.25° coarse grid
        const col: usize = @min(GRID_COLS - 1,
            @as(usize, @intFromFloat(@max(0.0, (lo + 180.0) / GRID_CELL_DEG))));
        const row: usize = @min(GRID_ROWS - 1,
            @as(usize, @intFromFloat(@max(0.0, (90.0 - la) / GRID_CELL_DEG))));
        const idx = row * GRID_COLS + col;

        // Bitmap test
        const byte_idx = self.bitmap_off + idx / 8;
        if ((self.data[byte_idx] >> @intCast(idx & 7)) & 1 == 0) return null;

        // Rank → values[]
        const rank  = self.bitmapRank(idx);
        const value = rdU16(self.data, self.values_off + rank * 2);

        if (value == SENTINEL_OCEAN)    return null;
        if (value != SENTINEL_BOUNDARY) return value;  // interior fast path

        // Layer 1: Morton boundary table
        const m = mortonEncode(la, lo);
        if (self.blockSearch(m))        |aid| return aid;
        if (self.blockSearch(m >> 2))   |aid| return aid;
        return null;
    }

    // -- Bitmap rank (O(1) using file's precomputed rank table) --------------

    fn bitmapRank(self: *const ReverseGeocoder, idx: usize) usize {
        const block_idx  = idx / 512;
        var rank: usize  = rdU32(self.data, self.rank_off + block_idx * 4);

        const block_byte = self.bitmap_off + block_idx * 64;  // 512 bits / 8
        const local_bit  = idx % 512;
        const full_bytes = local_bit / 8;
        const rem_bits:u3 = @intCast(local_bit % 8);

        for (0..full_bytes) |i| {
            rank += @popCount(self.data[block_byte + i]);
        }
        if (rem_bits > 0) {
            const mask: u8 = (@as(u8, 1) << rem_bits) - 1;
            rank += @popCount(self.data[block_byte + full_bytes] & mask);
        }
        return rank;
    }

    // -- Block binary search --------------------------------------------------

    fn blockSearch(self: *const ReverseGeocoder, m: u32) ?u16 {
        const n = self.morton_block_count;
        if (n == 0) return null;

        // upper_bound: find first block where dir[block] > m, then go back one
        var lo: usize = 0;
        var hi: usize = n;
        while (lo < hi) {
            const mid = lo + (hi - lo) / 2;
            if (rdU32(self.data, self.morton_dir_off + mid * 4) <= m) {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        if (lo == 0) return null;  // all blocks start above m
        const block_idx = lo - 1;

        const block_start = self.morton_block_off + block_idx * BLOCK_SIZE;
        for (0..BLOCK_RECORDS) |i| {
            const rec_off = block_start + i * RECORD_SIZE;
            const rec_m   = rdU32(self.data, rec_off);
            if (rec_m > m) break;
            if (rec_m == m) return rdU16(self.data, rec_off + 4);
        }
        return null;
    }
};

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
pub export fn geocoder_lookup(lat: f64, lon: f64) i32 {
    const gc = _geocoder orelse return -1;
    return if (gc.lookup(lat, lon)) |id| @intCast(id) else -1;
}

// ---------------------------------------------------------------------------
// Native CLI entry point
// ---------------------------------------------------------------------------

pub fn main() !void {
    const args = try std.process.argsAlloc(std.heap.page_allocator);
    defer std.process.argsFree(std.heap.page_allocator, args);

    if (args.len < 3) {
        std.debug.print("Usage: query <lat> <lon> [z0_geo.bin]\n", .{});
        return;
    }
    const lat = try std.fmt.parseFloat(f64, args[1]);
    const lon = try std.fmt.parseFloat(f64, args[2]);
    const path = if (args.len >= 4) args[3] else "z0_geo.bin";

    const file = try std.fs.cwd().openFile(path, .{});
    defer file.close();
    const data = try file.readToEndAlloc(std.heap.page_allocator, 256 * 1024 * 1024);
    defer std.heap.page_allocator.free(data);

    const gc = try ReverseGeocoder.init(data);
    if (gc.lookup(lat, lon)) |id| {
        std.debug.print("admin_id: {d}\n", .{id});
    } else {
        std.debug.print("ocean / unclassified\n", .{});
    }
}
