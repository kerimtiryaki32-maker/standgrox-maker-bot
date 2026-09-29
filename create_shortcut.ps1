$ErrorActionPreference = 'Stop'
$project = $PSScriptRoot
$icon = Join-Path $project 'icon.ico'
$script = Join-Path $project 'run_app.pyw'
if (-not (Test-Path $icon)) { throw 'icon.ico is missing.' }
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcut = $shell.CreateShortcut((Join-Path $desktop 'Standgrox Maker Bot.lnk'))
if (-not (Test-Path $script)) { throw 'run_app.pyw is missing.' }
$python = (& py -c 'import sys; print(sys.executable)').Trim()
if ($LASTEXITCODE -ne 0 -or -not $python) { throw 'Python was not found.' }
$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { throw 'pythonw.exe was not found.' }
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '"' + $script + '"'
$shortcut.WorkingDirectory = $project
$shortcut.IconLocation = $icon + ',0'
$shortcut.Description = 'Standgrox Maker Bot'
$shortcut.Save()
Write-Host 'Desktop shortcut created: Standgrox Maker Bot'
