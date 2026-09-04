[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string] $ModelPath,
    [ValidateSet('cpu-safe')]
    [string] $Backend = 'cpu-safe',
    [string] $Engine = (Join-Path $PSScriptRoot 'lae-engine-cpu.exe'),
    [string] $HostConfig,
    [switch] $NoBrowser,
    [switch] $RevealBootstrapUrl
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Get-RegularNonLinkFile {
    param([string] $Value, [string] $Label)
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -match '^(?i:\\\\|\\\\\?\\|\\\\\.\\)') {
        throw "$Label must be a local non-device path"
    }
    $item = Get-Item -LiteralPath $Value
    if ($item.LinkType -or $item.PSIsContainer) { throw "$Label must be a regular non-link file" }
    return $item.FullName
}

if ($Backend -ne 'cpu-safe') { throw 'the portable UI release contains only the exact CPU-safe product backend' }
$enginePath = Get-RegularNonLinkFile $Engine 'engine'
$model = Get-RegularNonLinkFile $ModelPath 'model'
$supervisor = Get-RegularNonLinkFile (Join-Path $PSScriptRoot 'portable-supervisor.mjs') 'portable supervisor'
$nodePath = Get-RegularNonLinkFile (Join-Path $PSScriptRoot 'runtime\node.exe') 'packaged Node.js runtime'
$expectedNodeSha256 = '5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5'
if ((Get-FileHash -LiteralPath $nodePath -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedNodeSha256) { throw 'packaged Node.js runtime identity mismatch' }
$nodeVersion = (& $nodePath --version | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $nodeVersion -ne 'v24.20.0') { throw "portable supervisor requires pinned Node.js v24.20.0; observed $nodeVersion" }

if ($NoBrowser -and -not $RevealBootstrapUrl) { throw '-NoBrowser requires the explicit -RevealBootstrapUrl diagnostic switch' }
$bootstrapPipeName = "LocalBMO-$([Guid]::NewGuid().ToString())"
$launchGatePipeName = "LocalBMOGate-$([Guid]::NewGuid().ToString())"
$pipeOptions = [System.IO.Pipes.PipeOptions]::CurrentUserOnly
$bootstrapPipe = [System.IO.Pipes.NamedPipeServerStream]::new($bootstrapPipeName, [System.IO.Pipes.PipeDirection]::In, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, $pipeOptions)
$launchGatePipe = [System.IO.Pipes.NamedPipeServerStream]::new($launchGatePipeName, [System.IO.Pipes.PipeDirection]::Out, 1, [System.IO.Pipes.PipeTransmissionMode]::Byte, $pipeOptions)
$arguments = @($supervisor, '--engine', $enginePath, '--model', $model, '--bootstrap-pipe', $bootstrapPipeName, '--launch-gate-pipe', $launchGatePipeName)
if (-not [string]::IsNullOrWhiteSpace($HostConfig)) {
    $config = Get-RegularNonLinkFile $HostConfig 'host config'
    $arguments += @('--config', $config)
}

# The supervisor generates both credentials in memory. Neither credential is
# accepted on argv or through the environment. The fragment URL crosses only
# the one-shot launcher pipe and the process remains foreground until shutdown.
# A kill-on-close Windows Job contains the supervisor and native engine, so an
# interrupted launcher cannot leave either runtime orphaned.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
public static class LocalAssistantJob {
  const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
  [StructLayout(LayoutKind.Sequential)] struct BasicLimits { public long PerProcessUserTimeLimit, PerJobUserTimeLimit; public uint LimitFlags; public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize; public uint ActiveProcessLimit; public UIntPtr Affinity; public uint PriorityClass, SchedulingClass; }
  [StructLayout(LayoutKind.Sequential)] struct IoCounters { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
  [StructLayout(LayoutKind.Sequential)] struct ExtendedLimits { public BasicLimits BasicLimitInformation; public IoCounters IoInfo; public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed; }
  [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int infoClass, ref ExtendedLimits info, uint length);
  [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
  [DllImport("kernel32.dll")] public static extern bool CloseHandle(IntPtr handle);
  public static IntPtr Create() { var job=CreateJobObject(IntPtr.Zero, null); if(job==IntPtr.Zero) throw new Win32Exception(); var info=new ExtendedLimits(); info.BasicLimitInformation.LimitFlags=JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE; if(!SetInformationJobObject(job, 9, ref info, (uint)Marshal.SizeOf(info))) { CloseHandle(job); throw new Win32Exception(); } return job; }
  public static void Assign(IntPtr job, IntPtr process) { if(!AssignProcessToJobObject(job, process)) throw new Win32Exception(); }
}
'@
function ConvertTo-WindowsProcessArgument {
    param([string] $Value)
    $quote = [char]34; $slash = [char]92; $result = [string]$quote; $slashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq $slash) { $slashes += 1; continue }
        if ($character -eq $quote) { $result += ''.PadLeft(($slashes * 2 + 1), $slash) + $quote; $slashes = 0; continue }
        if ($slashes -gt 0) { $result += ''.PadLeft($slashes, $slash); $slashes = 0 }
        $result += $character
    }
    if ($slashes -gt 0) { $result += ''.PadLeft(($slashes * 2), $slash) }
    return $result + $quote
}

$job = [LocalAssistantJob]::Create()
$process = $null
try {
    $start = [System.Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $nodePath
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $false
    $start.Arguments = (($arguments | ForEach-Object { ConvertTo-WindowsProcessArgument ([string]$_) }) -join ' ')
    $process = [System.Diagnostics.Process]::new()
    $process.StartInfo = $start
    if (-not $process.Start()) { throw 'portable supervisor process could not start' }
    [LocalAssistantJob]::Assign($job, $process.Handle)
    $launchGatePipe.WaitForConnection()
    $gateWriter = [System.IO.StreamWriter]::new($launchGatePipe, [System.Text.UTF8Encoding]::new($false), 256, $true)
    $gateWriter.Write("GO`n")
    $gateWriter.Flush()
    $gateWriter.Dispose()
    $launchGatePipe.Dispose()
    $launchGatePipe = $null
    $bootstrapPipe.WaitForConnection()
    $reader = [System.IO.StreamReader]::new($bootstrapPipe, [System.Text.UTF8Encoding]::new($false), $false, 256, $true)
    $bootstrapUrl = $reader.ReadLine()
    $reader.Dispose()
    if ($bootstrapUrl -notmatch '^http://127\.0\.0\.1:[0-9]{1,5}/#bootstrap=[A-Za-z0-9_-]{43}$') { throw 'portable supervisor did not emit an exact loopback bootstrap URL' }
    if ($RevealBootstrapUrl) { Write-Output $bootstrapUrl }
    if (-not $NoBrowser) {
        try {
            # ShellExecute receives the URL through the COM API; the launcher
            # never constructs a browser command line containing the nonce.
            $shellApplication = New-Object -ComObject Shell.Application
            $shellApplication.ShellExecute($bootstrapUrl)
        } catch {
            throw 'Browser launch was unavailable; rerun with -NoBrowser -RevealBootstrapUrl'
        }
    }
    Write-Output 'Local Assistant ready; browser bootstrap handed off.'
    $process.WaitForExit()
    $exitCode = $process.ExitCode
} finally {
    if ($process) {
        try { if (-not $process.HasExited) { $process.Kill() } } catch { }
        $process.Dispose()
    }
    if ($bootstrapPipe) { $bootstrapPipe.Dispose() }
    if ($launchGatePipe) { $launchGatePipe.Dispose() }
    [void][LocalAssistantJob]::CloseHandle($job)
}
exit $exitCode
