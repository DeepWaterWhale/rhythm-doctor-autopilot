$ErrorActionPreference = 'Stop'
Start-Process -FilePath 'C:\Python314\pythonw.exe' -ArgumentList @(('"' + (Join-Path $PSScriptRoot 'rhythm-doctor-autoplay-app.py') + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
