import marimo

__generated_with = "0.23.14"
app = marimo.App(width="medium")


@app.cell
def _():
    import sys
    import marimo as mo
    import sqlite_rs

    return mo, sqlite_rs, sys


@app.cell
def _():
    1+1
    return


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


@app.cell(hide_code=True)
def _(mo):
    mo.md(r"""
    ```
    1+1
    2-4
    ```
    """)
    return


@app.cell
def _():
    return


if __name__ == "__main__":
    app.run()
