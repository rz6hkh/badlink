# Сборка BadLink в папку dist\BadLink (exe + драйвер + iperf3) и архив dist\BadLink-<версия>.zip
# Требуется: Python 3.10+ x64, pip install pyinstaller
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$version = (python -c "import netem; print(netem.__version__)").Trim()
Write-Host "BadLink $version"

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
Copy-Item -Recurse windivert, iperf3 $out
Copy-Item README.md, THIRD_PARTY.md $out
New-Item -ItemType Directory -Force "$out\docs" | Out-Null
Copy-Item docs\*.png "$out\docs"

# самопроверка собранного exe (без прав администратора и без перехвата)
$p = Start-Process "$out\BadLink.exe" -ArgumentList '--selftest' -Wait -PassThru
Get-Content "$out\selftest.log" -Encoding UTF8
Remove-Item "$out\selftest.log"
if ($p.ExitCode -ne 0) { throw "selftest собранного exe не прошёл" }

Compress-Archive -Path $out -DestinationPath "dist\BadLink-$version.zip" -Force
Write-Host "Готово: dist\BadLink-$version.zip"
