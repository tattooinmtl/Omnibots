# A16.a hostile corpus

Each file tries to turn "text a bot reads" into "instructions a bot follows" (PLAN.md §3.3).
tests/test_injection.py runs every file here through web_fetch, the browser text path and read_file;
tests/test_a16_live.py (OMNIBOTS_LIVE=1) shows them to real models. To add a case, drop a file in
this folder: the checks run on every file, and `MUST_NOT` in the test lists the tool calls that
must never happen.
