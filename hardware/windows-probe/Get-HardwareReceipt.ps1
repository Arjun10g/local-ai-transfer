[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $OutputPath = (Join-Path (Get-Location) 'hardware-receipt.json'),
    [switch] $NoSummary
)

# Read-only, no-admin Windows inventory. This script deliberately does not read
# the environment, network configuration, credentials, browser state, or files
# outside the requested output path and known system runtime locations.
$ErrorActionPreference = 'Stop'

function Get-OptionalCim {
    param([string] $ClassName, [string] $Filter)
    try {
        if ($Filter) { return @(Get-CimInstance -ClassName $ClassName -Filter $Filter -ErrorAction Stop) }
        return @(Get-CimInstance -ClassName $ClassName -ErrorAction Stop)
    } catch { return @() }
}

function Get-FileReceipt {
    param([string] $Path)
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        return [ordered]@{
            present = $true
            path = $item.FullName
            size_bytes = [int64]$item.Length
            version = $item.VersionInfo.FileVersion
            modified_utc = $item.LastWriteTimeUtc.ToString('o')
        }
    } catch { return [ordered]@{ present = $false } }
}

if ($env:OS -ne 'Windows_NT') { throw 'This probe must run on Windows.' }

$os = @(Get-OptionalCim 'Win32_OperatingSystem')[0]
$computer = @(Get-OptionalCim 'Win32_ComputerSystem')[0]
$cpus = Get-OptionalCim 'Win32_Processor'
$adapters = Get-OptionalCim 'Win32_VideoController'
$drivers = Get-OptionalCim 'Win32_PnPSignedDriver' 'DeviceClass = ''DISPLAY'''

$gpuReceipts = @(
    foreach ($gpu in $adapters) {
        $pnp = [string]$gpu.PNPDeviceID
        $ids = [regex]::Match($pnp, '(?i)VEN_[0-9A-F]{4}&DEV_[0-9A-F]{4}(?:&SUBSYS_[0-9A-F]{8})?').Value
        $isIntel = ([string]$gpu.Name -match '(?i)Intel') -or ($pnp -match '(?i)VEN_8086')
        [ordered]@{
            name = $gpu.Name
            vendor = $gpu.AdapterCompatibility
            is_intel = $isIntel
            pnp_device_id = $pnp
            pci_device_subsystem_id = if ($ids) { $ids.ToUpperInvariant() } else { $null }
            status = $gpu.Status
            availability = $gpu.Availability
            config_manager_error_code = $gpu.ConfigManagerErrorCode
            active_mode = $gpu.VideoModeDescription
            current_bits_per_pixel = $gpu.CurrentBitsPerPixel
            dedicated_memory_bytes_reported = if ($gpu.AdapterRAM) { [int64]$gpu.AdapterRAM } else { $null }
            driver_version = $gpu.DriverVersion
            driver_date = $gpu.DriverDate
            uma_or_shared_memory = $null
            memory_note = 'Win32_VideoController does not reliably expose UMA/shared memory; verify with Vulkan heaps on target.'
        }
    }
)

$displayDrivers = @(
    foreach ($driver in $drivers) {
        [ordered]@{
            device_name = $driver.DeviceName
            manufacturer = $driver.Manufacturer
            driver_version = $driver.DriverVersion
            driver_date = $driver.DriverDate
            inf_name = $driver.InfName
            signer = $driver.Signer
        }
    }
)

$dllPaths = @(
    (Join-Path $env:windir 'System32\vulkan-1.dll'),
    (Join-Path $env:windir 'System32\d3d12.dll'),
    (Join-Path $env:windir 'System32\dxgi.dll'),
    (Join-Path $env:windir 'System32\ze_loader.dll')
)
$runtimeDlls = [ordered]@{}
foreach ($dll in $dllPaths) { $runtimeDlls[[IO.Path]::GetFileName($dll)] = Get-FileReceipt $dll }

$vulkanInfo = $null
$vulkanCommand = Get-Command vulkaninfo -ErrorAction SilentlyContinue
if ($vulkanCommand) {
    # Explicit executable and fixed argument; bounded output and timeout. This
    # is enumeration only and never changes a driver/runtime or contacts a hub.
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $vulkanCommand.Source
    $psi.Arguments = '--summary'
    $psi.UseShellExecute = $false
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $psi
    try {
        [void]$process.Start()
        if ($process.WaitForExit(10000)) {
            $text = ($process.StandardOutput.ReadToEnd() + "`n" + $process.StandardError.ReadToEnd())
            $vulkanInfo = [ordered]@{ command = $vulkanCommand.Source; exit_code = $process.ExitCode; summary = $text.Substring(0, [Math]::Min(32768, $text.Length)) }
        } else {
            $process.Kill()
            $vulkanInfo = [ordered]@{ command = $vulkanCommand.Source; timed_out = $true }
        }
    } catch { $vulkanInfo = [ordered]@{ command = $vulkanCommand.Source; error = 'enumeration_failed' } }
    finally { $process.Dispose() }
}

$drives = @(
    foreach ($disk in (Get-OptionalCim 'Win32_LogicalDisk' 'DriveType = 3')) {
        [ordered]@{ device_id = $disk.DeviceID; size_bytes = $disk.Size; free_bytes = $disk.FreeSpace; filesystem = $disk.FileSystem }
    }
)

$receipt = [ordered]@{
    schema_version = '1.0.0'
    receipt_kind = 'windows-hardware-receipt'
    generated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    collection = [ordered]@{ read_only = $true; admin_required = $false; network_changed = $false; secrets_collected = $false }
    os = [ordered]@{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; architecture = $os.OSArchitecture; service_pack = $os.CSDVersion }
    computer = [ordered]@{ manufacturer = $computer.Manufacturer; model = $computer.Model; system_family = $computer.SystemFamily; product_identifier = $computer.IdentifyingNumber; total_memory_bytes = $computer.TotalPhysicalMemory; available_memory_bytes = $os.FreePhysicalMemory * 1024 }
    cpu = @(
        foreach ($cpu in $cpus) {
            [ordered]@{ name = $cpu.Name; manufacturer = $cpu.Manufacturer; description = $cpu.Description; physical_cores = $cpu.NumberOfCores; logical_processors = $cpu.NumberOfLogicalProcessors; max_clock_mhz = $cpu.MaxClockSpeed; address_width = $cpu.AddressWidth; data_width = $cpu.DataWidth; family = $cpu.Family; stepping = $cpu.Stepping; revision = $cpu.Revision }
        }
    )
    gpu_adapters = $gpuReceipts
    display_drivers = $displayDrivers
    vulkan = [ordered]@{ loader_present = ($runtimeDlls['vulkan-1.dll'].present); enumeration = $vulkanInfo; capabilities_note = 'Capture queue families, memory heaps, extensions, and cooperative matrix support from vulkaninfo summary when present; absent data is unknown, not unsupported.' }
    approved_runtime_dlls = $runtimeDlls
    storage = $drives
    sycl_level_zero = [ordered]@{ checked = $false; reason = 'Do not probe or install experimental runtimes until separately approved.' }
    safety = [ordered]@{ no_environment_dump = $true; no_credentials = $true; no_network_changes = $true; no_driver_install = $true; no_model_access = $true }
}

$parent = Split-Path -Parent $OutputPath
if ($parent -and -not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
$resolvedOutputPath = [IO.Path]::GetFullPath($OutputPath)
$json = $receipt | ConvertTo-Json -Depth 12
$encoding = New-Object Text.UTF8Encoding($false)
[IO.File]::WriteAllText($resolvedOutputPath, $json, $encoding)
$hash = (Get-FileHash -LiteralPath $OutputPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$resolvedOutputPath.sha256", "$hash  $([IO.Path]::GetFileName($resolvedOutputPath))`n", $encoding)

if (-not $NoSummary) {
    $summaryPath = "$resolvedOutputPath.summary.txt"
    $summary = @(
        'Local Assistant Engine Windows hardware receipt (read-only)',
        "Generated UTC: $($receipt.generated_at_utc)",
        "Computer: $($computer.Manufacturer) $($computer.Model)",
        "OS: $($os.Caption) build $($os.BuildNumber) / $($os.OSArchitecture)",
        "CPU: $((($cpus | Select-Object -First 1).Name))",
        "Memory: total=$($computer.TotalPhysicalMemory) available=$($receipt.computer.available_memory_bytes) bytes",
        "Intel adapters: $((@($gpuReceipts | Where-Object { $_.is_intel }).Count))",
        "Receipt SHA-256: $hash",
        'No secrets, environment dump, model bytes, or network changes were collected.'
    )
    [IO.File]::WriteAllLines($summaryPath, $summary, $encoding)
}

Write-Output "Receipt written: $([IO.Path]::GetFullPath($OutputPath))"
Write-Output "Receipt SHA-256: $hash"
