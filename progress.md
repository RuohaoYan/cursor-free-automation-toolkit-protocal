## 2026-06-15 - Task: Harden example configuration defaults

### What was done
- Sanitized the example environment configuration so it no longer contains real-looking proxy credentials, phone numbers, payment PIN examples, or enabled external automation defaults.
- Added a short safety configuration note explaining which defaults are intentionally disabled and how to enable only the minimum required fields for a controlled test.

### Testing
- Verified repository access through the GitHub connector and read `README.md`, `main.py`, `requirements.txt`, `config.example.yaml`, `.env.example`, and `START_HERE_CN.txt` before editing.
- Verified the update target by using the current `.env.example` blob SHA before replacing the file.
- Runtime tests were not executed because the environment cannot clone this GitHub repository through the terminal and this change is limited to templates/documentation.

### Notes
- `.env.example`：removed real-looking sample credentials and changed external service defaults to opt-in.
- `docs/safe-configuration.md`：documented safe defaults and configuration usage boundaries.
- Rollback: revert commit `4ccf7471403a51cdda39c81903db4df9c76e0864` and the commit that created this progress entry/documentation if the previous template defaults are required.
