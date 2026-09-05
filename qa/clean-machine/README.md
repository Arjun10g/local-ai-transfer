# Clean-machine package checks

The scanner in `package.py` is exposed through the importable
`qa.clean_machine.package` alias. Source mode performs a bounded allowlist,
model/weight, dependency-string, oversized-file, and secret-pattern lint and
labels its result `ADVISORY-SOURCE-LINT-ONLY`.

Runtime-package scanning and package construction both refuse before touching
their caller-supplied paths. They remain unavailable until a handle-relative,
no-follow implementation can bind the complete source and destination trees.
