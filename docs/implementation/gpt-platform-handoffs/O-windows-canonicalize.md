# O: Windows canonicalization fix

2026-09-13. **Native path regression PASS; actual rebuilt Codex acceptance pending.**
Independent source, fixed-wrapper and raw-result review passed by
`/root/review_production_policy`. Production generation remains closed.

## Change and provenance

The pinned Rust1.95.0 Windows `get_path` now retries only a DOS-name error5 with
`VOLUME_NAME_NT`, using the same already-open handle, and returns its normalized
NT path under `\\?\GLOBALROOT`. Other errors retain existing behavior; a failed
fallback returns the original error. No lexical substitute or shared ACL grant
is used. This shared function covers canonicalize and File Debug formatting; callers
that canonicalize an executable path also benefit. current_exe itself uses
GetModuleFileNameW and is not changed.

Patch: `scripts/runtime-patches/rust-1.95.0-lpac-canonicalize.patch`, SHA256
`90f7ef88a479fc6621d0329d11f62d3e7b7bdced7f69dbe639dee4c02294c9ff`.
Pristine `library/std/src/sys/fs/windows.rs` SHA256
`e26633635161814b3e9bd75a50cf0bbe2d90d55c9342e3adcbced16d6b024099`;
patched SHA256 `370abe5e4236878194a6e5c5bf4d11366a1b3ed23dd8235c6852ee7e0904f3f4`.
Private compiler1.95.0 commit`59807616e1fa2540724bfbac14d7976d7e4a3860`.
The existing installed Rust/Codex runtimes were not replaced.

## Native red/green proof

Same dependency-free Rust fixture and compiler, static CRT and release panic-abort:
baseline uses bundled standard library; patched rebuilds its standard library
with scoped `RUSTC_BOOTSTRAP=1` and `-Z build-std=std,panic_abort`.
This is a private unstable build route, not an official Codex release.

| Run | Executable SHA256 | Result |
| --- | --- | --- |
| Baseline b82d75827f74 | `544e5e1841dbd0422061b1be34923c0b2ed73241b836b36eb0cdce2ef2d1a0bb` | PID16620, error5 at first inside canonicalization; panic-abort3221226505 |
| Patched 8d17cd127e5e | `699dfda6b5f1fe953f43504536d171c89f4643f364da0b763fa730fc1bc3901b` | PID21192, exit0, exact marker and empty stderr |

The patched marker follows assertions for absolute canonical path/reopen,
direct and canonical outside-file denial, read-only inside write denial and
unchanged content, missing-path NotFound, private-home Unicode write/canonical
reopen, and canonical parent containment. Fixture source SHA256
`1e5ef07fcedd655d09f8391c4f0311cada7b4a1e973d0da58ded0c441249c177`.

Both runs independently verified AppContainer1, functional LPAC access mask2,
exact fresh profile SID, exactly lpacCom/registryRead with attributes4, and
conbr/session1/Limited. No timeout or kill; owned children reaped, tasks disabled.
Both inner/outer preservation checks passed: live Hub8268, exact build/tree/schema
and three healthy workers unchanged, no new/missing monitored processes, no cleanup
error or Console registry change. Patched outer postflight:
`2026-09-13T04:22:18.3348634Z`.
No provider, credentials, live source or Anki access was performed.

Failed private assembly and compile attempts remain retained: Windows long-path
extraction, unexposed Rust NT constant, and missing rebuilt panic_abort runtime.
The final build corrects these without shared configuration/registry changes.

## Next exact handoff

Build pinned Codex source commit`3d2ee51ca2d5db578f328aa75e20aa22c0197c9a`
against the patched private standard library, preserving official Windows build
flags and explicit private-build hash provenance. Before acceptance, correct arg0's
temp-directory guard to compare normalized canonical paths on both sides.
`GLOBALROOT` remains a fail-closed compatibility limitation in PathUri hierarchical
permissions and no-follow filesystem operations; do not claim those consumers work.
Then bind the new binary hash to fresh version/startup acceptance. App-server,
credential persistence, tool/read/network boundaries and provider acceptance are
separate tests; this tiny fixture does not satisfy them.

All raw artifacts are retained beneath O's visualization root in
`windows-canonicalize-baseline`, `windows-canonicalize-patched`, and
`windows-runtime-path-fix/build`. Consumed tasks/profiles are never retry targets.

Retained source/receipt archive: [`windows-canonicalize-fix.zip`](../gpt-platform-evidence/windows-canonicalize-fix.zip),
SHA256 `c4f7b0fe31c5bb5dbdf0508fc741130ae78a9e25fca349094b760738a9a98a21`,
307 manifest files, 1,499,078 bytes. ZIP integrity and every manifest hash/size
verified. Public compiler binaries are retained locally and hash-indexed, excluded
from this Git evidence archive. Prior failed patch files keep their original names;
the final patch is the `candidate/scripts/runtime-patches/` entry and final build
receipt hash above, not the historical build-directory copy.

## Codex guard patch prepared and reviewed

`codex-0.153.4-lpac-paths.patch` SHA256
`3f1f57a02bff04544a74cfb2532bc069d34497dbc59c2610ea852739ad148127`
changes only the Windows release alias-preparation path: create the validated home
empty, resolve and normalize both home/TEMP paths, reject containment or resolution
errors before creating helper files. Explicit missing overrides still fail upstream.
An unsafe default home may leave an empty directory on rejection; no helper files
are created. Non-Windows and debug behavior is unchanged.
The first draft's missing-default-home regression was corrected before build.
Independent code review, pinned patch application and rustfmt checks passed;
the two added Windows containment/first-use/error tests have not yet run.

Private Codex release build is authorized with two compile jobs, BelowNormal,
8 GiB aggregate owned-job cap, 90-minute hard bound and periodic live-Hub health
checks. Official workspace version0.153.4 remains to preserve the locked graph;
new SHA/provenance identifies the custom binary, not an official release label.
Only the private source/toolchain/cache paths are modified.
