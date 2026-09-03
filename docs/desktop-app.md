# Tauri 桌面安装包

本目录把现有 React + FastAPI 工作台封装为普通桌面应用。最终用户只安装 Windows `Setup.exe` 或 macOS `DMG`，不需要安装 Docker、Python、Node.js 或 Rust。

## 运行结构

- Tauri：窗口、系统托盘、应用生命周期和本地后端进程管理。
- React：继续使用现有工作台页面，由 FastAPI 从本地静态资源提供。
- Python sidecar：FastAPI、闲鱼 WebSocket、AI、订单和自动发货逻辑。
- Playwright Chromium：作为 Tauri resource 随安装包分发。
- SQLite、Cookie、日志和浏览器 Profile：写入系统分配的应用数据目录，不写安装目录。

后端只绑定 `127.0.0.1`，Tauri 每次启动分配随机端口，并使用一次性桌面令牌建立 HttpOnly 本机会话。关闭主窗口时应用缩入系统托盘，只有从托盘选择“退出”才停止后端。

## 本地构建要求

安装：

- Python 3.11
- Node.js 22
- Rust stable
- 对应平台的 Tauri 系统依赖

然后在仓库根目录执行相应步骤：

```bash
python -m pip install -r requirements.txt -r desktop/requirements-build.txt
npm ci --prefix frontend
npm run build --prefix frontend
npm install --prefix desktop

# 将浏览器下载安装到 Tauri 资源目录
PLAYWRIGHT_BROWSERS_PATH="$PWD/desktop/src-tauri/resources/playwright" \
  python -m playwright install chromium

# target 请换成当前机器对应的 Rust target triple
python desktop/scripts/build_backend.py --target x86_64-pc-windows-msvc
npm run icon --prefix desktop
npm run tauri --prefix desktop -- build --target x86_64-pc-windows-msvc --bundles nsis
```

macOS 必须在对应架构的 macOS 主机或 GitHub hosted runner 上构建；Windows 安装包建议在 Windows runner 上构建。仓库内的 `desktop-build.yml` 已配置三种产物：

- Windows 11 x64 NSIS Setup.exe
- macOS Apple Silicon DMG
- macOS Intel DMG

## 签名边界

当前 CI 生成的是测试分发包：Windows 未配置 Authenticode 证书，macOS 使用 ad-hoc 签名且未公证。正式对外分发前，应配置 Windows 代码签名证书，以及 Apple Developer ID、notarization 凭据。

macOS 测试包通过 `Entitlements.plist` 允许内置 PyInstaller 后端加载其解压后的 Python 库。该设置仅作用于应用签名，不修改系统 Gatekeeper。改成正式开发者签名时，应统一内嵌库的签名，并重新评估是否仍需要此例外。

## 安装验收

编译成功不等于安装可用。CI 除了检查原始后端，还必须在 Tauri 完成签名后，从最终 `.app/Contents/MacOS` 再次运行后端，验证健康检查和桌面会话初始化。

首次打开从 Tauri 的本地页面切换到随机 loopback 端口。Bootstrap 必须先返回一个完整页面，再跳转到工作台；不要改回直接 303 重定向，否则 WebKit 会在跨站跳转链中丢掉 `SameSite=Strict` Cookie。HttpOnly、Strict Cookie 和普通业务登录校验都应保留。桌面模式关闭包含查询参数的 Uvicorn access log，继续保留应用层的路径日志，避免记录启动令牌。

分发前还需安装后查看真实窗口，至少确认登录页能够显示；健康检查不能替代窗口验收。账号登录、扫码、收发消息及自动发货属于后续业务验收，不能仅凭安装成功宣称通过。

## 开源许可

本项目延续上游 AGPL-3.0。分发修改后的桌面版本时，应保留许可证与版权声明，并向使用者提供对应源代码。
