# Windows release source — NOT_READY

This directory is a source skeleton, not a portable package. The checked-in
manifest, checksum file, notices, SBOM, UI, and provenance files are advisory
inputs only. They do not prove that Node.js, an engine, dependency licenses, or
any runnable package is present.

All Windows build, package, verification, backend-run, and assistant-launch
entrypoints intentionally refuse before input access, output creation, or
process/browser activity. Do not bypass those refusals or copy source files into
a hand-made package.

Repository-side source lint remains available through:

```text
python -m qa.clean_machine.scan release/windows
```

That bounded allowlist/secret/dependency check reports
`authorization: ADVISORY-SOURCE-LINT-ONLY`. Runtime-package mode refuses and
cannot authorize execution.

Before this path can be re-enabled, independent review must accept:

1. a stable handle-relative, no-follow package builder/verifier that hashes the
   packaged copies;
2. a native launcher that pins Node, supervisor, and engine identities through
   process creation and owns the complete Windows Job/process tree;
3. a fixed-schema streaming hardware collector with record/byte/time bounds;
4. remotely built and receipted artifacts; and
5. a standard-user acceptance run on the exact Dell Core Ultra 7 vPro
   Enterprise laptop, including mandatory CPU and candidate Vulkan lanes.

Current disposition: **NOT_READY**. This source is safe to inspect and merge,
but it is never safe to package or execute as a product release.
