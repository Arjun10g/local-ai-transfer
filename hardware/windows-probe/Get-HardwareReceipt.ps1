[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string] $OutputPath = (Join-Path (Get-Location) 'hardware-receipt.json'),
    [string] $VulkanInfoPath,
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string] $ExpectedVulkanInfoSha256,
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
$baseboards = Get-OptionalCim 'Win32_BaseBoard'
$biosRecords = Get-OptionalCim 'Win32_BIOS'
$memoryModules = Get-OptionalCim 'Win32_PhysicalMemory'
$cpus = Get-OptionalCim 'Win32_Processor'
$adapters = Get-OptionalCim 'Win32_VideoController'
$drivers = Get-OptionalCim 'Win32_PnPSignedDriver' 'DeviceClass = ''DISPLAY'''
$displayPnpEntities = Get-OptionalCim 'Win32_PnPEntity' 'PNPClass = ''Display'''

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
            # WMI does not prove whether an adapter is integrated/UMA.  Keep
            # this explicit unknown so the SYCL planner cannot infer it from
            # an Intel product name.
            integrated = $null
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

$displayPnpReceipts = @(
    foreach ($device in $displayPnpEntities) {
        [ordered]@{
            name = $device.Name
            manufacturer = $device.Manufacturer
            pnp_device_id = $device.PNPDeviceID
            hardware_ids = @($device.HardwareID)
            class_guid = $device.ClassGuid
            service = $device.Service
            status = $device.Status
            config_manager_error_code = $device.ConfigManagerErrorCode
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
$vulkanCommand = $null
if (-not [string]::IsNullOrWhiteSpace($VulkanInfoPath)) {
    if ([string]::IsNullOrWhiteSpace($ExpectedVulkanInfoSha256)) { throw 'An explicit Vulkan probe requires -ExpectedVulkanInfoSha256.' }
    $candidate = Get-Item -LiteralPath $VulkanInfoPath -ErrorAction Stop
    if ($candidate.PSIsContainer -or $candidate.LinkType -or (($candidate.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw 'The Vulkan probe must be a regular non-link file.' }
    $observedProbeHash = (Get-FileHash -LiteralPath $candidate.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($observedProbeHash -ne $ExpectedVulkanInfoSha256.ToLowerInvariant()) { throw 'The Vulkan probe SHA-256 does not match the approved identity.' }
    $vulkanCommand = [pscustomobject]@{ Source = $candidate.FullName; ExplicitlyPinned = $true; Sha256 = $observedProbeHash }
} else {
    $discovered = Get-Command vulkaninfo -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($discovered) {
        $vulkanCommand = [pscustomobject]@{ Source = $discovered.Source; ExplicitlyPinned = $false; Sha256 = (Get-FileHash -LiteralPath $discovered.Source -Algorithm SHA256).Hash.ToLowerInvariant() }
    }
}
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
        # Begin both reads before waiting so even a noisy but hash-approved
        # probe cannot deadlock on a full redirected pipe.
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if ($process.WaitForExit(10000)) {
            $text = ($stdoutTask.GetAwaiter().GetResult() + "`n" + $stderrTask.GetAwaiter().GetResult())
            $boundedSummary = $text.Substring(0, [Math]::Min(32768, $text.Length))
            $summaryBytes = [Text.Encoding]::UTF8.GetBytes($boundedSummary)
            $summaryHasher = [Security.Cryptography.SHA256]::Create()
            try { $summaryHash = [BitConverter]::ToString($summaryHasher.ComputeHash($summaryBytes)).Replace('-', '').ToLowerInvariant() }
            finally { $summaryHasher.Dispose() }
            $field = @{}
            foreach ($name in @('apiVersion', 'driverVersion', 'vendorID', 'deviceID', 'deviceType', 'deviceName')) {
                $match = [regex]::Match($boundedSummary, "(?im)^\s*$name\s*=\s*(.+?)\s*$")
                if ($match.Success) { $field[$name] = $match.Groups[1].Value.Trim() }
            }
            $vulkanInfo = [ordered]@{
                command_name = [IO.Path]::GetFileName($vulkanCommand.Source)
                command_sha256 = $vulkanCommand.Sha256
                explicitly_pinned = $vulkanCommand.ExplicitlyPinned
                exit_code = $process.ExitCode
                timed_out = $false
                output_truncated = ($text.Length -gt 32768)
                summary_sha256 = $summaryHash
                summary = $boundedSummary
                primary_device = [ordered]@{
                    name = $field['deviceName']
                    vendor_id = $field['vendorID']
                    device_id = $field['deviceID']
                    device_type = $field['deviceType']
                    api_version = $field['apiVersion']
                    driver_version = $field['driverVersion']
                }
            }
        } else {
            $process.Kill()
            $vulkanInfo = [ordered]@{ command_name = [IO.Path]::GetFileName($vulkanCommand.Source); command_sha256 = $vulkanCommand.Sha256; explicitly_pinned = $vulkanCommand.ExplicitlyPinned; timed_out = $true }
        }
    } catch { $vulkanInfo = [ordered]@{ command_name = [IO.Path]::GetFileName($vulkanCommand.Source); command_sha256 = $vulkanCommand.Sha256; explicitly_pinned = $vulkanCommand.ExplicitlyPinned; error = 'enumeration_failed' } }
    finally { $process.Dispose() }
}

$drives = @(
    foreach ($disk in (Get-OptionalCim 'Win32_LogicalDisk' 'DriveType = 3')) {
        [ordered]@{ device_id = $disk.DeviceID; size_bytes = $disk.Size; free_bytes = $disk.FreeSpace; filesystem = $disk.FileSystem }
    }
)

$receipt = [ordered]@{
    schema_version = '1.1.0'
    receipt_kind = 'windows-hardware-receipt'
    generated_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    collection = [ordered]@{ read_only = $true; admin_required = $false; network_changed = $false; secrets_collected = $false }
    os = [ordered]@{ caption = $os.Caption; version = $os.Version; build = $os.BuildNumber; architecture = $os.OSArchitecture; service_pack = $os.CSDVersion; sku = $os.OperatingSystemSKU; product_type = $os.ProductType; build_type = $os.BuildType }
    computer = [ordered]@{ manufacturer = $computer.Manufacturer; model = $computer.Model; system_family = $computer.SystemFamily; system_type = $computer.SystemType; total_memory_bytes = $computer.TotalPhysicalMemory; available_memory_bytes = $os.FreePhysicalMemory * 1024 }
    baseboards = @(
        foreach ($board in $baseboards) { [ordered]@{ manufacturer = $board.Manufacturer; product = $board.Product; version = $board.Version; status = $board.Status } }
    )
    bios = @(
        foreach ($bios in $biosRecords) { [ordered]@{ manufacturer = $bios.Manufacturer; smbios_bios_version = $bios.SMBIOSBIOSVersion; version = $bios.Version; release_date = $bios.ReleaseDate } }
    )
    memory_modules = @(
        foreach ($memory in $memoryModules) { [ordered]@{ device_locator = $memory.DeviceLocator; capacity_bytes = $memory.Capacity; speed_mts = $memory.Speed; configured_speed_mts = $memory.ConfiguredClockSpeed; smbios_memory_type = $memory.SMBIOSMemoryType; form_factor = $memory.FormFactor } }
    )
    cpu = @(
        foreach ($cpu in $cpus) {
            [ordered]@{ name = $cpu.Name; manufacturer = $cpu.Manufacturer; description = $cpu.Description; device_id = $cpu.DeviceID; processor_id = $cpu.ProcessorId; physical_cores = $cpu.NumberOfCores; logical_processors = $cpu.NumberOfLogicalProcessors; max_clock_mhz = $cpu.MaxClockSpeed; address_width = $cpu.AddressWidth; data_width = $cpu.DataWidth; architecture = $cpu.Architecture; family = $cpu.Family; stepping = $cpu.Stepping; revision = $cpu.Revision; second_level_address_translation = $cpu.SecondLevelAddressTranslationExtensions; virtualization_firmware_enabled = $cpu.VirtualizationFirmwareEnabled }
        }
    )
    gpu_adapters = $gpuReceipts
    display_drivers = $displayDrivers
    display_pnp_entities = $displayPnpReceipts
    vulkan = [ordered]@{ loader_present = ($runtimeDlls['vulkan-1.dll'].present); enumeration = $vulkanInfo; capabilities_note = 'Capture queue families, memory heaps, extensions, and cooperative matrix support from vulkaninfo summary when present; absent data is unknown, not unsupported.' }
    approved_runtime_dlls = $runtimeDlls
    storage = $drives
    sycl_level_zero = [ordered]@{ checked = $false; available = $false; device_count = 0; device_name = $null; device_id = $null; driver_version = $null; runtime_version = $null; probe = $null; reason = 'Do not probe or install experimental runtimes until separately approved.' }
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
        "Baseboard records: $(@($baseboards).Count); memory modules: $(@($memoryModules).Count)",
        "Intel adapters: $((@($gpuReceipts | Where-Object { $_.is_intel }).Count))",
        "Receipt SHA-256: $hash",
        'No secrets, environment dump, model bytes, or network changes were collected.'
    )
    [IO.File]::WriteAllLines($summaryPath, $summary, $encoding)
}

Write-Output "Receipt written: $([IO.Path]::GetFileName($resolvedOutputPath))"
Write-Output "Receipt SHA-256: $hash"
