# Retained Windows GPT acceptance evidence

Backed up from the reviewed candidate `274da9d74108e0e8a4f6b6b51ecec5a9550fdd0b` before activation work. Each ZIP contains its original 43 manifest-bound files and the manifest, including synthetic raw protocol, invocation, task definition and preservation checks. No provider credentials or private lecture inputs were used in either diagnostic. These are scoped acceptance records, not deployment packages.

- `windows-registry-acceptance.zip`: SHA256 `374ea485e78bbf0efab58280ed846e92ade30393df36b37cf6b97461aff709c7`; 116350 bytes.
- `windows-apply-patch-acceptance.zip`: SHA256 `c31bf88e0108bd17553379e1c5c4b40ac20091ae295e2eeaf732d369f4b620e2`; 164075 bytes.

Registry-only and single valid apply_patch denial passed. Broader OS/tool/provider acceptance is not established by these archives. Original unbundled files remain retained.

## Windows LPAC investigation (September 12)

All ZIPs contain a SHA256/size manifest and the exact executed wrapper/probe
sources, raw stdout/stderr, task state, native result and preservation evidence.
These retain failed read-boundary acceptance attempts. The final attempt proves
the LPAC token check only; it does not prove successful child initialization.

| Archive | Result | Archive SHA256 |
| --- | --- | --- |
| `windows-lpac-acceptance.zip` | Token scalar sizing failed before profile/child; outer JSON-ledger cleanup error; independent reconciliation found no owned survivors | `68886b33c5ac2fa7d18f82a1567ca0412da5bb2a623b176003fa584adb776975` |
| `windows-lpac-retest.zip` | Corrected token/ledger handling passed; profile created; process creation failed before child; both postflights passed | `0f32f1ff75386fff60d30e4eddc31fb344e509142abf81f673e64f55ed208ceb` |
| `windows-lpac-create-error.zip` | Retained actual CreateProcessW error203; no child; both postflights passed and task disabled | `e6b9fb69c4c8b4398240b5bbd28f38c39b3c7a646e94a0d98a5b995bf8905854` |
| `windows-lpac-localappdata.zip` | LOCALAPPDATA fixed process creation; unsupported token46 stopped suspended child, which was terminated/reaped; both postflights passed | `be57c5b51eb3972c8e5ea96b4da2ed0562d5d29d567d226d6dcb35f27d570029` |
| `windows-lpac-accesscheck.zip` | LPAC token proof passed; resumed child exited0xc0000142 before reads; both postflights passed | `29ddc228b677d75e29d8b0eaad8a244d2d2164acab2286520b1f8355e6328ae4` |
| `windows-lpac-console.zip` | Approved temporary DWORD1 test: startup failure unchanged; original absence independently verified restored; both postflights passed | `94a596db0f57387fbde32418c1d919820184f2f46dfecb738fa0cf3a2c65c441` |

The original profiles, fixtures, tasks and unbundled files remain retained.

Read-only console diagnosis: `windows-lpac-startup-diagnosis.zip`, SHA256 `2ecbb668182fcb80c71c869b519eceb34f0910f4ef4c703e77d26265ad9b4158`. Existing conbr LowBoxConsoleEnabled value was absent; no registry mutation occurred. This is a diagnostic lead, not a proven startup fix.

Read-only deeper diagnosis: `windows-lpac-deep-diagnosis.zip`, SHA256 `b9759b07f349c38d482db30617be1d5d165103edeef8eda137f95ca47a932297`. Contains pinned Microsoft capability requirements, actual cmd/Codex PE import metadata and bounded debugger inventory; no target launch or configuration change.

Two-capability implementation rerun: `windows-lpac-cmd-capabilities.zip`, SHA256 `7297760e5faac180842ed50acd2a66540841c406c66c9a94ff755e6a83afefde`. Exact token capabilities verified; startup failure unchanged; both preservation checks passed.

Portable CDB traces (Microsoft tool/PDB binaries remain in private diagnostic directories; manifests record excluded hashes):

- `windows-lpac-loader-trace.zip`: SHA256 `fba5e1f2d29a2d27d26a1a28c0a1ec8453cdd7be38846e5327553f4ce1384ade`; 63 manifest files. Child startup failure reproduced, loader flag could not be set without symbols.
- `windows-lpac-loader-symbols.zip`: SHA256 `6d50811d7d6e91dc917a9872f798218a3a18f0d3686525af216627d3c148d0b1`; 61 manifest files. Instrumentation expression failed in compiler child; watchdog timeout and inner reap error retained. Outer cleanup passed; independent reconciliation confirms all four owned PIDs absent and Hub preserved. No LPAC child result.

- `windows-lpac-native-loader.zip`: SHA256 `0261961bea28d0854c93352f06b83dc3e4a53897141c38d6eab48ddffa8d6bfe`; 57 manifest files. Matching symbols and loader flag verified. KERNELBASE.dll fails DLL_PROCESS_ATTACH; read oracle failed. Both postflights passed without timeout/cleanup error.

Detached startup fix: `windows-lpac-detached-fix.zip`, SHA256 `e8326e70542e26594d320c97e7e3a1e8db0c97973f8b8035100553886366a74c`; 248 manifest files/CRCs verified. Contains matching-symbol KERNELBASE return tracing and the successful undebugged LPAC cmd read oracle. Only the console flag changed; actual Codex/provider/deployment acceptance remains separate.

- `windows-canonicalize-fix.zip`: native Rust LPAC baseline error5 and patched path-proof PASS,
  source/build provenance and both postflights. SHA256
  `c4f7b0fe31c5bb5dbdf0508fc741130ae78a9e25fca349094b760738a9a98a21`;
  307 manifest files, 1,499,078 bytes. Actual Codex acceptance remains pending.

- `windows-codex-private-build.zip`: pinned private build preparation, metadata-only
  lock normalization, Application Control failure/event correlation, cleanup and
  signing inventory, plus unexecuted initialize probe/C# compile proof. SHA256
  `12234eba33b11d6598172ff7ade0a50d2ae685b890bc146676b345f21bae804f`; 257 manifest files, 1,583,016 bytes.
  No rebuilt Codex executable was produced.
