# 在新终端标签页起一个会话。参数文件字段：工作目录、标题、argv…
param([string]$ParamFile)
. (Join-Path $PSScriptRoot 'common.ps1')
try {
    $fields = Read-Fields $ParamFile
    $cwd = $fields[0]; $title = $fields[1]; $command = @($fields[2..($fields.Length - 1)])
    $wt = Join-Path $env:LOCALAPPDATA 'Microsoft\WindowsApps\wt.exe'
    if (Test-Path $wt) {
        # WT 重组子进程命令行、自己包外层引号，却不转义参数内的引号：先按 argv 规则转义，再剥掉外层引号交给它。
        $forwarded = foreach ($arg in $command) {
            $q = ConvertTo-Arg $arg
            if ($q.StartsWith('"')) { $q = $q.Substring(1, $q.Length - 2) }
            # WT 把裸 `;` 当作命令分隔符。
            $q.Replace(';', '\;')
        }
        # -w 0：放进最近用过的窗口，每份任务一个标签页。
        $line = Join-Args (@('-w', '0', 'new-tab', '--title', $title, '-d', $cwd) + @($forwarded))
        $info = [Diagnostics.ProcessStartInfo]::new($wt, $line)
        $info.UseShellExecute = $false
        $proc = [Diagnostics.Process]::Start($info)
        $proc.WaitForExit()
        if ($proc.ExitCode -ne 0) { throw "wt.exe 退出码 $($proc.ExitCode)" }
        Write-Result @{ ok = $true; terminal = 'wt' }
    } else {
        $rest = if ($command.Length -gt 1) { Join-Args $command[1..($command.Length - 1)] } else { '' }
        $info = [Diagnostics.ProcessStartInfo]::new($command[0], $rest)
        $info.UseShellExecute = $true
        $info.WorkingDirectory = $cwd
        [void][Diagnostics.Process]::Start($info)
        Write-Result @{ ok = $true; terminal = 'console' }
    }
} catch {
    Write-Result @{ ok = $false; error = "$_" }
    exit 1
}
