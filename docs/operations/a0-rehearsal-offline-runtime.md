# A0 rehearsal offline Windows runtime contract

## Purpose

The native A0 rehearsal must use a dependency-complete Python 3.12 environment without
installing from a package index during the execution gate. Git source identity and Python
executable identity are necessary but do not prove the installed distribution closure.

The tracked runtime identity is:

- `scripts/a0-rehearsal-windows-py312.lock.json`
- Python `3.12.10`, CPython cache tag `cpython-312`
- 79 installed distributions, excluding only `oms-study-automation`
- canonical distributions-array SHA-256
  `efe8c28015f62650e6f9f549066c98773ade4ee4d2039a7b547b147be0fd0318`

The project distribution is intentionally excluded. The rehearsal launcher imports the exact
Git-attested `src` tree directly, so an editable installation is neither the source of truth nor
an offline-provisioning prerequisite.

## Provisioning strategy

1. Build a Windows CPython 3.12 wheelhouse outside the acceptance transaction.
2. Preserve a separate SHA-256 manifest for every wheel. Wheelhouse construction and review are
   their own authorization boundary.
3. Create a fresh venv with the approved Python executable.
4. Install dependencies only from the reviewed wheelhouse with package-index access disabled.
   Do not perform an editable project install.
5. Run the stdlib-only verifier against the physical venv dependency directory:

   ```powershell
   $Python = 'C:\path\to\.venv\Scripts\python.exe'
   $Purelib = 'C:\path\to\.venv\Lib\site-packages'
   & $Python -I -S -B scripts\verify-a0-rehearsal-runtime-lock.py `
     --dependency-path $Purelib
   if ($LASTEXITCODE -ne 0) { throw 'A0 runtime lock verification failed' }
   ```

6. Require `RUNTIME_LOCK_OK`, the exact count `79`, and the exact lock digest above before the
   rehearsal root is created.
7. Separately require the launcher, base interpreter, Git commit/tree, source-tree hash, and the
   Windows dependency/import closure attestations.

Copying a venv is not a provisioning strategy. A preserved copy may be useful for diagnosis, but
it is unacceptable for a new execution candidate unless the exact runtime lock passes and the
copy operation itself has a reviewed provenance contract.

## Fail-closed rules

- No package-index or provider egress is allowed during runtime verification or rehearsal.
- Extra, missing, duplicate-normalized, or version-different distributions fail the lock.
- An invalid Python version, implementation, cache tag, count, or lock digest fails the lock.
- The lock proves installed names and versions, not wheel provenance. A future execution proposal
  must separately bind the reviewed wheelhouse manifest or explicitly reuse a previously accepted
  immutable venv.
- Runtime dependency origins must remain inside the launcher-attested physical dependency paths.
- Application source import remains inside the Windows Job-bound child.
