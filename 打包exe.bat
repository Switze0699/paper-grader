@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包批改模拟器

rem ============================================================
rem  一键打包：双击这个文件，等 2~3 分钟，exe 会自动更新。
rem
rem  ⚠ 为什么要专门有这个脚本（2026-10-05 踩过）：
rem     PyInstaller 默认把 exe 输出到 dist\ 文件夹，但桌面快捷方式
rem     指向的是**项目根目录**的 批改模拟器.exe。结果就是：
rem     打包"成功"了，双击桌面图标跑的却还是旧版本，看着像没生效。
rem     所以这里强制 --distpath 到项目根目录，打包完直接就是新版本。
rem ============================================================

if not exist ".venv\Scripts\python.exe" (
    echo   [错误] 找不到 .venv，请先双击 start_desktop.bat 把它建出来。
    pause
    exit /b 1
)

echo.
echo   正在打包，大约 2~3 分钟，请不要关窗口……
echo.

rem 先备份当前 exe（可回退）
if exist "批改模拟器.exe" (
    copy /y "批改模拟器.exe" "批改模拟器_上一次.bak.exe" >nul
)

".venv\Scripts\python.exe" -m PyInstaller build_exe.spec --noconfirm --distpath . --workpath build\build_exe

if errorlevel 1 (
    echo.
    echo   [失败] 打包出错了，看看上面的红字。
    echo   当前 exe 没有被改动，仍然可用。
    pause
    exit /b 1
)

echo.
echo   打包完成！
echo   新版本：%~dp0批改模拟器.exe
echo   旧版本已备份成：批改模拟器_上一次.bak.exe
echo.
echo   直接双击桌面图标就能用了（它指向的就是这个文件）。
echo.
pause
