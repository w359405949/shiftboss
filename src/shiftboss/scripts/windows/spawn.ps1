# 后台起常驻进程，无窗口、脱离父进程。参数文件字段：工作目录、argv…
param([string]$ParamFile)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $fields = Read-Fields $ParamFile
    $cwd = $fields[0]; $command = @($fields[1..($fields.Length - 1)])
    $rest = if ($command.Length -gt 1) { Join-Args $command[1..($command.Length - 1)] } else { '' }
    $info = [Diagnostics.ProcessStartInfo]::new($command[0], $rest)
    $info.UseShellExecute = $true
    $info.WindowStyle = [Diagnostics.ProcessWindowStyle]::Hidden
    $info.WorkingDirectory = $cwd
    $proc = [Diagnostics.Process]::Start($info)
    Write-Result @{ ok = $true; pid = $proc.Id }
} catch {
    Write-Result @{ ok = $false; error = "$_" }
    exit 1
}
