"""The OmniBots doctor (PLAN.md A16.d): checks, sets up and maintains everything OmniBots needs.

Reference: `layout.json` in this folder (explained in docs/DOCTOR.md). Ways to run it:
  - python -m omnibots.doctor [--no-fix] [--online] [--install-deps] [--json]
  - Settings → Doctor (the Run doctor button)
  - Omi's `call_doctor` tool ("Omi, call the doctor")
  - every app start, quietly (folders, settings, provider config)
"""

from omnibots.doctor.doctor import Finding, Report, run_doctor

__all__ = ["Finding", "Report", "run_doctor"]
