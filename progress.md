## 2026-06-15 - Task: Harden example configuration defaults

### What was done
- Sanitized the example environment configuration so it no longer contains real-looking proxy credentials, phone numbers, payment PIN examples, or enabled external automation defaults.
- Added a short safety configuration note explaining which defaults are intentionally disabled and how to enable only the minimum required fields for a controlled test.
- Recorded the change, verification boundary, affected files, and rollback points.

### Testing
- Verified repository access through the GitHub connector and read `README.md`, `main.py`, `requirements.txt`, `config.example.yaml`, `.env.example`, and `START_HERE_CN.txt` before editing.
- Verified the update target by using the current `.env.example` blob SHA before replacing the file.
- Re-read `.env.example`, `docs/safe-configuration.md`, and `progress.md` after writing to confirm the expected safe defaults and documentation are present.
- Runtime tests were not executed because the environment cannot clone this GitHub repository through the terminal and this change is limited to templates/documentation.

### Notes
- `.env.example`：removed real-looking sample credentials and changed external service defaults to opt-in.
- `docs/safe-configuration.md`：documented safe defaults and configuration usage boundaries.
- `progress.md`：recorded this task, verification evidence, affected files, and rollback information.
- Rollback: revert commits `4ccf7471403a51cdda39c81903db4df9c76e0864`, `075cedbbbd47ab220d35c5c4d4ef547fc2d80847`, and `060f39c627d708013421f5719f0237e0d46bcd82`; also revert any later commit that only edits this progress entry if a fully exact rollback is required.
