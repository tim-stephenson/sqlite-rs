import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys

    import marimo as mo
    import sqlite_rs

    return sqlite_rs, sys


@app.cell
def _(sys):
    sys.executable
    return


@app.cell
def _(sys):
    sys.platform
    return


@app.cell
def _(sqlite_rs):
    sqlite_rs
    return


@app.cell
def _(sqlite_rs):
    sqlite_rs.__dict__
    return


@app.cell
def _(sqlite_rs):
    sqlite_rs.sum_as_string(5, 15)
    return


@app.cell
def _():
    import sqlite3
    print(sqlite3.sqlite_version)  # version of SQLite compiled in

    # Find the actual shared library file
    import _sqlite3
    print(_sqlite3.__file__)
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
