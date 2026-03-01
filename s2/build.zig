const std = @import("std");

pub fn build(b: *std.Build) void {
    const target   = b.standardTargetOptions(.{});
    const optimize = b.standardOptimizeOption(.{ .preferred_optimize_mode = .ReleaseFast });

    // ── Native query CLI ─────────────────────────────────────────────────────
    const query_exe = b.addExecutable(.{
        .name = "query",
        .root_module = b.createModule(.{
            .root_source_file = b.path("query.zig"),
            .target   = target,
            .optimize = optimize,
        }),
    });
    b.installArtifact(query_exe);

    const run_cmd = b.addRunArtifact(query_exe);
    run_cmd.step.dependOn(b.getInstallStep());
    if (b.args) |run_args| run_cmd.addArgs(run_args);
    const run_step = b.step("run", "Run the query CLI");
    run_step.dependOn(&run_cmd.step);

    // ── WASM geocoder module ─────────────────────────────────────────────────
    // Exports: geocoder_init(ptr, len) i32, geocoder_lookup(enc6, enc7) i32
    // JS caller must provide H3-encoded cell IDs (use h3-js on the JS side).
    const wasm_target = b.resolveTargetQuery(.{
        .cpu_arch = .wasm32,
        .os_tag   = .freestanding,
    });
    const wasm = b.addExecutable(.{
        .name = "geocoder",
        .root_module = b.createModule(.{
            .root_source_file = b.path("wasm_entry.zig"),
            .target   = wasm_target,
            .optimize = .ReleaseSmall,
        }),
    });
    wasm.rdynamic = true;
    wasm.entry = .disabled;
    const wasm_install = b.addInstallArtifact(wasm, .{});
    const wasm_step = b.step("wasm", "Build WASM geocoder module");
    wasm_step.dependOn(&wasm_install.step);
}
