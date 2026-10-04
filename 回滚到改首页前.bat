@echo off
chcp 65001 >nul
echo ============================================================
echo   一键回退：恢复到"改首页之前"的状态
echo ============================================================
echo.
echo 这一步会把下面这些文件换回备份版本：
echo    app\screens\setup.py    （首页）
echo    core\qbank.py
echo    core\models.py
echo    core\pipeline.py
echo.
echo 当前的文件会先另存到「回滚点_当前的」里，所以这一步本身也能反悔。
echo.
pause

set ROOT=%~dp0
if not exist "%ROOT%回滚点_改首页前" (
    echo 找不到备份文件夹「回滚点_改首页前」，无法回退。
    pause
    exit /b 1
)

rem ---- 1. 先把当前版本存起来 ----
if not exist "%ROOT%回滚点_当前的\app\screens" mkdir "%ROOT%回滚点_当前的\app\screens"
copy /Y "%ROOT%app\screens\setup.py" "%ROOT%回滚点_当前的\app\screens\" >nul
copy /Y "%ROOT%core\qbank.py"          "%ROOT%回滚点_当前的\" >nul
copy /Y "%ROOT%core\models.py"          "%ROOT%回滚点_当前的\" >nul
copy /Y "%ROOT%core\pipeline.py"        "%ROOT%回滚点_当前的\" >nul
echo 已把当前版本另存到「回滚点_当前的」

rem ---- 2. 换回备份版本 ----
copy /Y "%ROOT%回滚点_改首页前\app\screens\setup.py" "%ROOT%app\screens\" >nul
copy /Y "%ROOT%回滚点_改首页前\qbank.py"               "%ROOT%core\" >nul
copy /Y "%ROOT%回滚点_改首页前\models.py"              "%ROOT%core\" >nul
copy /Y "%ROOT%回滚点_改首页前\pipeline.py"            "%ROOT%core\" >nul
echo 已恢复到"改首页之前"的状态

echo.
echo ⚠ 如果你是双击 exe 用的，还要重新打包一次才生效。
echo    （让项目里的 codex 或 AI 跑一次打包即可）
echo.
pause
