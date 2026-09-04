# Clean-machine package checks

The scanner in `package.py` is exposed through the importable
`qa.clean_machine.package` alias. It performs an allowlist, model/weight,
dependency-string, oversized-file, and secret-pattern scan without claiming a
native Windows launch. Native Windows and DLL-loader checks remain explicit
acceptance work.
