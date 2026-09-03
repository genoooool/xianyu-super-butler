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

## 开源许可

本项目延续上游 AGPL-3.0。分发修改后的桌面版本时，应保留许可证与版权声明，并向使用者提供对应源代码。
