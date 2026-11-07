@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title A股量化交易平台

rem ---- 1. 寻找 Python 3.11+（可与旧版本 Python 共存）----
set "PY="
for %%V in (3.13 3.12 3.11) do (
  if not defined PY (
    py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
  )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo.
  echo [错误] 没有找到 Python 3.11 或更高版本（akshare 最新版要求 3.11+）。
  echo        请到 https://www.python.org/downloads/ 下载安装 Python 3.12，
  echo        安装时勾选 "Add python.exe to PATH"，装好后重新双击本文件。
  echo.
  pause
  exit /b 1
)
echo 使用 Python: %PY%

rem ---- 2. 首次运行：创建独立虚拟环境 .venv ----
if not exist ".venv\Scripts\python.exe" (
  echo 首次运行，正在创建虚拟环境...
  %PY% -m venv .venv
  if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    pause
    exit /b 1
  )
)
set "VPY=.venv\Scripts\python.exe"

rem ---- 3. 安装 / 更新依赖（先用清华镜像，失败再用官方源）----
echo 正在检查依赖（首次需要几分钟）...
"%VPY%" -m pip install -q --disable-pip-version-check -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
  echo 镜像安装失败，改用官方源重试...
  "%VPY%" -m pip install -q --disable-pip-version-check -r requirements.txt
  if errorlevel 1 (
    echo [错误] 安装依赖失败，请把上面的报错发给我
    pause
    exit /b 1
  )
)

rem ---- 4. 启动平台，5 秒后自动打开浏览器 ----
rem 用法：双击 = 仿真行情；命令行 start.bat --feed akshare = 东方财富实时行情
start "" cmd /c "timeout /t 5 /nobreak >nul & start http://127.0.0.1:8000"
echo.
echo 平台启动中，浏览器会自动打开 http://127.0.0.1:8000
echo 使用期间请不要关闭本窗口；按 Ctrl+C 停止。
echo.
"%VPY%" -m quant run %*
echo.
echo 平台已停止。如果上面有报错，请截图发给我。
pause
