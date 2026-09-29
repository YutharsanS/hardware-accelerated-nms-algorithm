# Thin delegator so `make <target>` works from the repository root while the real rules
# live in scripts/, as the repository layout in docs/README.md prescribes.
include scripts/Makefile
