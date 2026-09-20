# API

Everything here acts on a live connection or statement from
`sqlite_rs.sqlite3`, never the stdlib `sqlite3`: the extension finds the
`sqlite3*` and `sqlite3_stmt*` inside those objects by reading CPython's
private struct layout, which is only guaranteed to match for objects this
package's own clone module built. A stdlib object raises `TypeError`.

```{eval-rst}
.. automodule:: sqlite_rs
   :members:
   :imported-members:
```
