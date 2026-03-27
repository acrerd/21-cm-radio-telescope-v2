@echo off
REM Generate code coverage report for coordinate functions

echo === Running tests with coverage ===
call pio test -e native-coverage

echo.
echo === Generating coverage report ===
C:\Strawberry\c\bin\gcov.exe -o .pio\build\native-coverage\src src\coordinates.cpp

echo.
echo === Coverage Summary ===
echo See coordinates.cpp.gcov for line-by-line details
echo.
echo To view uncovered lines:
echo   findstr /C:"#####" coordinates.cpp.gcov
