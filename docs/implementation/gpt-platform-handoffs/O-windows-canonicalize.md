# O: Windows canonicalization fix

2026-09-13. **Native path regression PASS; private Codex build blocked by Windows Application Control.**
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

The canonicalization and Windows alias-containment patches are implemented and
reviewed. Actual Codex building stopped at host Application Control before a
Codex executable was produced. Resume only through an explicitly approved
build/signing/execution route; retain the NUC's existing policy and all failed
artifacts. Do not retry the rejected file by renaming, moving, elevating, or
changing admission policy. A source/runtime failure is not a reason to weaken LPAC.

After obtaining a permitted build, bind its new executable hash to a fresh
version proof, followed by the already prepared fixed initialize probe. That
probe waits for the exact initialize response before closing stdin, uses fresh
file-only/no-auth configuration with all telemetry exporters disabled, and has
passed independent source review and actual Windows C# compile-only validation.
It has not executed. Configured elevated Windows sandbox setup is reached only
by explicit setup or execution requests; neither is in this fixed protocol.

`GLOBALROOT` remains a fail-closed compatibility limitation in PathUri hierarchical
permissions and no-follow filesystem operations; do not claim those consumers work.
App-server, credential persistence, tool/read/network boundaries and provider
acceptance are separate tests; the tiny path fixture does not satisfy them.

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

## Private build preparation correction

The release commit labels workspace packages0.153.4 but commits a lockfile with
local versions0.0.0. The first locked build stopped before compilation; owned-job
cleanup and both Hub checks passed, and the task was disabled. The exact retained
lock normalization changes149 local workspace-inherited package versions only
(143 explicit members plus six auto-included path members). Independent parsed
TOML and manifest review confirms every external package/revision/checksum and
all other fields remain unchanged. Original lock SHA256
`3494b8a78d0f643556a83a9cc184e912bcab9f4c5640288952f4223452ba5dc8`;
final normalized SHA256
`a2cb91dfb2e8112bc81d05158fa00b9698e2df8cc1ae0547b5dc5606a44904d3`.
The fresh build retains `--locked`; it does not update third-party dependencies.

## Confirmed Application Control build barrier

Fresh Limited task `OMS GPT Private Runtime Build 734bbdfad4a9 v2` started
CargoPID5044 at`2026-09-13T04:45:59.490270Z`. The corrected `--locked` graph passed;
Rust and dependency compilation began. Cargo exited101 without timeout when
Windows denied execution of parking_lot_core0.9.12's newly built helper.
No Codex executable was produced and no rebuilt Codex or provider was executed.

- File: private `target/codex-v2/release/build/parking_lot_core-a3179d96918c35dc/build-script-build.exe`,
  145,920 bytes, NotSigned, flat SHA256
  `20818708ed2329feb709538181338894244de14db08ba509c3b8acd5c3226a5a`.
- Cargo: OS error4551, application control blocked the file, never executed.
- CodeIntegrity event3077/record582 at`2026-09-13T04:48:33.9089330Z`:
  policy`VerifiedAndReputableDesktop`, GUID`{0283ac0f-fff1-49ae-ada1-8a933130cad6}`,
  PolicyID`27555.1000.240208`, status`0xc0e90002`. Its flat hash matches the file.
  Correlated3033/3089 events show no signature; this is not evidence of malware.
- All eight pre/during/post health guards passed. Postflight
  `2026-09-13T04:48:36.416719+00:00`: exact Hub8268/revision/tree/schema and worker
  identities preserved. No timeout, cleanup_errors empty, task disable returned0;
  independent task state Disabled and owned-process reconciliation empty.

Independent final build-failure/raw-event/cleanup review: **PASS** by
`/root/review_production_policy`. This admission barrier is separate from the
native-tested path fix; rebuilding, runtime acceptance and generation stay pending.
No policy/ACL/account/registry exception, paid fallback or reset credit was used.

A read-only code-signing certificate metadata query at
`2026-09-13T04:57:56.5602666Z` found zero certificates in both CurrentUser/My and
LocalMachine/My, without query errors. No key was accessed, exported or used.
This does not inventory other machines or remote signing services. The remaining
required input is an approved signing/build route that satisfies host policy;
no signature purchase, policy exception or alternative launch is authorized here.

Final build/preparation/diagnosis archive:
[`windows-codex-private-build.zip`](../gpt-platform-evidence/windows-codex-private-build.zip),
SHA256 `12234eba33b11d6598172ff7ade0a50d2ae685b890bc146676b345f21bae804f`, 257 manifest files, 1,583,016 bytes.
ZIP integrity and every manifest size/hash verified. Includes exact prepared
source/lock diffs, build drivers and receipts, matched policy events, signing
metadata, and reviewed initialize draft with Windows compile-only evidence.
Public compiler/dependency binaries remain retained outside Git. Final worker
receipt SHA256`24db470dd3887ab47c3f433a06942ad4c4a81cca1a655fea020b700c2f140d28`.
