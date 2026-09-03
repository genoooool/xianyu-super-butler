# Tauri 桌面安装包

本目录把现有 React + FastAPI 工作台封装为普通桌面应用。最终用户只安装 Windows `Setup.exe` 或 macOS `DMG`，不需要安装 Docker、Python、Node.js 或 Rust。

## 运行结构

- Tauri：窗口、系统托盘、应用生命周期和本地后端进程管理。
- React：继续使用现有工作台页面，由 FastAPI 从本地静态资源提供。
- Python sidecar：FastAPI、闲鱼 WebSocket、AI、订单和自动发货逻辑。
- Playwright Chromium：作为 Tauri resource 随安装包分发。
- SQLite、Cookie、日志和浏览器 Profile：写入系统分配的应用数据目录，不写安装目录。

后端只绑定 `127.0.0.1`，Tauri 每次启动分配随机端口，并使用一次性桌面令牌建立 HttpOnly 本机会话。关闭主窗口时应用缩入系统托盘，只有从托盘选择“退出”才停止后端。

macOS 的 `⌘Q` 也会完整退出。后端在任何配置、数据库或第三方模块初始化之前调用 `multiprocessing.freeze_support()`，防止打包后的辅助进程递归启动工作台。桌面模式监视 onefile 启动器及桌面壳的进程身份；启动器消失时取消主任务并回收自身子进程，退出卡住时有超时兜底。macOS 正常退出先发送 SIGTERM，让 onefile 启动器清理临时解包目录。

工作台顶部不再挂载公告组件，因此不展示滚动公告或版本更新横幅。关于页的内容不属于顶部横幅。

## 本机新消息弹窗

登录工作台后默认启用，可在“系统设置 → 账号与同步 → 本机消息提醒”关闭或提交测试提醒。
原生桌面壳每 2 秒读取本机内存通知队列，不额外请求闲鱼，也不依赖消息页面是否打开。
关闭窗口缩入托盘后仍可提醒；完整退出、退出登录、会话过期或关闭开关后停止。
系统通知权限和免打扰设置决定是否实际弹出；“测试提醒已提交”不是系统送达回执。
macOS 使用系统 UserNotifications 框架，前台回调明确请求普通横幅和通知中心展示，
因此工作台在最前面时也能提醒；不绕过系统关闭、专注模式或静默授权，不申请关键提醒权限。
原生调度失败会写入 macOS 系统日志（`Xianyu notification`），不记录买家信息。

消息入口过滤自身发言、群消息、现有 skip_notify 规则、旧消息和重复消息。
队列只面向当前登录的工作台用户，数量有上限且 90 秒过期；不保存买家姓名和聊天内容，只向桌面壳返回数量与游标。
原生轮询同时需要已有桌面 HttpOnly Cookie 和启动密钥，请求不会放宽普通接口鉴权或 WebView IPC 权限。
外部通知渠道和自动回复/发货流程保持不变。

启动时只加载当前页面，访问过的页面保留状态；Excel 与 AI 测试接口的重型依赖改为首次使用时加载。
桌面启动日志记录本地后端就绪耗时，避免把账号网络登录耗时混为界面加载耗时。

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

本机更新默认在本机构建，不需要触发 GitHub Actions。只改前后端时可复用现有内置 Chromium，不重新下载浏览器；桌面壳有变更时在本机重新编译。更新应用前保留旧 `.app`，不覆盖应用数据目录。跨平台安装包才另行安排对应主机或云端构建。

`smoke_backend.py` 现在还会在隔离目录运行 `--desktop-runtime-probe`：真实启动 spawn 辅助进程和空白 Chromium，分别检查正常终止、强制关闭 onefile 启动器后的所有已跟踪子进程退出。该模式不初始化业务数据库，也不加载账号。必须先通过该检查，再执行普通后端健康与桌面会话检查；不能仅凭登录页面正常就宣布后台生命周期通过。

首次打开从 Tauri 的本地页面切换到随机 loopback 端口。Bootstrap 必须先返回一个完整页面，再跳转到工作台；不要改回直接 303 重定向，否则 WebKit 会在跨站跳转链中丢掉 `SameSite=Strict` Cookie。HttpOnly、Strict Cookie 和普通业务登录校验都应保留。桌面模式关闭包含查询参数的 Uvicorn access log，继续保留应用层的路径日志，避免记录启动令牌。

分发前还需安装后查看真实窗口，至少确认登录页能够显示；健康检查不能替代窗口验收。账号登录、扫码、收发消息及自动发货属于后续业务验收，不能仅凭安装成功宣称通过。

`verify_notification_ui.py` 使用签名后端、隔离空数据库及内置 Chromium，验证首屏按需加载、提醒开关持久化、测试队列、轮询保护与退出登录。该脚本只提交合成提醒，不发送买家消息。
桌面壳提供 `--desktop-smoke` 启动参数，为安装验收创建独立的临时数据目录，不读取真实卖家数据库；测试数据保留供核查。
`desktop/tests/macos_notifications_test.m` 离线验证前台展示回调和授权分支，不访问系统通知中心。
原生提醒还须在已授权的签名 App 内分别验收前台、后台横幅；仅观察到授权提示或系统接收日志不算横幅验收通过。

## 开源许可

本项目延续上游 AGPL-3.0。分发修改后的桌面版本时，应保留许可证与版权声明，并向使用者提供对应源代码。
