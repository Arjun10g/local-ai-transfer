# Shadeform assertion boundary

`shadeform_runner.py` validates an immutable, metadata-only job receipt. It
does not create instances, transfer model data, or contact a provider. A valid
receipt requires a budget ceiling, warnings at 50/80/100%, an owner-bound
cleanup record, removed ephemeral credentials, and a provider backstop longer
than the test duration. `warning_level()` is the local assertion for budget
stop behavior; actual spend remains a Sol-approved operational gate.
