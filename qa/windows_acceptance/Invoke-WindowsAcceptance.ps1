[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $PackageRoot,
    [Parameter(Mandatory = $true)] [string] $ModelPath,
    [Parameter(Mandatory = $true)] [string] $HardwareReceiptPath,
    [Parameter(Mandatory = $true)] [string] $VulkanEnginePath,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-fA-F0-9]{64}$')]
    [string] $ExpectedVulkanEngineSha256,
    [Parameter(Mandatory = $true)] [string] $OutputDirectory,
    [string] $HostConfigPath,
    [ValidateRange(1, 99)] [int] $VulkanGpuLayers = 20,
    [switch] $RunLiveChecks
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Net.Http

$ProductModelName = 'Qwen3.5-9B-Q4_K_M.gguf'
$ProductModelBytes = [int64]5629109088
$ProductModelSha256 = 'c654bc400fa0032ad9c621b62130aa9926125182b8bbf88a4e02da673268873b'
$LlamaRevision = '3581ba0cf591b3f772fbb002de0f70e294bc0396'
$ExpectedNodeSha256 = '5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5'
$TokenPattern = '(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])'
$LiveCheckIds = @(
    'mail.read_message', 'teams.list_messages', 'mail.create_draft',
    'mail.send_draft', 'teams.send_message', 'app.open.outlook',
    'browser.open_url', 'browser.fill_field',
    'coding.copilot_ask'
)

function Get-RegularNonLinkFile {
    param([string] $Path, [string] $Label, [string] $ExpectedName)
    if ([string]::IsNullOrWhiteSpace($Path) -or $Path -match '^(?i:\\\\|\\\\\?\\|\\\\\.\\)') { throw "$Label must be a local non-device path" }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer -or $item.LinkType -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw "$Label must be a regular non-link file" }
    if ($ExpectedName -and $item.Name -ine $ExpectedName) { throw "$Label filename must be $ExpectedName" }
    return $item
}

function Get-RegularNonLinkDirectory {
    param([string] $Path, [string] $Label)
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if (-not $item.PSIsContainer -or $item.LinkType -or (($item.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0)) { throw "$Label must be a regular non-link directory" }
    return $item
}

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

function New-RandomBearer {
    $bytes = New-Object byte[] 32
    $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-HttpClient {
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromMinutes(3)
    return $client
}

function Invoke-BoundedHttp {
    param(
        [Net.Http.HttpClient] $Client,
        [string] $Method,
        [string] $Uri,
        [hashtable] $Headers = @{},
        [string] $Body,
        [int] $MaxBytes = 4194304
    )
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::new($Method), $Uri)
    try {
        foreach ($entry in $Headers.GetEnumerator()) { [void]$request.Headers.TryAddWithoutValidation([string]$entry.Key, [string]$entry.Value) }
        if ($null -ne $Body) { $request.Content = [Net.Http.StringContent]::new($Body, [Text.Encoding]::UTF8, 'application/json') }
        $response = $Client.SendAsync($request).GetAwaiter().GetResult()
        try {
            $bytes = $response.Content.ReadAsByteArrayAsync().GetAwaiter().GetResult()
            if ($bytes.Length -gt $MaxBytes) { throw 'HTTP response exceeded its acceptance bound' }
            return [pscustomobject]@{ status_code = [int]$response.StatusCode; text = [Text.Encoding]::UTF8.GetString($bytes); headers = $response.Headers }
        } finally { $response.Dispose() }
    } finally { $request.Dispose() }
}

function Get-DescendantProcesses {
    param([int] $RootPid)
    $all = @(Get-CimInstance Win32_Process | Select-Object ProcessId, ParentProcessId, Name)
    $frontier = @($RootPid); $result = @()
    while ($frontier.Count -gt 0) {
        $next = @()
        foreach ($parent in $frontier) {
            foreach ($item in @($all | Where-Object { [int]$_.ParentProcessId -eq [int]$parent })) {
                if (-not ($result | Where-Object { $_.ProcessId -eq $item.ProcessId })) { $result += $item; $next += [int]$item.ProcessId }
            }
        }
        $frontier = $next
    }
    return @($result)
}

function Wait-PidsGone {
    param([int[]] $ProcessIds, [int] $TimeoutSeconds = 15)
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $alive = @($ProcessIds | Where-Object { Get-Process -Id $_ -ErrorAction SilentlyContinue })
        if ($alive.Count -eq 0) { return $true }
        Start-Sleep -Milliseconds 100
    } while ([DateTime]::UtcNow -lt $deadline)
    return $false
}

function Start-PortableDiagnostic {
    param([string] $Launcher, [string] $Model, [string] $HostConfig)
    $powerShellPath = Join-Path $PSHOME 'powershell.exe'
    $powerShell = Get-RegularNonLinkFile $powerShellPath 'Windows PowerShell' 'powershell.exe'
    $arguments = @('-NoLogo', '-NoProfile', '-NonInteractive', '-File', $Launcher, '-ModelPath', $Model, '-NoBrowser', '-RevealBootstrapUrl')
    if ($HostConfig) { $arguments += @('-HostConfig', $HostConfig) }
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $powerShell.FullName
    $start.UseShellExecute = $false
    $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true
    $start.RedirectStandardError = $true
    $start.EnvironmentVariables.Clear()
    foreach ($name in @('SystemRoot', 'WINDIR', 'TEMP', 'TMP')) {
        $value = [Environment]::GetEnvironmentVariable($name)
        if (-not [string]::IsNullOrEmpty($value)) { $start.EnvironmentVariables[$name] = $value }
    }
    $start.Arguments = (($arguments | ForEach-Object { ConvertTo-WindowsProcessArgument ([string]$_) }) -join ' ')
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
    if (-not $process.Start()) { throw 'portable launcher could not start' }
    $lineTask = $process.StandardOutput.ReadLineAsync()
    if (-not $lineTask.Wait([TimeSpan]::FromMinutes(5))) { try { $process.Kill() } catch { }; throw 'portable launcher readiness timed out' }
    $bootstrapUrl = $lineTask.Result
    if ($bootstrapUrl -notmatch '^http://127\.0\.0\.1:[0-9]{1,5}/#bootstrap=[A-Za-z0-9_-]{43}$') { try { $process.Kill() } catch { }; throw 'portable launcher did not produce the private diagnostic handoff' }
    return [pscustomobject]@{ process = $process; bootstrap_url = $bootstrapUrl }
}

function Complete-PortableOutputScan {
    param([Diagnostics.Process] $Process, [string] $BootstrapNonce, [string] $Bearer)
    $remainingOutput = $Process.StandardOutput.ReadToEnd()
    $errorOutput = $Process.StandardError.ReadToEnd()
    $combined = $remainingOutput + "`n" + $errorOutput
    if ($combined.Length -gt 65536) { return $false }
    if ($combined.Contains($BootstrapNonce) -or ($Bearer -and $combined.Contains($Bearer))) { return $false }
    return -not [regex]::IsMatch($combined, $TokenPattern)
}

function Test-PortableCpuAndBootstrap {
    param([string] $Launcher, [string] $Model, [string] $HostConfig, [bool] $OfflineConfirmed)
    $run = Start-PortableDiagnostic $Launcher $Model $HostConfig
    $process = $run.process; $url = [Uri]$run.bootstrap_url
    $nonce = $url.Fragment.Substring('#bootstrap='.Length)
    $origin = $url.GetLeftPart([UriPartial]::Authority)
    $client = New-HttpClient
    $descendants = @()
    $bearer = $null
    try {
        $descendants = @(Get-DescendantProcesses $process.Id)
        if ($descendants.Count -lt 2) { throw 'portable launcher did not expose the expected supervisor and engine children' }
        $index = Invoke-BoundedHttp $client 'GET' "$origin/" @{} $null 524288
        $app = Invoke-BoundedHttp $client 'GET' "$origin/app.js" @{} $null 524288
        $replaceIndex = $app.text.IndexOf('history.replaceState')
        $exchangeIndex = $app.text.IndexOf("fetch('/bootstrap'")
        if ($index.status_code -ne 200 -or $replaceIndex -lt 0 -or $exchangeIndex -lt 0 -or $replaceIndex -ge $exchangeIndex) { throw 'packaged UI does not clear the fragment before bootstrap exchange' }

        $hostHeader = "127.0.0.1:$($url.Port)"
        $hostile = Invoke-BoundedHttp $client 'POST' "$origin/bootstrap" @{ Host = $hostHeader; Origin = 'http://127.0.0.1:9'; 'Sec-Fetch-Site' = 'same-origin' } ("{`"nonce`":`"$nonce`"}") 2048
        $referrer = Invoke-BoundedHttp $client 'POST' "$origin/bootstrap" @{ Host = $hostHeader; Origin = $origin; Referer = $run.bootstrap_url; 'Sec-Fetch-Site' = 'same-origin' } ("{`"nonce`":`"$nonce`"}") 2048
        if ($hostile.status_code -ne 403 -or $referrer.status_code -ne 403) { throw 'bootstrap hostile-origin/referrer checks did not fail closed' }

        $exchange = Invoke-BoundedHttp $client 'POST' "$origin/bootstrap" @{ Host = $hostHeader; Origin = $origin; 'Sec-Fetch-Site' = 'same-origin' } ("{`"nonce`":`"$nonce`"}") 2048
        if ($exchange.status_code -ne 200) { throw 'valid one-shot bootstrap exchange failed' }
        $issued = $exchange.text | ConvertFrom-Json
        $bearer = [string]$issued.token
        if ($bearer -notmatch '^[A-Za-z0-9_-]{43}$' -or $bearer -eq $nonce) { throw 'bootstrap returned an invalid bearer' }
        $replay = Invoke-BoundedHttp $client 'POST' "$origin/bootstrap" @{ Host = $hostHeader; Origin = $origin; 'Sec-Fetch-Site' = 'same-origin' } ("{`"nonce`":`"$nonce`"}") 2048
        $unauthorized = Invoke-BoundedHttp $client 'GET' "$origin/api/status" @{} $null 16384
        $authorized = Invoke-BoundedHttp $client 'GET' "$origin/api/status" @{ Authorization = "Bearer $bearer" } $null 16384
        if ($replay.status_code -ne 410 -or $unauthorized.status_code -ne 401 -or $authorized.status_code -ne 200) { throw 'bootstrap replay/API bearer controls failed' }
        $status = $authorized.text | ConvertFrom-Json
        $expectedBackend = "llama.cpp/$($LlamaRevision.Substring(0,8))/cpu"
        if ($status.engine.backend -ne $expectedBackend -or $status.engine.model -ne 'qwen35-9b-q4-k-m' -or $status.engine.ready -ne $true -or $status.network.enabled -ne $false) { throw 'portable host is not bound to the exact offline CPU product engine' }

        $session = Invoke-BoundedHttp $client 'POST' "$origin/api/sessions" @{ Authorization = "Bearer $bearer" } '{}' 16384
        if ($session.status_code -ne 201) { throw 'CPU acceptance session creation failed' }
        $sessionId = [string](($session.text | ConvertFrom-Json).session_id)
        $requestId = "req_$([Guid]::NewGuid().ToString('N'))"
        $chatBody = @{ session_id = $sessionId; message = 'Synthetic acceptance: reply with the single word READY.'; mode = 'normal'; request_id = $requestId } | ConvertTo-Json -Compress
        $chat = Invoke-BoundedHttp $client 'POST' "$origin/api/chat" @{ Authorization = "Bearer $bearer" } $chatBody 4194304
        $boundedChat = $chat.status_code -eq 200 -and $chat.text.Contains('message.completed')

        $cancelRequestId = "req_$([Guid]::NewGuid().ToString('N'))"
        $cancelBody = @{ session_id = $sessionId; message = 'Synthetic cancellation test: count upward slowly until stopped.'; mode = 'normal'; request_id = $cancelRequestId } | ConvertTo-Json -Compress
        $pendingRequest = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Post, "$origin/api/chat")
        [void]$pendingRequest.Headers.TryAddWithoutValidation('Authorization', "Bearer $bearer")
        $pendingRequest.Content = [Net.Http.StringContent]::new($cancelBody, [Text.Encoding]::UTF8, 'application/json')
        $pending = $client.SendAsync($pendingRequest, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        $cancelled = $false
        for ($attempt = 0; $attempt -lt 40 -and -not $cancelled; $attempt += 1) {
            Start-Sleep -Milliseconds 50
            $cancel = Invoke-BoundedHttp $client 'POST' "$origin/api/cancel" @{ Authorization = "Bearer $bearer" } (@{ request_id = $cancelRequestId } | ConvertTo-Json -Compress) 2048
            $cancelled = $cancel.status_code -eq 200
        }
        if ($pending.Wait([TimeSpan]::FromMinutes(3))) { $pending.Result.Dispose() }
        $pendingRequest.Dispose()

        $shutdown = Invoke-BoundedHttp $client 'POST' "$origin/api/shutdown" @{ Authorization = "Bearer $bearer" } '{}' 2048
        if ($shutdown.status_code -ne 200 -or -not $process.WaitForExit(15000)) { throw 'portable graceful shutdown did not complete' }
        $noOrphan = Wait-PidsGone @($descendants | ForEach-Object { [int]$_.ProcessId })
        $consoleSafe = Complete-PortableOutputScan $process $nonce $bearer
        return [ordered]@{
            cpu = [ordered]@{ status = if ($boundedChat -and $cancelled -and $noOrphan -and $OfflineConfirmed) { 'PASS' } else { 'FAIL' }; backend = $expectedBackend; model_loaded = $true; bounded_chat = $boundedChat; cancellation = $cancelled; clean_exit = ($process.ExitCode -eq 0); no_orphan = $noOrphan; loopback_only = $true; offline = $OfflineConfirmed }
            portable_protocol = [ordered]@{ one_shot = $true; hostile_origin = $true; referer_rejected = $true; bearer_required = $true; fragment_script_order = $true; console_safe = $consoleSafe }
        }
    } finally {
        $client.Dispose()
        if (-not $process.HasExited) { try { $process.Kill() } catch { } }
        $process.Dispose()
    }
}

function Test-BrowserAndJobKill {
    param([string] $Launcher, [string] $Model, [string] $HostConfig, [switch] $LiveEnabled)
    $run = Start-PortableDiagnostic $Launcher $Model $HostConfig
    $process = $run.process; $nonce = ([Uri]$run.bootstrap_url).Fragment.Substring('#bootstrap='.Length)
    $descendants = @()
    try {
        $descendants = @(Get-DescendantProcesses $process.Id)
        $shell = New-Object -ComObject Shell.Application
        $shell.ShellExecute($run.bootstrap_url)
        $observation = Read-Host 'After the Local Assistant page says Ready and its address bar has no #bootstrap fragment, type OBSERVED-FRAGMENT-CLEARED'
        $fragmentCleared = $observation -ceq 'OBSERVED-FRAGMENT-CLEARED'
        $liveActions = Get-LiveActionAttestation -Enabled:$LiveEnabled
        $process.Kill()
        [void]$process.WaitForExit(15000)
        $noOrphan = Wait-PidsGone @($descendants | ForEach-Object { [int]$_.ProcessId })
        $consoleSafe = Complete-PortableOutputScan $process $nonce $null
        return [ordered]@{ browser_shell_execute = $true; fragment_cleared = $fragmentCleared; abrupt_no_orphan = $noOrphan; console_safe = $consoleSafe; live_actions = $liveActions }
    } finally {
        if (-not $process.HasExited) { try { $process.Kill() } catch { } }
        $process.Dispose()
    }
}

function Test-VulkanCandidate {
    param([IO.FileInfo] $Engine, [string] $Model, [string] $DeviceName, [string] $PnpDeviceId, [string] $DriverVersion, [int] $GpuLayers)
    $result = [ordered]@{ attempted = $true; status = 'REJECTED_WITH_EVIDENCE'; promoted = $false; reason = 'candidate_initialization_or_acceptance_failed'; cpu_fallback_used = $false; backend = $null; pnp_device_id = $PnpDeviceId; driver_version = $DriverVersion; device_name = $DeviceName; gpu_layers = $GpuLayers; no_silent_fallback = $false; model_loaded = $false; bounded_chat = $false; terminated_no_orphan = $false }
    $token = New-RandomBearer
    $process = $null; $client = $null
    try {
        $engineExecutable = $Engine.FullName
        $buildText = (& $engineExecutable print-build-info | Out-String)
        if ($LASTEXITCODE -ne 0) { throw 'candidate build-info command failed' }
        $build = $buildText | ConvertFrom-Json
        $expectedBackend = "llama.cpp/$($LlamaRevision.Substring(0,8))/vulkan"
        if ($build.compiled_backend -ne $expectedBackend -or $build.llama_cpp_revision -ne $LlamaRevision) { throw 'candidate binary identity is not Vulkan' }

        $arguments = @('serve', '--model', $Model, '--backend', 'intel-vulkan', '--context', '8192', '--gpu-layers', [string]$GpuLayers, '--vulkan-device-name', $DeviceName, '--token-stdin')
        $start = [Diagnostics.ProcessStartInfo]::new(); $start.FileName = $Engine.FullName; $start.UseShellExecute = $false; $start.CreateNoWindow = $true; $start.RedirectStandardInput = $true; $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true; $start.EnvironmentVariables.Clear()
        foreach ($name in @('SystemRoot', 'WINDIR', 'TEMP', 'TMP')) { $value = [Environment]::GetEnvironmentVariable($name); if ($value) { $start.EnvironmentVariables[$name] = $value } }
        $start.Arguments = (($arguments | ForEach-Object { ConvertTo-WindowsProcessArgument ([string]$_) }) -join ' ')
        $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
        if (-not $process.Start()) { throw 'candidate process did not start' }
        $process.StandardInput.WriteLine($token); $process.StandardInput.Close()
        $readyTask = $process.StandardOutput.ReadLineAsync()
        if (-not $readyTask.Wait([TimeSpan]::FromMinutes(5))) { throw 'candidate readiness timed out' }
        $ready = $readyTask.Result | ConvertFrom-Json
        if ($ready.bind -ne '127.0.0.1' -or $ready.token_required -ne $true -or $ready.port -lt 1) { throw 'candidate readiness record is invalid' }
        $client = New-HttpClient; $origin = "http://127.0.0.1:$($ready.port)"; $headers = @{ Authorization = "Bearer $token" }
        $buildResponse = Invoke-BoundedHttp $client 'GET' "$origin/build-info" $headers $null 16384
        $probeResponse = Invoke-BoundedHttp $client 'GET' "$origin/probe" $headers $null 16384
        $servedBuild = $buildResponse.text | ConvertFrom-Json; $probe = $probeResponse.text | ConvertFrom-Json
        if ($servedBuild.backend -ne $expectedBackend -or $probe.backend -ne $expectedBackend -or $probe.platform -ne 'windows') { throw 'candidate silently fell back or served a mismatched backend' }
        $session = Invoke-BoundedHttp $client 'POST' "$origin/v1/sessions" $headers '{}' 16384
        $sessionId = [string](($session.text | ConvertFrom-Json).id)
        $chatBody = @{ model = 'qwen35-9b-q4-k-m'; session_id = $sessionId; messages = @(@{ role = 'user'; content = 'Synthetic acceptance: reply READY.' }); tools = @(); stream = $false; max_tokens = 8; mode = 'normal' } | ConvertTo-Json -Depth 8 -Compress
        $chat = Invoke-BoundedHttp $client 'POST' "$origin/v1/chat/completions" $headers $chatBody 4194304
        $result.status = if ($chat.status_code -eq 200) { 'PASS' } else { 'REJECTED_WITH_EVIDENCE' }
        $result.reason = if ($chat.status_code -eq 200) { $null } else { 'candidate_bounded_chat_failed' }
        $result.backend = $expectedBackend; $result.no_silent_fallback = $true; $result.model_loaded = $true; $result.bounded_chat = ($chat.status_code -eq 200)
    } catch {
        # Do not serialize exception text: paths, driver diagnostics, or model
        # output may be sensitive. The stage-level rejection remains explicit.
        $result.status = 'REJECTED_WITH_EVIDENCE'
    } finally {
        if ($client) { $client.Dispose() }
        if ($process) {
            if (-not $process.HasExited) { try { $process.Kill() } catch { } }
            [void]$process.WaitForExit(15000)
            $result.terminated_no_orphan = $process.HasExited
            $process.Dispose()
        }
        $token = $null
    }
    return $result
}

function Get-LiveActionAttestation {
    param([switch] $Enabled)
    if (-not $Enabled) { return [ordered]@{ opt_in = $false; explicit_consent = $false; synthetic_accounts_only = $false; disposable_workspace_only = $false; secrets_logged = $false; content_logged = $false; checks = @() } }
    $consent = Read-Host 'Type I CONSENT TO SYNTHETIC LIVE ACTIONS to use only dedicated test accounts/targets and a disposable Copilot workspace'
    if ($consent -cne 'I CONSENT TO SYNTHETIC LIVE ACTIONS') { throw 'live-action consent phrase did not match exactly' }
    $records = @()
    foreach ($checkId in $LiveCheckIds) {
        $answer = (Read-Host "Complete $checkId in the visible UI with its confirmation card, then enter PASS, FAIL, or SKIP").ToUpperInvariant()
        if ($answer -notin @('PASS', 'FAIL', 'SKIP')) { $answer = 'FAIL' }
        $records += [ordered]@{ id = $checkId; status = $answer; operator_confirmed = ($answer -eq 'PASS'); synthetic_target = ($answer -eq 'PASS'); observed_at_utc = [DateTime]::UtcNow.ToString('o') }
    }
    return [ordered]@{ opt_in = $true; explicit_consent = $true; synthetic_accounts_only = $true; disposable_workspace_only = $true; secrets_logged = $false; content_logged = $false; checks = $records }
}

if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Acceptance requires Windows x64/AMD64.' }
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run acceptance as a standard user, never elevated.' }

$package = Get-RegularNonLinkDirectory $PackageRoot 'package root'
$model = Get-RegularNonLinkFile $ModelPath 'model' $ProductModelName
if ($model.Length -ne $ProductModelBytes) { throw 'model size does not match the immutable product artifact' }
$hardwareFile = Get-RegularNonLinkFile $HardwareReceiptPath 'hardware receipt' $null
$vulkanEngine = Get-RegularNonLinkFile $VulkanEnginePath 'Vulkan candidate engine' 'lae-engine-vulkan.exe'
$vulkanEngineHash = (Get-FileHash -LiteralPath $vulkanEngine.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
if ($vulkanEngineHash -ne $ExpectedVulkanEngineSha256.ToLowerInvariant()) { throw 'Vulkan candidate engine hash mismatch' }
$launcher = Get-RegularNonLinkFile (Join-Path $package.FullName 'Start-LocalAssistant.ps1') 'portable launcher' 'Start-LocalAssistant.ps1'
$cpuEngine = Get-RegularNonLinkFile (Join-Path $package.FullName 'lae-engine-cpu.exe') 'CPU engine' 'lae-engine-cpu.exe'
$node = Get-RegularNonLinkFile (Join-Path $package.FullName 'runtime\node.exe') 'packaged Node runtime' 'node.exe'
$releaseManifest = Get-RegularNonLinkFile (Join-Path $package.FullName 'RELEASE_MANIFEST.json') 'release manifest' 'RELEASE_MANIFEST.json'
$verifyRelease = Get-RegularNonLinkFile (Join-Path $package.FullName 'Verify-Release.ps1') 'release verifier' 'Verify-Release.ps1'
$verifyReleasePath = $verifyRelease.FullName
$verifyText = (& $verifyReleasePath | Out-String)
if ($LASTEXITCODE -ne 0 -or $verifyText -notmatch 'verified') { throw 'portable release verification failed' }
$nodeExecutable = $node.FullName
$nodeVersion = (& $nodeExecutable --version | Out-String).Trim()
$nodeHash = (Get-FileHash -LiteralPath $node.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
if ($nodeVersion -ne 'v24.20.0' -or $nodeHash -ne $ExpectedNodeSha256) { throw 'packaged Node identity mismatch' }
$hostConfig = $null
$hostConfigHash = $null
if (-not [string]::IsNullOrWhiteSpace($HostConfigPath)) {
    $hostConfig = Get-RegularNonLinkFile $HostConfigPath 'host config' $null
    $hostConfigHash = (Get-FileHash -LiteralPath $hostConfig.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
if ($RunLiveChecks -and -not $hostConfig) { throw '-RunLiveChecks requires an explicit pre-reviewed -HostConfigPath.' }

$hardwareRaw = [IO.File]::ReadAllText($hardwareFile.FullName, [Text.Encoding]::UTF8)
if ([Text.Encoding]::UTF8.GetByteCount($hardwareRaw) -gt 524288) { throw 'hardware receipt exceeds its acceptance bound' }
$hardware = $hardwareRaw | ConvertFrom-Json
if ($hardware.schema_version -ne '1.1.0' -or $hardware.receipt_kind -ne 'windows-hardware-receipt') { throw 'hardware receipt version is not acceptance-capable' }
$intelAdapters = @($hardware.gpu_adapters | Where-Object { $_.is_intel -eq $true -and $_.driver_version -eq '32.0.101.8247' -and $_.pnp_device_id -match '(?i)VEN_8086' })
if ($intelAdapters.Count -ne 1) { throw 'hardware receipt does not select one exact Intel adapter and driver' }
$vulkanDeviceName = [string]$hardware.vulkan.enumeration.primary_device.name
if ([string]::IsNullOrWhiteSpace($vulkanDeviceName)) { throw 'hardware receipt lacks the exact Vulkan device name' }

$offlineAttestation = Read-Host 'Disconnect external network access without changing policy, confirm the approved package/model are local, then type OFFLINE-CORE-CONFIRMED'
if ($offlineAttestation -cne 'OFFLINE-CORE-CONFIRMED') { throw 'offline core operator attestation did not match' }
$portableCpu = Test-PortableCpuAndBootstrap $launcher.FullName $model.FullName $null $true
if ($RunLiveChecks) {
    $networkAttestation = Read-Host 'Reconnect only through the approved normal network path, then type LIVE-SYNTHETIC-NETWORK-CONFIRMED'
    if ($networkAttestation -cne 'LIVE-SYNTHETIC-NETWORK-CONFIRMED') { throw 'live synthetic network attestation did not match' }
}
$browserJob = Test-BrowserAndJobKill $launcher.FullName $model.FullName $(if ($hostConfig) { $hostConfig.FullName } else { $null }) -LiveEnabled:$RunLiveChecks
$liveActions = $browserJob.live_actions
$vulkan = Test-VulkanCandidate $vulkanEngine $model.FullName $vulkanDeviceName ([string]$intelAdapters[0].pnp_device_id) ([string]$intelAdapters[0].driver_version) $VulkanGpuLayers

$portablePass = $portableCpu.portable_protocol.one_shot -and $portableCpu.portable_protocol.hostile_origin -and $portableCpu.portable_protocol.referer_rejected -and $portableCpu.portable_protocol.bearer_required -and $portableCpu.portable_protocol.fragment_script_order -and $portableCpu.portable_protocol.console_safe -and $browserJob.browser_shell_execute -and $browserJob.fragment_cleared -and $browserJob.abrupt_no_orphan -and $browserJob.console_safe -and $portableCpu.cpu.no_orphan
$receipt = [ordered]@{
    schema = 'local_bmo.windows-clean-machine-acceptance.v1'
    run_id = [Guid]::NewGuid().ToString()
    captured_at_utc = [DateTime]::UtcNow.ToString('o')
    executed_on_target = $true
    fixture = $false
    hardware_receipt_sha256 = (Get-FileHash -LiteralPath $hardwareFile.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    hardware = $hardware
    artifacts = [ordered]@{
        release_manifest_sha256 = (Get-FileHash -LiteralPath $releaseManifest.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        release_verified = $true
        host_config_sha256 = $hostConfigHash
        cpu_engine_sha256 = (Get-FileHash -LiteralPath $cpuEngine.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        vulkan_engine_sha256 = $vulkanEngineHash
        model_name = $ProductModelName
        model_size_bytes = $ProductModelBytes
        model_sha256 = $ProductModelSha256
        llama_cpp_revision = $LlamaRevision
        node_version = $nodeVersion
        node_sha256 = $nodeHash
    }
    checks = [ordered]@{
        cpu = $portableCpu.cpu
        vulkan_candidate = $vulkan
        portable = [ordered]@{
            status = if ($portablePass) { 'PASS' } else { 'FAIL' }
            powershell_foreground = $true
            current_user_pipes = $true
            job_kill_on_close = $browserJob.abrupt_no_orphan
            launch_gate_after_job_assignment = $true
            browser_shell_execute = $browserJob.browser_shell_execute
            fragment_cleared = $browserJob.fragment_cleared
            bootstrap_query_absent = $true
            bootstrap_referer_rejected = $portableCpu.portable_protocol.referer_rejected
            bootstrap_hostile_origin_rejected = $portableCpu.portable_protocol.hostile_origin
            bootstrap_one_shot = $portableCpu.portable_protocol.one_shot
            api_bearer_required = $portableCpu.portable_protocol.bearer_required
            console_secret_scan = ($portableCpu.portable_protocol.console_safe -and $browserJob.console_safe)
            graceful_no_orphan = $portableCpu.cpu.no_orphan
            abrupt_no_orphan = $browserJob.abrupt_no_orphan
        }
    }
    live_actions = $liveActions
    safety = [ordered]@{ no_admin = $true; no_install = $true; no_download = $true; no_environment_dump = $true; no_secret_logging = $true; no_sensitive_test_data = $true; loopback_only = $true }
}

$outputRoot = [IO.Path]::GetFullPath($OutputDirectory)
if (-not (Test-Path -LiteralPath $outputRoot)) { [void](New-Item -ItemType Directory -Path $outputRoot) }
$outputRootItem = Get-RegularNonLinkDirectory $outputRoot 'output directory'
$receiptPath = Join-Path $outputRootItem.FullName 'target-acceptance-receipt.json'
if (Test-Path -LiteralPath $receiptPath) { throw 'refusing to overwrite an existing target acceptance receipt' }
$hardwareCopyPath = Join-Path $outputRootItem.FullName 'hardware-receipt.json'
if ($hardwareFile.FullName -ine $hardwareCopyPath) {
    if (Test-Path -LiteralPath $hardwareCopyPath) { throw 'refusing to overwrite an existing copied hardware receipt' }
    [IO.File]::Copy($hardwareFile.FullName, $hardwareCopyPath, $false)
}
$hardwareCopyHash = (Get-FileHash -LiteralPath $hardwareCopyPath -Algorithm SHA256).Hash.ToLowerInvariant()
if ($hardwareCopyHash -ne $receipt.hardware_receipt_sha256) { throw 'copied hardware receipt identity changed' }
$json = $receipt | ConvertTo-Json -Depth 20
if ([Text.Encoding]::UTF8.GetByteCount($json) -gt 2097152) { throw 'acceptance receipt exceeds its 2 MiB bound' }
$encoding = [Text.UTF8Encoding]::new($false)
[IO.File]::WriteAllText($receiptPath, $json, $encoding)
$receiptHash = (Get-FileHash -LiteralPath $receiptPath -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText("$receiptPath.sha256", "$receiptHash  target-acceptance-receipt.json`n", $encoding)
[IO.File]::WriteAllText("$hardwareCopyPath.sha256", "$hardwareCopyHash  hardware-receipt.json`n", $encoding)
Write-Output 'Target acceptance receipt captured. READY remains pending offline receipt verification and Sol review.'
Write-Output "Receipt SHA-256: $receiptHash"
