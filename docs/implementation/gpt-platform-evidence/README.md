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
