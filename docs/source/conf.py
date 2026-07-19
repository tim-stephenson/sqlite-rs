# ruff: noqa: ARG001, A001, T201

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
]

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
def build_marimo_notebooks(app: Sphinx, config: Config) -> None:
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


def setup(app: Sphinx) -> None:
    _ = app.connect("config-inited", build_marimo_notebooks)
