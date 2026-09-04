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

macOS 现用 PyInstaller onedir：完整运行环境位于 `Contents/Resources/backend/`，
不再每次启动都解压。代价是安装后多占约200MB磁盘，压缩安装包变化较小。
Windows 保留 onefile；旧布局仍可启动。两种布局都保留桌面壳退出监视及 SIGTERM 清理。

## 记住工作台登录

macOS 登录页可勾选“记住账号和密码”，默认不勾选；验证成功后才保存。
仅保存工作台管理员/用户账号，不是闲鱼账号。下次打开自动填入，仍需点击登录。
使用系统 Keychain 的本机 generic-password 项，不写普通文件、SQLite、日志或 localStorage，
不通过 shell 参数传递密码，不设置 iCloud 同步。取消勾选会清除保存项。
钥匙串访问被拒绝、锁定或超时会提示未完成，仍可手动登录；软件更新后系统可能再次询问访问。
本机桌面 Cookie 保护读取/清除，保存还需已登录且再次验证密码；非桌面环境不开放保存。
`--desktop-smoke` 禁用真实钥匙串访问；`--desktop-keychain-probe` 仅验证唯一临时测试项并清除它。

工作台顶部不再挂载公告组件，因此不展示滚动公告或版本更新横幅。关于页的内容不属于顶部横幅。

## 自有软件更新（1.0.0 起）

“系统设置 → 软件更新”和“关于”共用更新入口。仅管理员可操作；本轮支持 macOS，
Windows/Linux 仅提供手动下载入口。用户点击检查才访问自有 GitHub Release，
不在后台拉原作者公告，也不自动弹横幅/自动安装。

- 固定清单：`https://github.com/genoooool/xianyu-super-butler/releases/latest/download/latest.json`。
  版本、更新内容、当前平台下载地址、签名及 `data_compatibility` 随正式 Release 一起发布。
  没清单、网络失败、无新版、格式不兼容分别展示；不把检查失败当已是最新版。
- 只允许本仓库 HTTPS 下载、更高的正式版本。原生 Tauri Updater 验证签名后，
  再核对签名包内部 `update-contract.json` 的应用标识、版本、机型和数据兼容编号。
  清单不能通过冒写新版本号来安装旧签名包。压缩下载限制512MiB，解压内容限制4GiB。
- 网页不新增 updater/shell IPC 权限。普通管理员 Bearer 登录发起操作；原生轮询、
  状态回写与安装准备还需桌面启动密钥/HttpOnly Cookie。操作固定版本、防重复提交，
  下载完成后再次验证原会话。退出登录/失效后不能继续安装。
- 下载时业务照常运行；安装前用原子空闲检查暂时阻止新操作，消息/发货/HTTP操作仍繁忙则
  拒绝本次安装。已在执行的业务不被更新强行取消；等待任务不提前标记去重。
  后端完全退出后才替换程序并重启。暂时门禁只在内存中，不改店铺、AI开关或数据库。
- 不提供额外自动回退/恢复旧备份机制。升级仅替换 App，不清理用户数据目录；
  同一 Mac 用户、同一应用标识重装会读取旧数据。不能用彻底卸载工具删除关联数据。
  数据格式改变必须调整兼容编号并审查迁移，不可为让升级通过而保持假兼容。

本机发布流程（不依赖 Actions）：

1. 同步 `tauri.conf.json`、`Cargo.toml`、`desktop/package.json`、
   `app/desktop_updates.py` 的正式版本；运行相关回归。首次自有版本为1.0.0。
2. 直接 Vite 构建至新目录，再用 `build_backend.py --static-dir` 指定同一份新资源；
   `TAURI_CONFIG='{"bundle":{"resources":[],"externalBin":[]}}' cargo build --release` 编译原生壳。
   该覆盖仅用于手工组包，完整运行资源由下步复制，不改数据目录。
3. `package_macos_release.py` 接收 `--base-app`（已验证资源/浏览器包）、`--backend`、
   `--native`、全新 `--output-dir`、仓库外 `--signing-key` 和 `--notes`。
   它核对原生版本/公钥，生成并签名 `.app.tar.gz`、`.sig`、`latest.json` 和手动安装 ZIP。
   私钥权限必须600，仅保存在本机；脚本不上传、安装或清除旧资源。
4. 对最终包运行 `verify_updates_ui.py`、`verify_native_updates.py` 与
   `verify_updater_install.py`。最后一个在 `/private/tmp` 的标记副本中运行真实安装器，
   不使用正式数据，测试后保留证据；测试用HTTP/版本覆盖只在Rust `cfg(test)` 内。
5. 经明确发布授权，建立同版本 `v版本号` Release，上传上述四个发布文件；不要把
   `inherited-backend-unused`、私钥、日志、测试数据或整个输出目录上传。

当前包为本机 ad-hoc macOS 签名，并非 Apple Developer ID 公证；更新包签名与苹果公证
是两件事。首次从网络下载安装可能遇到系统安全提示。首个带更新器的版本需要手动安装一次，
后续正式新版才能应用内更新。公开跨版本升级仍须在发布后实测，不以离线副本替换代替。

## 本机新消息弹窗

登录工作台后默认启用，可在“系统设置 → 账号与同步 → 本机消息提醒”关闭或提交测试提醒。
原生桌面壳每 2 秒读取本机内存通知队列，不额外请求闲鱼，也不依赖消息页面是否打开。
关闭窗口缩入托盘后仍可提醒；完整退出、退出登录、会话过期或关闭开关后停止。
系统通知权限和免打扰设置决定是否实际弹出；“测试提醒已提交”不是系统送达回执。
macOS 使用系统 UserNotifications 框架，前台回调明确请求普通横幅和通知中心展示，
因此工作台在最前面时也能提醒；不绕过系统关闭、专注模式或静默授权，不申请关键提醒权限。
macOS 的“消息提示音”默认开启，可独立关闭；弹窗与声音开关按工作台用户保存至现有本机设置表，重开软件后恢复，关闭弹窗时不发声。
提示音采用系统通知的默认声音，前台、后台都遵循该开关，不用直接播放音频来绕过免打扰。
第一次需要声音时申请普通 alert + sound 授权；已有授权仍由系统决定是否允许声音，
不会自动调整系统音量或通知设置。若有横幅无声音，请检查系统音量和该应用的通知声音开关。
原生调度失败会写入 macOS 系统日志（`Xianyu notification`），不记录买家信息。

消息入口过滤自身发言、群消息、现有 skip_notify 规则、旧消息和重复消息。
队列只面向当前登录的工作台用户，数量有上限且 90 秒过期；不保存买家姓名和聊天内容，只向桌面壳返回数量、游标与提示音偏好。
原生轮询同时需要已有桌面 HttpOnly Cookie 和启动密钥，请求不会放宽普通接口鉴权或 WebView IPC 权限。
外部通知渠道和自动回复/发货流程保持不变。

启动时只加载当前页面，访问过的页面保留状态；Excel 与 AI 测试接口的重型依赖改为首次使用时加载。
桌面启动日志记录本地后端就绪耗时，避免把账号网络登录耗时混为界面加载耗时。

## AI 知识库

入口：“AI 回复 → 选择当前账号 → 打开商品与店铺知识库”。默认编辑当前店铺，
也可切换到“共用资料”或“商品专属”；商品必须先在当前店铺商品列表中存在。
共用资料只在同一工作台用户拥有的店铺之间通用，不向其他工作台用户共享。

- 同一主题名称（忽略全半角、大小写与连续空格）先按 **商品专属 > 店铺 > 共用** 覆盖，
  再检索；不同主题仍可互相补充。请给需要覆盖的规则使用相同主题名称。
- 支持手动填写问答/资料，以及 UTF-8（可带 BOM）的 `.txt`、`.md` 上传。
  文件最多256KB、60000字。上传只预览；确认后保存为一条可编辑资料并启用，原文件不另存。
  同范围同名导入返回冲突，不悄悄覆盖旧资料；需在原资料中编辑。取消预览不写数据库。
- 单条最多60000字、每用户最多500条/合计100万字。短问答（不超过2000字）保持完整，
  长文按约1400字分段并携带附近标题；先按整个主题覆盖，再检索分段，避免旧文档尾部规则复活。
  按主题、问法/触发词与内容词项匹配，最多6段；不是向量语义检索或模型训练。
  不解析 Word/PDF，不执行 Markdown/HTML/脚本或下载远程资源。
- “检索预览”只展示最终命中资料及来源，不调用模型、不发送买家消息。
  编辑、停用、重新启用后，下一次 AI 回复使用新规则；停用高层资料会恢复下层同主题资料。
- 原有关键词回复优先级、人工接管、AI 开关与议价/交易状态限制不变。
  资料仅在 AI 分支使用；未命中时保留原有商品信息、上下文及回复规则。
  模型仍可能理解错误，知识库不保证自动回复绝不出错。
- 命中内容会发给当前配置的 AI 服务商，请勿存放密码、卡密、买家隐私。
  资料在本机 SQLite 新增独立表中保存，不改现有聊天或订单字段。
- JSON 备份包含知识资料；恢复时校验用户/店铺归属并按同范围同主题合并，
  不会删除备份中未出现的知识条目。旧备份无知识表时保留现有资料；完整数据库备份也包含资料。

`tests/test_ai_knowledge.py` 覆盖优先级、用户和店铺隔离、停用回退、检索与恢复校验。
`verify_knowledge_ui.py` 用正式前端、真实知识接口和隔离 SQLite 测试页面操作；
卖家/模型接口仅使用本机固定测试数据，不连接真实业务。

### 输入长度保护

模型输入设20000个 UTF-8 字节的保守大小上限（每消息另计64字节开销），
知识片段最多占9000字节；仅加入完整片段，优先保留最近连续历史。
不截断当前问题、商品事实、自定义提示词或议价/交易安全约束；这些必需内容超限时不调用模型。
有知识命中但一段也放不下时同样停止本次 AI 生成，避免静默忽略规则。
字节预算不是精确 tokenizer；输出/推理预算仍独立限制在最多8000 tokens，
不能保证任意自定义小窗口模型不会超限。建议模型上下文至少32K；供应商拒绝时沿用失败回退。

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

本机优先构建，不自动触发 CI。macOS 的后端默认 `--layout onedir`，平台配置将
`resources/backend/` 整体打包；Windows 默认 onefile。
`build_backend.py` 每次用新目录且不删除旧产物，目标 staging 已存在时明确停止。
可传 `--output-dir`、`--stage-dir`、`--static-dir` 使用独立构建目录；再次构建前先保留旧 staging。

## 签名边界

当前 CI 生成的是测试分发包：Windows 未配置 Authenticode 证书，macOS 使用 ad-hoc 签名且未公证。正式对外分发前，应配置 Windows 代码签名证书，以及 Apple Developer ID、notarization 凭据。

macOS 测试包通过 `Entitlements.plist` 允许内置 PyInstaller 后端加载其解压后的 Python 库。该设置仅作用于应用签名，不修改系统 Gatekeeper。改成正式开发者签名时，应统一内嵌库的签名，并重新评估是否仍需要此例外。

## 安装验收

编译成功不等于安装可用。CI 除了检查原始后端，还必须在 Tauri 完成签名后，从最终 `.app/Contents/Resources/backend/xianyu-backend` 再次运行后端，验证健康检查和桌面会话初始化。

本机更新默认在本机构建，不需要触发 GitHub Actions。只改前后端时可复用现有内置 Chromium，不重新下载浏览器；桌面壳有变更时在本机重新编译。更新应用前保留旧 `.app`，不覆盖应用数据目录。跨平台安装包才另行安排对应主机或云端构建。

`desktop/app-icon.svg` 保留原图案，视口四周各留约10%透明边距，使 Dock 中的可见图案不再铺满图标槽位。
修改后必须重新生成 Tauri 图标并重新编译桌面壳，同时更新 `.app` 中的 `icon.icns`；不能只替换源码 SVG。

`smoke_backend.py` 现在还会在隔离目录运行 `--desktop-runtime-probe`：真实启动 spawn 辅助进程和空白 Chromium，分别检查正常终止、强制关闭 onefile 启动器后的所有已跟踪子进程退出。该模式不初始化业务数据库，也不加载账号。必须先通过该检查，再执行普通后端健康与桌面会话检查；不能仅凭登录页面正常就宣布后台生命周期通过。

onedir 验收使用独立启动器，验证正常终止和强制退出启动器后后端/浏览器均退出；
不是强制杀死负责清理的后端本身。`benchmark_startup.py` 交替启动前后版本各3次，
每次全新空库，保留日志，测量至健康端点；不等同于真实店铺联网或整个窗口的启动时间。

首次打开从 Tauri 的本地页面切换到随机 loopback 端口。Bootstrap 必须先返回一个完整页面，再跳转到工作台；不要改回直接 303 重定向，否则 WebKit 会在跨站跳转链中丢掉 `SameSite=Strict` Cookie。HttpOnly、Strict Cookie 和普通业务登录校验都应保留。桌面模式关闭包含查询参数的 Uvicorn access log，继续保留应用层的路径日志，避免记录启动令牌。

分发前还需安装后查看真实窗口，至少确认登录页能够显示；健康检查不能替代窗口验收。账号登录、扫码、收发消息及自动发货属于后续业务验收，不能仅凭安装成功宣称通过。

`verify_notification_ui.py` 使用签名后端、隔离空数据库及内置 Chromium，验证首屏按需加载、弹窗/提示音开关持久化、测试队列、轮询保护与退出登录。该脚本只提交合成提醒，不发送买家消息。
桌面壳提供 `--desktop-smoke` 启动参数，为安装验收创建独立的临时数据目录，不读取真实卖家数据库；测试数据保留供核查。
`desktop/tests/macos_notifications_test.m` 离线验证前台展示/声音开关、旧版声音授权升级及拒绝/静默授权分支，不访问系统通知中心。
原生提醒还须在已授权的签名 App 内分别验收前台、后台横幅；仅观察到授权提示或系统接收日志不算横幅验收通过。
声音请求被系统接受也不等于用户实际听到，最终听感需由用户确认。

## 开源许可

本项目延续上游 AGPL-3.0。分发修改后的桌面版本时，应保留许可证与版权声明，并向使用者提供对应源代码。
