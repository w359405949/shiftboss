# 平台脚本共用：读参数文件、按 Windows argv 规则转义、打印结果行。
$ErrorActionPreference = 'Stop'

function Read-Fields([string]$Path) {
    return [IO.File]::ReadAllText($Path, [Text.Encoding]::UTF8).Split([char]0)
}

# 与 Python subprocess.list2cmdline 对单个参数的规则相同。
function ConvertTo-Arg([string]$Value) {
    $quote = ($Value.Length -eq 0) -or ($Value -match "[ `t]")
    $out = [Text.StringBuilder]::new()
    $slashes = 0
    if ($quote) { [void]$out.Append('"') }
    foreach ($c in $Value.ToCharArray()) {
        if ($c -eq '\') { $slashes++ }
        elseif ($c -eq '"') { [void]$out.Append('\' * ($slashes * 2)); $slashes = 0; [void]$out.Append('\"') }
        else { [void]$out.Append('\' * $slashes); $slashes = 0; [void]$out.Append($c) }
    }
    [void]$out.Append('\' * $slashes)
    if ($quote) { [void]$out.Append('\' * $slashes); [void]$out.Append('"') }
    return $out.ToString()
}

function Join-Args([string[]]$Values) {
    return (@($Values | ForEach-Object { ConvertTo-Arg $_ }) -join ' ')
}

function Write-Result([hashtable]$Result) {
    [Console]::Out.WriteLine(($Result | ConvertTo-Json -Compress))
}
