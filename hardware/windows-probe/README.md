# Windows hardware receipt — NOT_READY

`Get-HardwareReceipt.ps1` refuses before querying CIM/WMI, inspecting a probe,
or writing output. PowerShell management providers and `ConvertTo-Json`
materialize data before script-level byte bounds, so they cannot establish the
required bounded target receipt.

Do not bypass the refusal. A remotely built native collector must stream a
fixed schema with per-field, record, total-byte, and deadline limits. Until it
is independently reviewed and accepted on the exact target, CPU/GPU PNP, OS,
Vulkan, RAM, and board details remain user-reported rather than product
evidence.
