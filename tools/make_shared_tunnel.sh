#!/bin/bash
# Make the shared, tunnel-only ssh key for ettus3 and the self-contained
# Windows batch file that carries it, for handing to anyone who needs the
# scheduler and controller pages.
#
#   tools/make_shared_tunnel.sh            make the key (once) and the .bat
#   tools/make_shared_tunnel.sh --rotate   revoke the old key, make a new one
#   tools/make_shared_tunnel.sh --revoke   remove the key from ettus3, nothing else
#
# Output goes to ~/srt-tunnel/, outside the repository: the .bat contains a
# private key and must never be committed.
#
# The key is installed in ~/.ssh/authorized_keys with
#   restrict,port-forwarding,permitopen=<the two pages>,command="/bin/true"
# so whoever holds it can open exactly the two forwards the tunnel needs and
# nothing else: no shell, no command, no other port, no agent or X11. A leaked
# copy gives the two web pages (which can move the dish), not the computer.
# Rotating replaces it for everybody at once.
set -euo pipefail

OUT="$HOME/srt-tunnel"
KEY="$OUT/srt_tunnel_key"
BAT="$OUT/ettus3-tunnel-shared.bat"
AUTH="$HOME/.ssh/authorized_keys"
HOST=ettus3.astro.gla.ac.uk
TAG=shared-tunnel-only
OPTS='restrict,port-forwarding,permitopen="192.168.50.120:80",permitopen="127.0.0.1:5000",command="/bin/true"'

revoke() {
    if grep -q " $TAG-" "$AUTH"; then
        cp -p "$AUTH" "$AUTH.bak.$(date +%Y%m%d%H%M%S)"
        grep -v " $TAG-" "$AUTH" > "$AUTH.new" || true
        chmod 600 "$AUTH.new"
        mv "$AUTH.new" "$AUTH"
        echo "Removed the shared tunnel key from $AUTH"
    fi
    rm -f "$KEY" "$KEY.pub"
}

case "${1:-}" in
    --revoke) revoke; rm -f "$BAT"; exit 0 ;;
    --rotate) revoke ;;
    "") ;;
    *) echo "usage: $0 [--rotate|--revoke]" >&2; exit 2 ;;
esac

mkdir -p "$OUT"
chmod 700 "$OUT"

if [ ! -e "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "$TAG-$(date +%Y%m%d)" -f "$KEY"
fi
PUB=$(cat "$KEY.pub")
if ! grep -qF "$PUB" "$AUTH"; then
    cp -p "$AUTH" "$AUTH.bak.$(date +%Y%m%d%H%M%S)"
    printf '%s %s\n' "$OPTS" "$PUB" >> "$AUTH"
    echo "Installed the shared tunnel key in $AUTH"
fi

# cmd's echo would mangle & | < > ^ % ( ) ! - base64 has none, but check.
if grep -q '[&|<>^%()!]' "$KEY"; then
    echo "private key contains a character cmd cannot echo" >&2
    exit 1
fi
HOSTKEY=$(cut -d' ' -f1,2 /etc/ssh/ssh_host_ed25519_key.pub)

{
cat <<'EOF'
@echo off
setlocal EnableExtensions

rem ===========================================================================
rem  ettus3-tunnel-shared.bat - the Acre Road telescope's web pages
rem
rem  Double-click it. It opens an ssh tunnel to ettus3 and starts your browser
rem  on the scheduler (http://127.0.0.1:5000/) and the telescope controller
rem  (http://127.0.0.1:8080/). Leave its window open while you use them;
rem  press any key in it to close the tunnel.
rem
rem  Needs the campus network or the university VPN, and Windows 10 or 11
rem  (which include ssh). Nothing to install, no password.
rem
rem  This file contains a key that lets it open the tunnel, and nothing
rem  else - it cannot log in to ettus3. Do not pass it on outside the
rem  project; the pages it opens can move the dish.
rem ===========================================================================

set "DIR=%LOCALAPPDATA%\ettus3-tunnel"
set "KEYFILE=%DIR%\key"
set "KNOWN=%DIR%\known_hosts"
set "CFG=%DIR%\config"
set "PORT1=8080"
set "PORT2=5000"
set "URL1=http://127.0.0.1:5000/"
set "URL2=http://127.0.0.1:8080/"

where ssh.exe >nul 2>&1
if errorlevel 1 (
    echo ERROR: ssh.exe not found.
    echo Install it via: Settings ^> Apps ^> Optional Features ^> OpenSSH Client
    pause
    exit /b 1
)

call :PortBusy %PORT1%
if errorlevel 1 (
    echo ERROR: local port %PORT1% is already in use - is the tunnel already open?
    pause
    exit /b 1
)
call :PortBusy %PORT2%
if errorlevel 1 (
    echo ERROR: local port %PORT2% is already in use - is the tunnel already open?
    pause
    exit /b 1
)

rem --- Write the key, the host's fingerprint and an ssh config of our own ---
rem Our own config (-F) means nothing in your ~/.ssh is read or changed.
if not exist "%DIR%" mkdir "%DIR%"
if exist "%KEYFILE%" del /f /q "%KEYFILE%"
> "%KEYFILE%" (
EOF
sed 's/^/echo /' "$KEY"
cat <<EOF
)
> "%KNOWN%" echo ettus3-srt $HOSTKEY
EOF
cat <<'EOF'
set "KEYF=%KEYFILE:\=/%"
set "KNOWNF=%KNOWN:\=/%"
> "%CFG%" (
echo Host ettus3-srt
echo   HostName ettus3.astro.gla.ac.uk
echo   User astro
echo   IdentityFile "%KEYF%"
echo   IdentitiesOnly yes
echo   HostKeyAlias ettus3-srt
echo   UserKnownHostsFile "%KNOWNF%"
echo   StrictHostKeyChecking yes
echo   CheckHostIP no
echo   BatchMode yes
echo   ConnectTimeout 10
echo   ServerAliveInterval 30
echo   ServerAliveCountMax 3
echo   ExitOnForwardFailure yes
)

rem Windows ssh refuses a key or config that other accounts can read.
for /f "delims=" %%U in ('whoami') do set "ME=%%U"
icacls "%KEYFILE%" /inheritance:r /grant:r "%ME%:F" >nul
if errorlevel 1 (
    echo ERROR: could not set the permissions on %KEYFILE%
    pause
    exit /b 1
)
icacls "%CFG%" /inheritance:r /grant:r "%ME%:F" >nul

rem --- Pre-flight, while errors are still visible ----------------------------
echo Checking the connection to ettus3 ...
ssh -F "%CFG%" ettus3-srt true
if errorlevel 1 (
    echo.
    echo ERROR: could not connect to ettus3. Most often:
    echo   - not on the campus network: start the university VPN and retry
    echo   - the key in this file has been replaced: ask for the current one
    echo Details:  ssh -v -F "%CFG%" ettus3-srt true
    pause
    exit /b 1
)

rem --- Start the tunnel hidden ------------------------------------------------
echo Opening tunnel ...
set "SSHPID="
for /f "usebackq delims=" %%P in (`powershell -NoProfile -Command "(Start-Process -FilePath ssh -WindowStyle Hidden -PassThru -ArgumentList ('-F \"' + $env:CFG + '\" -N -L 8080:192.168.50.120:80 -L 5000:127.0.0.1:5000 ettus3-srt')).Id"`) do set "SSHPID=%%P"
if not defined SSHPID (
    echo ERROR: failed to start ssh.
    pause
    exit /b 1
)

set /a TRIES=0
:wait
call :PortBusy %PORT1%
if errorlevel 1 goto ready
tasklist /FI "PID eq %SSHPID%" 2>nul | find "%SSHPID%" >nul
if errorlevel 1 (
    echo.
    echo ERROR: ssh exited before the tunnel came up.
    echo Diagnose with:  ssh -v -F "%CFG%" -N -L 8080:192.168.50.120:80 -L 5000:127.0.0.1:5000 ettus3-srt
    pause
    exit /b 1
)
set /a TRIES+=1
if %TRIES% GEQ 60 (
    echo ERROR: timed out waiting for the tunnel.
    goto cleanup
)
timeout /t 1 /nobreak >nul
goto wait

:ready
echo Tunnel is up.
start "" "%URL1%"
start "" "%URL2%"
echo.
echo   %URL1%   scheduler
echo   %URL2%   telescope controller
echo.
echo Leave this window open. Press any key to close the tunnel.
pause >nul

:cleanup
echo Closing tunnel ...
taskkill /PID %SSHPID% /T /F >nul 2>&1
del /f /q "%KEYFILE%" "%CFG%" "%KNOWN%" >nul 2>&1
echo Done.
timeout /t 2 /nobreak >nul
exit /b 0

:PortBusy
powershell -NoProfile -Command "if (Get-NetTCPConnection -State Listen -LocalPort %1 -ErrorAction SilentlyContinue) { exit 1 } else { exit 0 }"
exit /b %errorlevel%
EOF
} | sed 's/$/\r/' > "$BAT"
chmod 600 "$BAT"

echo "Wrote $BAT"
echo "Test it from this machine:"
echo "  ssh -i $KEY -o IdentitiesOnly=yes -N -L 18080:192.168.50.120:80 -L 15000:127.0.0.1:5000 astro@$HOST"
