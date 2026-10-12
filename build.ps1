$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
if (-not (Test-Path -LiteralPath '.venv\Scripts\python.exe')) {
    py -m venv .venv
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python environment' }
}
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'Could not install build dependencies' }
& .\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean Loomly.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed' }
$buildDirectory = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot 'build'))
if ($buildDirectory -ne [IO.Path]::GetFullPath($PSScriptRoot) -and
    $buildDirectory.StartsWith([IO.Path]::GetFullPath($PSScriptRoot) + '\')) {
    if (Test-Path -LiteralPath $buildDirectory) {
        Remove-Item -LiteralPath $buildDirectory -Recurse -Force
    }
} else { throw 'Build directory outside project' }
Write-Host 'Ready: dist\Loomly.exe'
