@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Deletes the two ActivityWatch buckets created by this project.
rem ActivityWatch requires ?force=1 when aw-server is not in testing mode.

set "AW_BASE_URL=http://localhost:5600/api/0"

for %%B in (
    aw-watcher-afk_FlorianIPad
    aw-watcher-window_FlorianIPad
) do (
    echo Deleting bucket %%B ...
    curl -sSf -X DELETE "%AW_BASE_URL%/buckets/%%B?force=1" -o NUL
    if errorlevel 1 (
        echo Failed to delete bucket %%B.
        exit /b 1
    )
)

echo Done.
exit /b 0
