// wasm_entry.zig — WASM module root for h3 geocoder.
// Force-references the exported functions from query.zig so they survive DCE.
// The `main()` in query.zig is unreferenced and not compiled for this target.
const q = @import("query.zig");

comptime {
    _ = &q.geocoder_init;
    _ = &q.geocoder_lookup;
}
