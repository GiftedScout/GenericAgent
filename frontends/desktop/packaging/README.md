# Ubuntu / Linux desktop packaging

This personal fork builds Linux only. Windows/macOS builders and installers are removed.

- Native Tauri embeds the tracked `frontends/desktop/dist/**` renderer. The independent v1
  browser renderer is `static/**`; portable `runtime/app` excludes duplicated dist/source metadata.
- Workflow: `.github/workflows/desktop-release-package.yml`, Ubuntu 22.04, glibc <= 2.35.
- Python runtime requirements: `python-runtime-requirements.txt`; installer scripts: `scripts/linux/`.
- Node/Rust/actions/embedded Python inputs remain pinned in the workflow and Cargo.lock.
- Manual dispatch creates CI artifacts only. A `desktop-portable-*` tag publishes the Linux
  portable archive and `SHA256SUMS-linux.txt`; this task does not create a release.

## Validation and limits

Run `python3 -m pytest frontends/tests` and native `cargo test --locked`.
Use `release_qualification/linux/run_linux_release_qualification.sh` for installed-package journeys,
then `verify_release_evidence.py --linux REPORT --expected-commit COMMIT --output MANIFEST`.
Native GUI journeys and AppImage installation are separate gates, not implied by unit tests.
Computer-use remains unfinished and paused; retain its Ubuntu code without proactively invoking it.
