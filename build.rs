use std::env;
use std::fs;
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

/// Run `python -c <code>`, returning trimmed stdout on success or `None` on
/// any failure (unlike `python_query` below, this must not panic -- it's
/// used to probe candidate interpreters that may not actually be valid, and,
/// for cross builds, may not even be executable at all -- see
/// `find_cross_python_executable`).
fn try_python_version(python: &Path) -> Option<String> {
    let output = Command::new(python)
        .args(["-c", "import sysconfig; print(sysconfig.get_python_version())"])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    String::from_utf8(output.stdout).ok().map(|s| s.trim().to_string())
}

/// Locate the target interpreter directly on disk when `pyo3_build_config`
/// has no runnable `executable()` for it -- i.e. when cross compiling, which
/// per PyO3/maturin's `PythonInterpreter::runnable` doc means "the target
/// interpreter isn't runnable, and its executable is empty". This is exactly
/// what happens for the musllinux Docker jobs: maturin still sets
/// `PYO3_PYTHON`, but to its own glibc bootstrap interpreter (used only for
/// pyo3's internal purposes), not to a usable target interpreter -- trusting
/// it here would (and did) point this build's `-I` flags at the wrong
/// Python's headers while compiling with a musl cross-compiler.
///
/// manylinux/musllinux Docker images install every supported target
/// interpreter under `/opt/python` (CPython, e.g. `cp310-cp310/`) and
/// `/opt/pypy` (PyPy, e.g. `pp311-pypy311_pp73/`), so locate the one
/// matching pyo3_build_config's resolved target version/implementation.
/// Confirm it actually reports that version when it *can* be executed (e.g.
/// same-architecture cross builds, or foreign ones running under QEMU) --
/// but when a candidate can't be executed at all (a genuine foreign-arch
/// binary with no emulation registered, e.g. cross-compiling from a fast
/// native `*-cross` image), trust the directory-naming convention itself
/// rather than treat "can't execute" as "wrong version". That convention is
/// already exact enough on its own: `dir_prefix` encodes the full version
/// tag (e.g. "cp311-cp311"), so a matching directory can't belong to a
/// different interpreter version.
fn find_cross_python_executable() -> Option<PathBuf> {
    let abi = pyo3_build_config::get().target_abi();
    let version = abi.version();
    let is_pypy = matches!(abi.implementation(), pyo3_build_config::PythonImplementation::PyPy);
    let threaded = abi.kind().is_free_threaded();

    let (root, dir_prefix, exe_name) = if is_pypy {
        (
            "/opt/pypy",
            format!("pp{}{}-", version.major, version.minor),
            format!("pypy{}.{}", version.major, version.minor),
        )
    } else {
        let tag = format!("cp{}{}", version.major, version.minor);
        let suffix = if threaded { "t" } else { "" };
        (
            "/opt/python",
            format!("{tag}-{tag}{suffix}"),
            format!("python{}.{}{suffix}", version.major, version.minor),
        )
    };

    let expected_version = format!("{}.{}", version.major, version.minor);
    let entries = fs::read_dir(root).ok()?;
    for entry in entries.filter_map(|e| e.ok()) {
        if !entry.file_name().to_string_lossy().starts_with(&dir_prefix) {
            continue;
        }
        let candidate = entry.path().join("bin").join(&exe_name);
        if !candidate.is_file() {
            continue;
        }
        match try_python_version(&candidate) {
            Some(v) if v == expected_version => return Some(candidate),
            Some(_) => continue,
            None => return Some(candidate),
        }
    }
    None
}

/// Where a target Python interpreter came from, since that determines
/// whether it's safe to execute it to answer `sysconfig` questions.
enum PythonSource {
    /// Runnable on this machine right now -- a native build's own
    /// interpreter (which may be a venv, so its headers can live anywhere;
    /// `sysconfig` must be asked, not guessed), or a same-architecture cross
    /// build. Safe to execute.
    Runnable(String),
    /// Found via `find_cross_python_executable`'s manylinux/musllinux
    /// `/opt/python/<tag>/bin/<exe>` layout, but not necessarily executable
    /// -- may be a genuine foreign-architecture binary with no QEMU/binfmt
    /// registered. Its `sysconfig` data must be derived from the fixed
    /// layout instead of executed.
    CrossFound(PathBuf),
}

/// The target Python interpreter that pyo3 itself is building against, so
/// the clone module below targets exactly the same interpreter as `_core`
/// rather than re-discovering one independently.
fn resolve_python() -> PythonSource {
    if let Some(exe) = pyo3_build_config::get().executable() {
        return PythonSource::Runnable(exe.to_owned());
    }
    if let Some(path) = find_cross_python_executable() {
        return PythonSource::CrossFound(path);
    }
    if let Ok(exe) = env::var("PYO3_PYTHON") {
        return PythonSource::Runnable(exe);
    }
    PythonSource::Runnable("python3".to_string())
}

/// `sysconfig.get_path('include')` for `python`, without executing anything
/// for a `CrossFound` interpreter that may not be executable at all: CPython
/// (and PyPy, which mirrors it for C-extension compatibility) always installs
/// headers at `<prefix>/include/<exe_name>` under the POSIX sysconfig scheme
/// (e.g. `bin/python3.11` <-> `include/python3.11/`, `bin/python3.13t` <->
/// `include/python3.13t/`) -- the exact same naming `find_cross_python_executable`
/// already trusts to build `exe_name` in the first place.
fn include_dir(python: &PythonSource) -> PathBuf {
    match python {
        PythonSource::Runnable(exe) => {
            PathBuf::from(python_query(exe, "import sysconfig; print(sysconfig.get_path('include'))"))
        }
        PythonSource::CrossFound(path) => {
            let exe_name = path.file_name().unwrap_or_else(|| panic!("no filename in {}", path.display()));
            let prefix = path
                .parent() // .../bin
                .and_then(Path::parent) // .../<tag>
                .unwrap_or_else(|| panic!("unexpected cross python path layout: {}", path.display()));
            prefix.join("include").join(exe_name)
        }
    }
}

/// `sysconfig.get_config_var('EXT_SUFFIX')` for `python`, without executing
/// anything for a `CrossFound` interpreter. CPython's import machinery
/// always accepts a bare, untagged `.so` as a fallback suffix (see
/// `Python/dynload_shlib.c`'s `_PyImport_DynLoadFiletab`, always ending in an
/// untagged entry) specifically so extension modules don't have to be built
/// with the exact platform/ABI tag -- reconstructing that tag by hand per
/// target (distinguishing e.g. "ppc64le" from its actual multiarch triplet
/// "powerpc64le", glibc vs musl, free-threaded "t") would be exactly the kind
/// of hand-rolled cross-platform logic this is trying to avoid. This branch
/// only ever runs for Linux cross builds (macOS/Windows always have a
/// runnable interpreter), so `.so` is always correct here.
fn ext_suffix(python: &PythonSource) -> String {
    match python {
        PythonSource::Runnable(exe) => {
            python_query(exe, "import sysconfig; print(sysconfig.get_config_var('EXT_SUFFIX'))")
        }
        PythonSource::CrossFound(_) => ".so".to_string(),
    }
}

/// The target CPython `"<major>.<minor>"` (no free-threaded `t` suffix --
/// `vendor/cpython/<minor>/` has one directory per minor version regardless
/// of GIL/free-threaded build, see `cpython_vendor_dir`), read directly from
/// `pyo3_build_config`'s already-resolved target ABI. Must not execute a
/// live interpreter for this: in cross builds (e.g. musllinux) the only
/// "python" reachable via `PYO3_PYTHON`/PATH is maturin's own bootstrap
/// interpreter, whose version has nothing to do with whichever interpreter
/// this specific `--find-interpreter` build invocation is actually
/// targeting.
fn target_python_minor() -> String {
    let version = pyo3_build_config::get().target_abi().version();
    format!("{}.{}", version.major, version.minor)
}

/// `vendor/cpython/<minor>/`, e.g. `vendor/cpython/3.12/`, containing that
/// minor version's vendored `Modules/_sqlite` and `Lib/sqlite3` sources (see
/// scripts/vendor_cpython.py). The vendored `Modules/_sqlite/*.c` sources
/// reach into CPython's internal (`pycore_*.h`) headers, which are not a
/// stable API across minor versions, so build.rs must compile against the
/// exact minor version of whichever interpreter it's building for rather
/// than a single pinned version.
fn cpython_vendor_dir(vendor_cpython_dir: &Path, minor: &str) -> PathBuf {
    let dir = vendor_cpython_dir.join(minor);
    if !dir.join("Modules/_sqlite").is_dir() {
        let available: Vec<String> = fs::read_dir(vendor_cpython_dir)
            .map(|entries| {
                entries
                    .filter_map(|e| e.ok())
                    .filter(|e| e.path().is_dir())
                    .map(|e| e.file_name().to_string_lossy().into_owned())
                    .collect()
            })
            .unwrap_or_default();
        panic!(
            "no vendored CPython sources for Python {minor} in {}; available: {available:?}. \
             Run `python scripts/vendor_cpython.py` to vendor more minor versions.",
            vendor_cpython_dir.display()
        );
    }
    dir
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

/// The `.c` files directly under `cpython_sqlite_dir` (not `clinic/`, which
/// holds generated `.c.h` headers, not translation units). Discovered
/// rather than hardcoded because the source set varies by CPython minor
/// version -- e.g. 3.10 has `cache.c` instead of `blob.c` (the `Blob` type
/// was added in 3.11).
fn clone_module_sources(cpython_sqlite_dir: &Path) -> Vec<PathBuf> {
    let mut sources: Vec<PathBuf> = fs::read_dir(cpython_sqlite_dir)
        .unwrap_or_else(|e| panic!("failed to read {}: {e}", cpython_sqlite_dir.display()))
        .map(|entry| entry.unwrap_or_else(|e| panic!("failed to read dir entry: {e}")).path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "c"))
        .collect();
    assert!(!sources.is_empty(), "found no .c files in {}", cpython_sqlite_dir.display());
    sources.sort();
    sources
}

/// Compile CPython's vendored (unmodified) `Modules/_sqlite/*.c` into a
/// `_sqlite3<EXT_SUFFIX>` extension module in `python/sqlite_rs/sqlite3/`,
/// dynamically linked against the `libsqlite3` built above instead of
/// whatever SQLite the host Python was built with.
///
/// The file must be named `_sqlite3<EXT_SUFFIX>` (not something distinctive
/// of this project) because module.c hardcodes its init symbol as
/// `PyInit__sqlite3` (see e.g. `vendor/cpython/3.14/Modules/_sqlite/module.c`), and
/// CPython's import machinery for extension modules looks up
/// `PyInit_<last dotted component>`. Nesting it under `sqlite_rs/sqlite3/`
/// gives it the distinct dotted name `sqlite_rs.sqlite3._sqlite3` -- never
/// shadowing the stdlib's top-level `_sqlite3` -- while also matching
/// stdlib's own internal layout (`sqlite3._sqlite3`), which is what
/// `materialize_lib_sqlite3`'s rewritten imports below expect.
fn build_sqlite_clone_module(
    cpython_sqlite_dir: &Path,
    sqlite_dir: &Path,
    libsqlite3_dir: &Path,
    out_dir: &Path,
) -> PathBuf {
    let python = resolve_python();
    let include_dir = include_dir(&python);
    let internal_dir = include_dir.join("internal");
    let ext_suffix = ext_suffix(&python);

    let sources = clone_module_sources(cpython_sqlite_dir);

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
            // One level up: python/sqlite_rs/sqlite3/_sqlite3.so -> ../libsqlite3.dylib
            cmd.arg("-Wl,-rpath,@loader_path/..");
        }
        "windows" => {
            panic!(
                "Windows build of the sqlite3 clone module is not yet supported \
                 (see plan follow-ups); phase 1 targets macOS + Linux x86_64 only"
            );
        }
        _ => {
            cmd.arg("-shared");
            cmd.arg("-Wl,-rpath,$ORIGIN/..");
        }
    }
    cmd.arg("-o").arg(&out_path);

    let status = cmd.status().expect("failed to invoke linker for the sqlite3 clone module");
    assert!(status.success(), "linking the sqlite3 clone module failed: {cmd:?}");

    out_path
}

/// Substitutions mechanically applied to both the vendored runtime
/// `__init__.py`/`dbapi2.py` (via `materialize_lib_sqlite3`) and the vendored
/// `.pyi` stubs (via `materialize_typeshed_sqlite3`) below -- both carry the
/// identical self-referential absolute imports, since the stubs describe the
/// same source. Order matters: the more specific "sqlite3.dbapi2" pattern
/// must come before the bare "sqlite3" one.
const SELF_REFERENTIAL_IMPORT_REWRITES: &[(&str, &str)] = &[
    ("from sqlite3.dbapi2 import", "from sqlite_rs.sqlite3.dbapi2 import"),
    ("from sqlite3 import", "from sqlite_rs.sqlite3 import"),
    ("from _sqlite3 import", "from sqlite_rs.sqlite3._sqlite3 import"),
];

fn rewrite_self_referential_imports(source: String) -> String {
    let mut rewritten = source;
    for (from, to) in SELF_REFERENTIAL_IMPORT_REWRITES {
        rewritten = rewritten.replace(from, to);
    }
    rewritten
}

/// Copy CPython's vendored (unmodified on disk, in `vendor/cpython/Lib/sqlite3/`)
/// `__init__.py`/`dbapi2.py`/`dump.py` into `python/sqlite_rs/sqlite3/`.
///
/// `__init__.py` and `dbapi2.py` assume they ARE the top-level `sqlite3`/
/// `_sqlite3` (`from sqlite3.dbapi2 import *`, `from _sqlite3 import *`) --
/// absolute, not relative, imports -- so nesting them under `sqlite_rs`
/// unmodified would resolve against the wrong (system stdlib) module, if
/// any. `SELF_REFERENTIAL_IMPORT_REWRITES` mechanically corrects just those
/// self-referential import lines; `dump.py` has none and is copied as-is.
fn materialize_lib_sqlite3(vendor_lib_sqlite3_dir: &Path, out_dir: &Path) {
    for name in ["__init__.py", "dbapi2.py", "dump.py"] {
        let source = fs::read_to_string(vendor_lib_sqlite3_dir.join(name))
            .unwrap_or_else(|e| panic!("failed to read {name}: {e}"));
        fs::write(out_dir.join(name), rewrite_self_referential_imports(source))
            .unwrap_or_else(|e| panic!("failed to write {name}: {e}"));
    }
}

/// Copy basedpyright's vendored (unmodified on disk, in `vendor/typeshed/stdlib/`)
/// `_sqlite3.pyi`/`sqlite3/{__init__,dbapi2,dump}.pyi` into `python/sqlite_rs/sqlite3/`,
/// applying the same `SELF_REFERENTIAL_IMPORT_REWRITES` as `materialize_lib_sqlite3`
/// above -- these stubs describe the same source and carry the identical
/// self-referential absolute imports (see scripts/vendor_typeshed_sqlite3.py).
fn materialize_typeshed_sqlite3(vendor_typeshed_dir: &Path, out_dir: &Path) {
    let stdlib = vendor_typeshed_dir.join("stdlib");
    let files = [
        (stdlib.join("_sqlite3.pyi"), out_dir.join("_sqlite3.pyi")),
        (stdlib.join("sqlite3").join("__init__.pyi"), out_dir.join("__init__.pyi")),
        (stdlib.join("sqlite3").join("dbapi2.pyi"), out_dir.join("dbapi2.pyi")),
        (stdlib.join("sqlite3").join("dump.pyi"), out_dir.join("dump.pyi")),
    ];
    for (src, dest) in files {
        let source =
            fs::read_to_string(&src).unwrap_or_else(|e| panic!("failed to read {}: {e}", src.display()));
        fs::write(&dest, rewrite_self_referential_imports(source))
            .unwrap_or_else(|e| panic!("failed to write {}: {e}", dest.display()));
    }
}

/// Compile `native/sqlite_rs_shim.c` (first-party, not vendored) and
/// statically link it into `_core` itself, then point `_core`'s own dynamic
/// linking at the `libsqlite3` built above so it shares the same SQLite
/// instance as the clone module.
fn compile_shim_and_link_core(native_dir: &Path, cpython_sqlite_dir: &Path, sqlite_dir: &Path, libsqlite3_dir: &Path) {
    let python = resolve_python();
    let include_dir = include_dir(&python);

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
    let vendor_cpython_dir = manifest_dir.join("vendor/cpython");
    let vendor_typeshed_dir = manifest_dir.join("vendor/typeshed");
    let native_dir = manifest_dir.join("native");
    let python_pkg_dir = manifest_dir.join("python/sqlite_rs");
    let sqlite3_pkg_dir = python_pkg_dir.join("sqlite3");

    let python_minor = target_python_minor();
    let cpython_version_dir = cpython_vendor_dir(&vendor_cpython_dir, &python_minor);
    let cpython_sqlite_dir = cpython_version_dir.join("Modules/_sqlite");
    let vendor_lib_sqlite3_dir = cpython_version_dir.join("Lib/sqlite3");

    println!("cargo:rerun-if-changed=build.rs");
    println!("cargo:rerun-if-changed={}", sqlite_dir.display());
    // Watch the whole vendor/cpython tree, not just python_minor's
    // subdirectory: rebuilding for a different interpreter (e.g. `maturin
    // develop` against a different venv) must pick up the newly-selected
    // minor version's sources too.
    println!("cargo:rerun-if-changed={}", vendor_cpython_dir.display());
    println!("cargo:rerun-if-changed={}", vendor_typeshed_dir.display());
    println!("cargo:rerun-if-changed={}", native_dir.display());
    // Once any explicit rerun-if-changed is emitted, Cargo stops watching the
    // whole package and only reruns build.rs when one of these listed paths'
    // mtime changes -- it has no notion of "did my own outputs disappear".
    // `maturin develop` deletes the previous editable install (including
    // everything build.rs wrote into python/sqlite_rs/) before reinstalling;
    // if none of the paths above changed since the last build, Cargo would
    // otherwise skip rerunning build.rs and leave those files missing. A
    // missing rerun-if-changed target always counts as "changed", so listing
    // a couple of build.rs's own key outputs here makes it self-healing.
    println!(
        "cargo:rerun-if-changed={}",
        python_pkg_dir.join(shared_lib_name("sqlite3")).display()
    );
    println!("cargo:rerun-if-changed={}", sqlite3_pkg_dir.join("__init__.py").display());

    fs::create_dir_all(&sqlite3_pkg_dir)
        .unwrap_or_else(|e| panic!("failed to create {}: {e}", sqlite3_pkg_dir.display()));

    let libsqlite3 = build_libsqlite3(&sqlite_dir, &python_pkg_dir);
    println!("cargo:warning=built {}", libsqlite3.display());

    let clone_module =
        build_sqlite_clone_module(&cpython_sqlite_dir, &sqlite_dir, &python_pkg_dir, &sqlite3_pkg_dir);
    println!("cargo:warning=built {}", clone_module.display());

    materialize_lib_sqlite3(&vendor_lib_sqlite3_dir, &sqlite3_pkg_dir);
    println!("cargo:warning=materialized {}", sqlite3_pkg_dir.display());

    materialize_typeshed_sqlite3(&vendor_typeshed_dir, &sqlite3_pkg_dir);
    println!("cargo:warning=materialized typeshed stubs into {}", sqlite3_pkg_dir.display());

    compile_shim_and_link_core(&native_dir, &cpython_sqlite_dir, &sqlite_dir, &python_pkg_dir);
}
