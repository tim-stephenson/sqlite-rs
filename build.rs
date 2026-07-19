use std::env;
use std::path::{Path, PathBuf};
use std::process::Command;

fn manifest_dir() -> PathBuf {
    PathBuf::from(env::var("CARGO_MANIFEST_DIR").unwrap())
}

fn target_os() -> String {
    env::var("CARGO_CFG_TARGET_OS").unwrap()
}

/// Shared library filename for `name` on the target OS, e.g. "sqlite3" -> "libsqlite3.dylib".
fn shared_lib_name(name: &str) -> String {
    match target_os().as_str() {
        "macos" => format!("lib{name}.dylib"),
        "windows" => format!("{name}.dll"),
        _ => format!("lib{name}.so"),
    }
}

/// Compile `vendor/sqlite/sqlite3.c` and link it into a standalone shared
/// `libsqlite3` placed in `python/sqlite_rs/`. This is the one SQLite
/// instance the Python clone module and this crate both dynamically link
/// against, so a live `sqlite3*` connection can be passed safely between
/// them (see the plan doc / native/sqlite_rs_shim.c for why).
fn build_libsqlite3(sqlite_dir: &Path, out_dir: &Path) -> PathBuf {
    let objects = cc::Build::new()
        .file(sqlite_dir.join("sqlite3.c"))
        .include(sqlite_dir)
        .pic(true)
        .warnings(false)
        .define("SQLITE_ENABLE_FTS5", None)
        .define("SQLITE_ENABLE_RTREE", None)
        .define("SQLITE_ENABLE_COLUMN_METADATA", None)
        .define("SQLITE_ENABLE_DBSTAT_VTAB", None)
        .compile_intermediates();
    assert!(!objects.is_empty(), "cc produced no object files for sqlite3.c");

    let lib_name = shared_lib_name("sqlite3");
    let out_path = out_dir.join(&lib_name);

    let mut cmd: Command = cc::Build::new().get_compiler().to_command();
    cmd.args(&objects);
    match target_os().as_str() {
        "macos" => {
            cmd.arg("-dynamiclib");
            cmd.arg("-install_name").arg(format!("@rpath/{lib_name}"));
        }
        "windows" => {
            panic!(
                "Windows build of libsqlite3 is not yet supported (see plan follow-ups); \
                 phase 1 targets macOS + Linux x86_64 only"
            );
        }
        _ => {
            cmd.arg("-shared");
            cmd.arg(format!("-Wl,-soname,{lib_name}"));
        }
    }
    cmd.arg("-o").arg(&out_path);

    let status = cmd.status().expect("failed to invoke linker for libsqlite3");
    assert!(status.success(), "linking {lib_name} failed: {cmd:?}");

    out_path
}

/// Path of the host Python interpreter that pyo3 itself is building against,
/// so the clone module below targets exactly the same interpreter as `_core`
/// rather than re-discovering one independently.
fn python_executable() -> String {
    pyo3_build_config::get()
        .executable()
        .map(str::to_owned)
        .or_else(|| env::var("PYO3_PYTHON").ok())
        .unwrap_or_else(|| "python3".to_string())
}

/// Run `python -c <code>` and return trimmed stdout.
fn python_query(python: &str, code: &str) -> String {
    let output = Command::new(python)
        .args(["-c", code])
        .output()
        .unwrap_or_else(|e| panic!("failed to run `{python}`: {e}"));
    assert!(
        output.status.success(),
        "`{python} -c {code}` failed: {}",
        String::from_utf8_lossy(&output.stderr)
    );
    String::from_utf8(output.stdout).unwrap().trim().to_string()
}

const CLONE_MODULE_SOURCES: &[&str] = &[
    "blob.c",
    "connection.c",
    "cursor.c",
    "microprotocols.c",
    "module.c",
    "prepare_protocol.c",
    "row.c",
    "statement.c",
    "util.c",
];

/// Compile CPython's vendored (unmodified) `Modules/_sqlite/*.c` into a
/// `_sqlite3<EXT_SUFFIX>` extension module in `python/sqlite_rs/`, dynamically
/// linked against the `libsqlite3` built above instead of whatever SQLite the
/// host Python was built with.
///
/// The file must be named `_sqlite3<EXT_SUFFIX>` (not something distinctive
/// of this project) because module.c hardcodes its init symbol as
/// `PyInit__sqlite3` (see `vendor/cpython/Modules/_sqlite/module.c`), and
/// CPython's import machinery for extension modules looks up
/// `PyInit_<last dotted component>`. Living inside `sqlite_rs/` gives it the
/// distinct dotted name `sqlite_rs._sqlite3`, so it never shadows or
/// conflicts with the stdlib's top-level `_sqlite3`.
fn build_sqlite_clone_module(
    cpython_sqlite_dir: &Path,
    sqlite_dir: &Path,
    libsqlite3_dir: &Path,
    out_dir: &Path,
) -> PathBuf {
    let python = python_executable();
    let include_dir = python_query(&python, "import sysconfig; print(sysconfig.get_path('include'))");
    let internal_dir = Path::new(&include_dir).join("internal");
    let ext_suffix = python_query(
        &python,
        "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))",
    );

    let sources: Vec<PathBuf> = CLONE_MODULE_SOURCES
        .iter()
        .map(|f| cpython_sqlite_dir.join(f))
        .collect();

    let objects = cc::Build::new()
        .files(&sources)
        .include(cpython_sqlite_dir)
        .include(sqlite_dir)
        .include(&include_dir)
        .include(&internal_dir)
        .pic(true)
        .warnings(false)
        .compile_intermediates();
    assert!(!objects.is_empty(), "cc produced no object files for the sqlite3 clone module");

    let out_path = out_dir.join(format!("_sqlite3{ext_suffix}"));

    let mut cmd: Command = cc::Build::new().get_compiler().to_command();
    cmd.args(&objects);
    cmd.arg(format!("-L{}", libsqlite3_dir.display()));
    cmd.arg("-lsqlite3");
    match target_os().as_str() {
        "macos" => {
            cmd.arg("-dynamiclib");
            cmd.arg("-undefined").arg("dynamic_lookup");
            cmd.arg("-Wl,-rpath,@loader_path");
        }
        "windows" => {
            panic!(
                "Windows build of the sqlite3 clone module is not yet supported \
                 (see plan follow-ups); phase 1 targets macOS + Linux x86_64 only"
            );
        }
        _ => {
            cmd.arg("-shared");
            cmd.arg("-Wl,-rpath,$ORIGIN");
        }
    }
    cmd.arg("-o").arg(&out_path);

    let status = cmd.status().expect("failed to invoke linker for the sqlite3 clone module");
    assert!(status.success(), "linking the sqlite3 clone module failed: {cmd:?}");

    out_path
}

/// Compile `native/sqlite_rs_shim.c` (first-party, not vendored) and
/// statically link it into `_core` itself, then point `_core`'s own dynamic
/// linking at the `libsqlite3` built above so it shares the same SQLite
/// instance as the clone module.
fn compile_shim_and_link_core(native_dir: &Path, cpython_sqlite_dir: &Path, sqlite_dir: &Path, libsqlite3_dir: &Path) {
    let python = python_executable();
    let include_dir = python_query(&python, "import sysconfig; print(sysconfig.get_path('include'))");

    cc::Build::new()
        .file(native_dir.join("sqlite_rs_shim.c"))
        .include(native_dir)
        .include(cpython_sqlite_dir)
        .include(sqlite_dir)
        .include(&include_dir)
        .pic(true)
        .warnings(false)
        .compile("sqlite_rs_shim");

    println!("cargo:rustc-link-search=native={}", libsqlite3_dir.display());
    println!("cargo:rustc-link-lib=dylib=sqlite3");
    match target_os().as_str() {
        "macos" => println!("cargo:rustc-cdylib-link-arg=-Wl,-rpath,@loader_path"),
        "windows" => panic!(
            "Windows build of _core's sqlite3 link step is not yet supported \
             (see plan follow-ups); phase 1 targets macOS + Linux x86_64 only"
        ),
        _ => println!("cargo:rustc-cdylib-link-arg=-Wl,-rpath,$ORIGIN"),
    }
}

fn main() {
    let manifest_dir = manifest_dir();
    let sqlite_dir = manifest_dir.join("vendor/sqlite");
    let cpython_sqlite_dir = manifest_dir.join("vendor/cpython/Modules/_sqlite");
    let native_dir = manifest_dir.join("native");
    let python_pkg_dir = manifest_dir.join("python/sqlite_rs");

    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={}", sqlite_dir.display());
    println!("cargo:rerun-if-changed={}", cpython_sqlite_dir.display());
    println!("cargo:rerun-if-changed={}", native_dir.display());

    let libsqlite3 = build_libsqlite3(&sqlite_dir, &python_pkg_dir);
    println!("cargo:warning=built {}", libsqlite3.display());

    let clone_module =
        build_sqlite_clone_module(&cpython_sqlite_dir, &sqlite_dir, &python_pkg_dir, &python_pkg_dir);
    println!("cargo:warning=built {}", clone_module.display());

    compile_shim_and_link_core(&native_dir, &cpython_sqlite_dir, &sqlite_dir, &python_pkg_dir);
}
