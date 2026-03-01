//! query.zig — H3 offline reverse geocoder, Zig runtime (LKHA0001 format).
//!
//! Reads an LKHA0001 binary blob (produced by builder.py) and answers
//! reverse-geocode queries with no heap allocations.
//!
//! The binary stores raw uint64 H3 cell IDs (res 4/5/6) in a flat sorted array.
//! Each record: [uint64 h3_index][uint32 packed_meta] = 12 bytes.
//! packed_meta bits 31-22: country_id (10 bits)
//!             bits 21-14: state_offset (8 bits)
//!             bits 13- 0: district_offset (14 bits)
//!
//! H3 cell computation (latlng → cell ID) must be performed by the caller.
//! cellToParent is implemented here in pure Zig (bit manipulation only).
//!
//! API (library):
//!   const gc = try ReverseGeocoder.init(data_slice);
//!   const meta = gc.lookupByCell(cell_hi, cell_lo);  // ?u32, null = ocean
//!
//! WASM exports (compiled with -target wasm32-freestanding):
//!   geocoder_init(ptr, len) i32         — 0 on success, -1 on bad file
//!   geocoder_lookup(hi, lo) i32         — packed_meta (u32 as i32) or -1
//!
//! JavaScript caller example:
//!   const cell6Str = h3.latLngToCell(lat, lon, 6);
//!   const cell6Int = BigInt('0x' + cell6Str);
//!   const hi = Number(cell6Int >> 32n) >>> 0;
//!   const lo = Number(cell6Int & 0xFFFFFFFFn) >>> 0;
//!   const packed = wasmExports.geocoder_lookup(hi, lo);  // i32

const std = @import("std");

// ---------------------------------------------------------------------------
// Constants (must match builder.py exactly)
// ---------------------------------------------------------------------------

const MAGIC       = "LKHA0001";
const HDR_SIZE    = 20;   // bytes before the record array
const RECORD_SIZE = 12;   // u64 h3_index + u32 packed_meta

// Resolution fallback ladder used in lookup
const RES_FINE     = 6;
const RES_COARSE   = 5;
const RES_COARSEST = 4;

// H3 index bit-layout constants
const H3_RES_MASK:   u64 = 0x000F_0000_0000_0000;
const H3_RES_OFFSET: u6  = 52;
const H3_PER_DIGIT:  u6  = 3;
const H3_MAX_RES:    u6  = 15;
const H3_DIGIT_MASK: u64 = 7;

// ---------------------------------------------------------------------------
// Read helpers (little-endian, unaligned)
// ---------------------------------------------------------------------------

inline fn rdU32(data: []const u8, off: usize) u32 {
    return std.mem.readInt(u32, data[off..][0..4], .little);
}
inline fn rdU64(data: []const u8, off: usize) u64 {
    return std.mem.readInt(u64, data[off..][0..8], .little);
}

// ---------------------------------------------------------------------------
// H3 cellToParent — pure bit manipulation, no tables needed
// ---------------------------------------------------------------------------

/// Return the H3 cell at `parent_res` that contains `cell`.
/// Valid only when parent_res <= H3_GET_RESOLUTION(cell).
pub fn cellToParent(cell: u64, parent_res: u8) u64 {
    const child_res: u8 = @intCast((cell & H3_RES_MASK) >> H3_RES_OFFSET);
    // Update resolution field
    var parent = (cell & ~H3_RES_MASK) | (@as(u64, parent_res) << H3_RES_OFFSET);
    // Clear digits below parent_res by setting them to 7 (INVALID_DIGIT)
    var r: u8 = parent_res + 1;
    while (r <= child_res) : (r += 1) {
        const shift: u6 = @intCast((H3_MAX_RES - r) * H3_PER_DIGIT);
        parent |= H3_DIGIT_MASK << shift;
    }
    return parent;
}

// ---------------------------------------------------------------------------
// ReverseGeocoder
// ---------------------------------------------------------------------------

pub const ReverseGeocoder = struct {
    data:     []const u8,
    n:        usize,   // number of records
    rec_base: usize,   // byte offset of first record

    pub const Error = error{ BadMagic, BadVersion };

    /// Initialise from a raw LKHA0001 binary slice (no allocation).
    pub fn init(data: []const u8) Error!ReverseGeocoder {
        if (data.len < HDR_SIZE) return Error.BadMagic;
        if (!std.mem.eql(u8, data[0..8], MAGIC)) return Error.BadMagic;
        const version = rdU32(data, 8);
        if (version != 1) return Error.BadVersion;

        // Header: [0:8] magic, [8:12] version, [12:16] N records, [16:20] name_table_off
        const n_records = rdU32(data, 12);
        return .{
            .data     = data,
            .n        = n_records,
            .rec_base = HDR_SIZE,
        };
    }

    /// Reverse-geocode using an H3 cell ID at res RES_FINE (6).
    /// Tries res 6 → 5 → 4 via cellToParent fallback.
    /// Returns packed_meta or null for ocean / unclassified.
    pub fn lookupByCell(self: *const ReverseGeocoder, cell_hi: u32, cell_lo: u32) ?u32 {
        var cell: u64 = (@as(u64, cell_hi) << 32) | cell_lo;

        inline for (.{ RES_FINE, RES_COARSE, RES_COARSEST }) |res| {
            if (res != RES_FINE) cell = cellToParent(cell, res);
            if (self.binarySearch(cell)) |meta| return meta;
        }
        return null;
    }

    // -- Binary search in flat sorted record array ---------------------------

    fn binarySearch(self: *const ReverseGeocoder, cell_id: u64) ?u32 {
        if (self.n == 0) return null;
        var lo: usize = 0;
        var hi: usize = self.n - 1;

        while (lo <= hi) {
            const mid = lo + (hi - lo) / 2;
            const off = self.rec_base + mid * RECORD_SIZE;
            const rec = rdU64(self.data, off);
            if      (rec < cell_id) { lo = mid + 1; }
            else if (rec > cell_id) {
                if (mid == 0) break;
                hi = mid - 1;
            }
            else return rdU32(self.data, off + 8);
        }
        return null;
    }
};

// ---------------------------------------------------------------------------
// packed_meta decoding helper
// ---------------------------------------------------------------------------

pub const Meta = struct {
    country_id:      u10,
    state_offset:    u8,
    district_offset: u14,
};

pub fn unpackMeta(meta: u32) Meta {
    return .{
        .country_id      = @truncate(meta >> 22),
        .state_offset    = @truncate(meta >> 14),
        .district_offset = @truncate(meta),
    };
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

/// Takes H3 cell ID split as (high 32 bits, low 32 bits) at res 6.
/// Returns packed_meta (u32 reinterpreted as i32) or -1 for ocean.
pub export fn geocoder_lookup(hi: u32, lo: u32) i32 {
    const gc = _geocoder orelse return -1;
    return if (gc.lookupByCell(hi, lo)) |meta| @bitCast(meta) else -1;
}

// ---------------------------------------------------------------------------
// Native CLI entry point
// ---------------------------------------------------------------------------

pub fn main() !void {
    const args = try std.process.argsAlloc(std.heap.page_allocator);
    defer std.process.argsFree(std.heap.page_allocator, args);

    if (args.len < 3) {
        std.debug.print("Usage: query <h3_cell_hex> [h3_geo.bin]\n", .{});
        std.debug.print("  h3_cell_hex: H3 res-6 cell ID as a hex string, e.g. 861ec9027ffffff\n", .{});
        return;
    }
    const cell_hex = args[1];
    const path     = if (args.len >= 3) args[2] else "h3_geo.bin";
    const cell_id  = try std.fmt.parseInt(u64, cell_hex, 16);

    const file = try std.fs.cwd().openFile(path, .{});
    defer file.close();
    const data = try file.readToEndAlloc(std.heap.page_allocator, 256 * 1024 * 1024);
    defer std.heap.page_allocator.free(data);

    const gc = try ReverseGeocoder.init(data);
    const hi: u32 = @truncate(cell_id >> 32);
    const lo: u32 = @truncate(cell_id);

    if (gc.lookupByCell(hi, lo)) |meta| {
        const m = unpackMeta(meta);
        std.debug.print("packed_meta: 0x{X:0>8}\n", .{meta});
        std.debug.print("  country_id:      {d}\n", .{m.country_id});
        std.debug.print("  state_offset:    {d}\n", .{m.state_offset});
        std.debug.print("  district_offset: {d}\n", .{m.district_offset});
    } else {
        std.debug.print("ocean / unclassified\n", .{});
    }
}
