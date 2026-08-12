$ErrorActionPreference = 'Stop'
Set-Location (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (Get-Command py -ErrorAction SilentlyContinue) { $Py = 'py'; $PyArgs = @('-3') }
elseif (Get-Command python -ErrorAction SilentlyContinue) { $Py = 'python'; $PyArgs = @() }
else { throw 'Python 3 is not installed or not in PATH.' }
& $Py @PyArgs -m pip install --upgrade pyinstaller
& $Py @PyArgs -m PyInstaller --noconfirm --clean --windowed --name RipFoundry `
    --icon '.\assets\RipFoundry.ico' `
    --add-data '.\assets\RipFoundry.ico;assets' `
    --add-data '.\assets\RipFoundry.png;assets' `
    ripfoundry.py
Write-Host ''
Write-Host 'Built executable:'
Write-Host (Join-Path $PWD 'dist\RipFoundry\RipFoundry.exe')
