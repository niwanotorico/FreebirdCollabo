@echo off
rem Freebird Collab: relay on this PC + Cloudflare quick tunnel (no account).
rem Uses system Python if present, otherwise Blender's bundled python.exe.
cd /d "%~dp0"
where py >nul 2>nul && ( py -3 quick_tunnel.py & goto :end )
where python >nul 2>nul && ( python quick_tunnel.py & goto :end )
for /d %%D in ("C:\Program Files\Blender Foundation\Blender *") do (
  for /d %%V in ("%%D\*") do (
    if exist "%%V\python\bin\python.exe" ( "%%V\python\bin\python.exe" quick_tunnel.py & goto :end )
  )
)
echo Python not found. Install Python 3 from python.org, or edit this .bat with your Blender python.exe path.
:end
pause
