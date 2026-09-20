# ruff: noqa: ARG001, A001, T201

import ast
import importlib.metadata
import shutil
import subprocess
from pathlib import Path

from sphinx.application import Sphinx
from sphinx.config import Config

# Configuration file for the Sphinx documentation builder.
#
# For the full list of built-in configuration values, see the documentation:
# https://www.sphinx-doc.org/en/master/usage/configuration.html

# -- Project information -----------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#project-information

project = "sqlite-rs"
copyright = "2026, Timothy Stephenson"
author = "Timothy Stephenson"
release = importlib.metadata.version(project)
version = ".".join(release.split(".", maxsplit=2)[:2])

# -- General configuration ---------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#general-configuration

extensions = [
    "myst_parser",
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.intersphinx",
]

# The API pages are generated from the docstrings the extension carries at
# runtime, so there is one copy of each and it is the one help() prints.
autodoc_member_order = "bysource"
# no-value: a constant's value here is whatever the machine that built the
# docs had. DEBUG_BUILD published as "True" because this builds unoptimized,
# which says nothing true about the package.
autodoc_default_options = {
    "members": True,
    "undoc-members": False,
    "no-value": True,
}

# Docstrings are written for help(), where `x` reads as code rather than as
# reStructuredText's default "title reference".
default_role = "code"

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "polars": ("https://docs.pola.rs/api/python/stable", None),
}

templates_path = ["_templates"]
exclude_patterns = []


# -- Options for HTML output -------------------------------------------------
# https://www.sphinx-doc.org/en/master/usage/configuration.html#options-for-html-output

html_theme = "pydata_sphinx_theme"
html_static_path = ["_static"]


# myst configuration
source_suffix = {
    ".md": "markdown",
}
root_doc = "index"


# marimo notebook html outputs
def build_marimo_notebooks(app: Sphinx, config: Config) -> None:  # pyright: ignore[reportUnusedParameter]
    """Automatically compiles modified Marimo notebooks to static HTML using pathlib."""
    # Find directories relative to this conf.py file
    conf_dir = Path(__file__).resolve().parent
    notebooks_dir = conf_dir / "notebooks"
    output_dir = conf_dir / "_static" / "marimo_html"

    # Ensure the output folder exists
    output_dir.mkdir(parents=True, exist_ok=True)

    if not notebooks_dir.exists():
        return

    print("\n[Marimo Build] Checking for notebook changes...")

    marimo_executable = shutil.which("marimo")
    if marimo_executable is None:
        msg = "No marimo installed."
        raise RuntimeError(msg)

    # Scan for Marimo .py files (skipping files starting with an underscore)
    for notebook_path in notebooks_dir.glob("*.py"):
        if notebook_path.name.startswith("_"):
            continue

        # Target output file path (.py -> .html)
        output_path = output_dir / notebook_path.with_suffix(".html").name

        # Check if the HTML needs to be created or updated
        is_outdated = (
            not output_path.exists()
            or notebook_path.stat().st_mtime > output_path.stat().st_mtime
        )

        if is_outdated:
            print(f" -> Exporting changed notebook: {notebook_path.name}")
            try:
                _ = subprocess.run(  # noqa: S603
                    [
                        marimo_executable,
                        "export",
                        "html",
                        str(notebook_path),
                        "-o",
                        str(output_path),
                    ],
                    shell=False,
                    check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as e:
                print(f" [Error] Failed to export {notebook_path.name}: {e}")

    print("[Marimo Build] Check complete!\n")


def stub_signatures() -> dict[str, tuple[str, str | None]]:
    """(arguments, return annotation) for each function in the type stub.

    pyo3 gives the extension's functions a signature at runtime but no
    annotations, so autodoc can only show `execute_and_fetch_all(connection,
    sql)`. The types are in the stub that type checkers already read; taking
    them from there keeps one copy of each rather than restating them in a
    docstring or a directive.
    """
    stub = Path(__file__).resolve().parents[2] / "python" / "sqlite_rs" / "__init__.pyi"
    module = ast.parse(stub.read_text(encoding="utf-8"))
    return {
        node.name: (
            f"({ast.unparse(node.args)})",
            f"{ast.unparse(node.returns)}" if node.returns else None,
        )
        for node in module.body
        if isinstance(node, ast.FunctionDef)
    }


_STUB_SIGNATURES = stub_signatures()


# Sphinx dictates the parameter list; three of the seven are what we need.
def use_stub_signature(  # noqa: PLR0913, PLR0917
    app: Sphinx,  # pyright: ignore[reportUnusedParameter]
    what: str,  # pyright: ignore[reportUnusedParameter]
    name: str,
    obj: object,  # pyright: ignore[reportUnusedParameter]
    options: object,  # pyright: ignore[reportUnusedParameter]
    signature: str | None,
    return_annotation: str | None,
) -> tuple[str | None, str | None]:
    """Swap autodoc's untyped signature for the stub's typed one."""
    unqualified = name.rsplit(".", maxsplit=1)[-1]
    return _STUB_SIGNATURES.get(unqualified, (signature, return_annotation))


def setup(app: Sphinx) -> None:
    _ = app.connect("config-inited", build_marimo_notebooks)
    _ = app.connect("autodoc-process-signature", use_stub_signature)
