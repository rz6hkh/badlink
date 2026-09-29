# Сборка BadLink в папку dist\BadLink (exe + драйвер + iperf3) и архив dist\BadLink-<версия>.zip
# Требуется: Python x64 и pip install pyinstaller.
#   обычная сборка (Windows 8.1/10/11): Python 3.10+
#   -Win7 (Windows 7 SP1 / Server 2008 R2): Python 3.8 — последний с поддержкой Win7 — и iperf3 на Cygwin 3.4
param([switch]$Win7)
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$version = (python -c "import netem; print(netem.__version__)").Trim()
$pyver = (python -c "import sys; print('%d.%d' % sys.version_info[:2])").Trim()
if ($Win7 -and $pyver -ne '3.8') { throw "Сборка для Windows 7 требует Python 3.8 (сейчас $pyver)" }
$suffix = if ($Win7) { '-win7' } else { '' }
Write-Host "BadLink $version$suffix (Python $pyver)"

Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
# PyInstaller пишет INFO в stderr — в Windows PowerShell 5.1 это не должно считаться ошибкой
$ErrorActionPreference = 'Continue'
python -m PyInstaller --noconfirm --clean --onedir --windowed --uac-admin `
    --name BadLink --distpath dist --workpath build --specpath build main.py 2>&1 |
    ForEach-Object { "$_" } | Where-Object { $_ -notmatch ' INFO: ' }
$ErrorActionPreference = 'Stop'
if ($LASTEXITCODE -ne 0) { throw "PyInstaller завершился с ошибкой" }

# драйвер и iperf3 кладём рядом с exe (не внутрь _internal): так их видно и можно заменить (LGPL)
$out = 'dist\BadLink'
Copy-Item -Recurse windivert $out
$iperfSrc = if ($Win7) { 'iperf3-win7' } else { 'iperf3' }
New-Item -ItemType Directory -Force "$out\iperf3" | Out-Null
Copy-Item "$iperfSrc\*" "$out\iperf3"
Copy-Item README.md, THIRD_PARTY.md $out
New-Item -ItemType Directory -Force "$out\docs" | Out-Null
Copy-Item docs\*.png "$out\docs"

# самопроверка собранного exe (без прав администратора и без перехвата)
$p = Start-Process "$out\BadLink.exe" -ArgumentList '--selftest' -Wait -PassThru
Get-Content "$out\selftest.log" -Encoding UTF8
Remove-Item "$out\selftest.log"
if ($p.ExitCode -ne 0) { throw "selftest собранного exe не прошёл" }

Compress-Archive -Path $out -DestinationPath "dist\BadLink-$version$suffix.zip" -Force
Write-Host "Готово: dist\BadLink-$version$suffix.zip"
