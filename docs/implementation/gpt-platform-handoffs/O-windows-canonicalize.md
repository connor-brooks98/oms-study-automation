# O: Windows canonicalization fix

2026-09-13. **Native path regression PASS; compiler-path fix passed; private Windows build/version PASS; initialize hostname panic.**
Independent source, fixed-wrapper and raw-result review passed by
`/root/review_production_policy`. Production generation remains closed.

## Current resumed build — 2026-09-13

The user-disabled Smart App Control state was verified before the fresh run.
That run passed the previous policy barrier, then failed compiling AWS-LC:
`tree_drbg_jitter_entropy.c:17` could not resolve its nested `jitterentropy.h`
include. The header exists and matches the locked crate. A bounded native
syntax-only comparison reproduced C1083 at the original path (exit 2) and passed
at a shorter path (exit 0), with all 1,935 source files byte-identical. Both
processes reaped without timeout; the exact Hub postflight passed at
`2026-09-13T13:46:14.460994Z`. Independent evidence review passed.

The correction uses a fresh, shorter private Cargo cache and ordinary `--locked`
dependency fetching. Source, Rust toolchain, patches, V8 inputs and dependency lock
remain pinned and unchanged. The full rebuild produced the AWS-LC Rust libraries
at `2026-09-13T14:26:46.837786Z`, confirming the compiler-path fix. The later `codex-app-server` and `codex-core`
compilations failed with allocation/LLVM out-of-memory errors; Cargo exited 101
without timeout or cleanup errors. All 116 health receipts passed; independent
postflight at `2026-09-13T15:14:04.6788506Z` confirmed the task disabled, no owned
processes and exact Hub preservation. The evidence does not yet distinguish the
8 GiB job limit from system commit pressure. A serial build adjustment passed independent review and started at
`2026-09-13T15:19:14.149615Z`: both Cargo job settings are one, with the same
8 GiB cap, release profile and retained pinned target/cache. The fresh task and
receipts preserve the failed run. A read-only peak-memory query adds diagnostics
before unconditional job cleanup. The serial run passed both earlier crates,
then failed final `codex-cli` compilation with LLVM out-of-memory. Measured peak
job commitment was 8,605,347,840 bytes against the 8,589,934,592-byte cap; this
supports limit pressure but does not exclude system commit pressure. Cargo exited
101 without timeout or cleanup errors. All 121 health guards passed, and independent
postflight at `2026-09-13T16:20:00.7999856Z` confirmed task disabled, owned processes
absent and exact Hub preservation. No executable was produced; native
version/initialize remain pending. Host headroom and the smallest bounded
adjustment were reviewed. A 10 GiB attempt stopped before compiler/job creation
when fresh commit headroom fell below the required 12 GiB; independent
`2026-09-13T16:29:58.7598436Z` reconciliation and preservation passed. A smaller
9.5 GiB attempt passed independent review and fresh native preflight, then started
at `2026-09-13T16:33:54.101386Z` (Cargo PID 26216), reusing completed artifacts.
The preflight requires cap plus 2 GiB commit/4 GiB physical reserve; five-second
memory guards stop only the owned job below the same 2/4 GiB emergency floors.
Source, compiler, dependency lock, profile and serial execution remain unchanged.
The final CLI again failed with LLVM OOM, with peak job memory 10,256,633,856
bytes versus the 10,200,547,328-byte cap. All 109 memory checks and 18 Hub guards
passed; minimum commit headroom was 2,785,030,144 bytes. Cargo exited 101 without
timeout or cleanup errors. Independent postflight at
`2026-09-13T16:42:50.6894697Z` confirmed task disabled, owned processes absent and
exact Hub preservation. No executable was accepted or runtime probe launched.
A native offline locked unit-graph comparison verified that setting
`CARGO_PROFILE_RELEASE_LTO=false` changes only 1,281 LTO fields among 1,392 units;
all other graph fields match exactly. This disables cross-crate ThinLTO while
retaining local ThinLTO, optimization level 3, codegen units 4 and release debug
settings. Both metadata calls and independent cleanup/preservation passed.
The reviewed full rebuild started at `2026-09-13T16:58:16.080448Z` (Cargo PID 26340)
in fresh `target/codex-no-lto-20260913`, with unchanged source/compiler/lock,
serial execution, 9.5 GiB cap and reserve guards. Earlier targets are retained.
The run stopped at its 90-minute watchdog at `2026-09-13T18:28:16.252540Z`:
Cargo exit 124, timed out, no cleanup errors or observed compiler errors.
Peak job memory was 3,568,472,064 bytes; all 183 Hub guards and 1,260 memory checks
passed. Independent `2026-09-13T18:29:17.3470206Z` reconciliation confirmed task
disabled, owned processes absent and exact Hub preservation. Compilation reached
app-server protocol but produced no executable. A fresh continuation with the same profile, source and limits passed final
receipt-binding review and fresh native preflight. It started at
`2026-09-13T18:34:37.702748Z` (Cargo PID 22100), immediately reusing completed
artifacts at app-server protocol. The new task/log identity retains the same
90-minute bound and resource guards; all nine prior tasks were disabled and
free disk was 61,115,301,888 bytes at preflight. This continuation passed core and
app-server compilation, then reached its normal deadline during TUI compilation
at `2026-09-13T20:04:37.876860Z` (exit 124, timed out, cleanup errors empty).
No compiler errors were observed. Peak job memory was 7,354,302,464 bytes;
all 183 Hub and 1,260 memory checks passed. Independent reconciliation at
`2026-09-13T20:05:34.4223002Z` confirmed the task disabled, owned processes absent
and exact Hub preservation. The retained target had no executable at 20:06:05Z.
A second fresh continuation passed final receipt-binding review and native
preflight, then started at `2026-09-13T20:11:04.150898Z` (Cargo PID 24776),
reusing completed core/app-server artifacts. Its deadline is 21:41:04Z, with the
same source, profile, target and resource controls. It succeeded at
`2026-09-13T21:21:27.770259Z` (Cargo exit 0, no timeout or cleanup errors),
finishing the release build in 70m22s. All 144 Hub and 986 memory checks passed;
peak job memory was 9,299,189,760 bytes, below the 10,200,547,328-byte cap.
Independent reconciliation at `2026-09-13T21:22:58.6803676Z` confirmed task
disabled, owned processes absent and exact Hub preservation.

The custom executable is 293,978,624 bytes, SHA256
`74d7706f403336b693853eb8582e0424d9f765ac6c2d98e408206bf03e5ecf4f`.
The frozen local copy matches the native receipt. The full build and exact native LPAC version check passed. Initialize failed
with a hostname API panic; dependent arg0 execution remains pending. This is a private
patched build, not the official release binary. Generation remains closed.

A prior attempt to copy the entire old cache was abandoned after its control-call
timeout. The exact owned worker was terminated and reaped; process reconciliation
was empty and the Hub postflight passed at `2026-09-13T14:08:59.398506Z`.
Its partial cache and all failed artifacts are retained and unused. No agent
changed Windows policy, installed a signing service or purchased anything.

Completed diagnostic snapshot: [windows-runtime-build-path-fix.zip](../gpt-platform-evidence/windows-runtime-build-path-fix.zip),
SHA256 `c300963743eb1d860281887f87381386bf72c8c322a76b8d3d0277f31e4ed3d2`,
251 manifest files, 597,372 bytes. ZIP integrity and all manifest hashes/sizes
passed. This snapshot excludes the then-running corrected build and contains
unbound acceptance preparations only; it does not prove Codex startup.

Completed full-build failure: [windows-runtime-build-oom.zip](../gpt-platform-evidence/windows-runtime-build-oom.zip),
SHA256 `682bf47301aded1ab534b70fb1e0ca1b0544f7d7b18250e90aa76cd10d05b976`,
180 manifest files, 319,816 bytes. ZIP integrity and all manifest hashes/sizes
passed. It includes the completed run, compiler failures and preservation evidence;
it excludes any successor build and does not establish Codex startup.

Completed serial-build failure: [windows-runtime-serial-build-oom.zip](../gpt-platform-evidence/windows-runtime-serial-build-oom.zip),
SHA256 `723c9040e0ec1b5463b6327e55c0281f0a27686fe5fd47120c276d87b0bc0094`,
237 manifest files, 256,482 bytes. ZIP integrity and all manifest
hashes/sizes passed. The snapshot excludes any successor run and contains no
native Codex startup acceptance.

Completed 10 GiB preflight stop: [windows-runtime-cap10-preflight.zip](../gpt-platform-evidence/windows-runtime-cap10-preflight.zip),
SHA256 `db37479cadcd5355efb73fc62cc253e89f525fc8857974aea4b1b8d368d3e7f8`,
58 manifest files, 58,839 bytes; CRC and all manifest hashes/sizes
passed. No compiler/job launched. The later 9.5 GiB run is excluded.

Completed 9.5 GiB final CLI failure: [windows-runtime-cap95-build-oom.zip](../gpt-platform-evidence/windows-runtime-cap95-build-oom.zip),
SHA256 `2d9473e9e9e6fee539ab9739f94ea59b9e3eaa846d9f1f1dd3429fd03181c2d9`,
86 manifest files, 122,560 bytes; CRC and all manifest hashes/sizes
passed. The snapshot contains no successful Codex build or native startup.

Completed profile-resolution proof: [windows-runtime-lto-profile-proof.zip](../gpt-platform-evidence/windows-runtime-lto-profile-proof.zip),
SHA256 `b25e7141a618022b7504ad9d1e9cb0ecb53d4c0110d0f89b6652f27054b7c921`,
68 manifest files, 499,349 bytes; CRC and all manifest hashes/sizes
passed. The full rebuild is excluded.

Completed release LTO=false build window: [windows-runtime-lto-false-build-window.zip](../gpt-platform-evidence/windows-runtime-lto-false-build-window.zip),
SHA256 `e734f380afd2a455d5862efa9aa4ccedf8cb330f6d6c73af578f31f82b357427`,
419 manifest files, 1,215,823 bytes; CRC and all manifest hashes/sizes
passed. Includes the completed profile proof; excludes the continuation and any
native Codex startup acceptance.

Completed first LTO=false continuation: [windows-runtime-lto-false-continuation-window.zip](../gpt-platform-evidence/windows-runtime-lto-false-continuation-window.zip),
SHA256 `17f810b3d2936e7d0127c61d1e6bf34eaa888d22791357c65ebb9871ad01b782`,
323 manifest files, 578,490 bytes; CRC and all manifest hashes/sizes passed.
Excludes the successor continuation and any native Codex startup acceptance.

Successful release build: [windows-runtime-lto-false-build-success.zip](../gpt-platform-evidence/windows-runtime-lto-false-build-success.zip),
SHA256 `24a842bb9b83a305851fbf70eb5c35aab1f3c5dc9266445a101e5c4a7aaf1128`,
309 manifest files, 483,229 bytes; CRC and all manifest hashes/sizes passed.
The executable is retained separately at the exact local path in
`frozen-artifact.json`; this archive contains its provenance and receipts.
Native runtime and provider acceptance are excluded.

## Native rebuilt-runtime acceptance

The exact custom executable passed LPAC `--version` with raw stdout
`codex-cli 0.153.4\n`, empty stderr, normal exit 0, and no timeout/kill.
Child PID 26168 passed the fixed token/profile/capability/session oracles.
Independent `2026-09-13T21:29:13.2310035Z` reconciliation and both postflights
passed; all owned processes were gone, task disabled, original installed Codex
hash and exact Hub unchanged.

The subsequent fixed initialize probe failed. Child PID 3020 passed the same
pre-resume LPAC gates but returned no stdout/initialize response. Stderr records
`gethostname-1.1.0/src/lib.rs:85: GetComputerNameExW did not provide buffer size`.
The 15-second child handshake deadline triggered owned kill/reap (exit
`0xe0000001`); the wrapper exited 1 without its own timeout. Only the fixed
initialize request was sent; no initialized notification or provider call ran.
Both postflights and independent `2026-09-13T21:34:11.3729403Z` reconciliation
passed, with all three owned PIDs gone, task disabled, original runtime pin and
exact Hub unchanged. Independent raw-output, token, manifest and preservation
review passed for this failure classification. No retry or arg0 tests ran.

Version/initialize evidence: [windows-runtime-version-initialize.zip](../gpt-platform-evidence/windows-runtime-version-initialize.zip),
SHA256 `47d3e7123b79d840e5341514f5533fdb6f47fce846ffbb68e79c91bfaf424482`,
126 manifest files, 218,012 bytes; CRC and all manifest hashes/sizes passed.
Root-cause diagnosis must precede any fresh reviewed retry; build/version success
does not establish initialize, provider or deployment acceptance.

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

Connor subsequently disabled Smart App Control himself and authorized continued
bounded work. Read-only preflight at `2026-09-13T13:24:38.9479483Z` confirms the
previously blocking policy and its evaluation counterpart are not enforced.
Other Windows policies remain active. The exact live Hub and worker identities
are unchanged, both old tasks are disabled, and no owned build process remains.
Independent review passed the fresh build invocation with the same source pins,
two jobs, 8 GiB job cap, 90-minute bound and recurring health guards. Its task,
logs and target directory are fresh; all earlier failed bytes remain retained.
No agent changed policy and no signing purchase is requested. The historical
barrier and prior restriction below describe the earlier host state.

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
