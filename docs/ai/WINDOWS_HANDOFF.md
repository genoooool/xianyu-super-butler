# Windows 交接（2026-09-05）

## 当前状态

本分支已修复 Windows 上的四个桌面差异，并在默认主机“小呆电脑”
`DESKTOP-RMIV2F3` 完成源码、原生编译、冻结后端和离线 UI 验证：

1. Windows 通知点击保留原通知的账号与会话目标，交回后端做登录账号归属校验，再打开对应会话。
2. 登录页支持“记住账号和密码”，只使用当前 Windows 用户的 Credential Manager，没有明文回退。
3. 回到消息中心时重新读取快捷短语；自动发货和固定回复继续在每次执行时读取数据库，并保留账号/商品隔离。
4. Windows 后端从 PyInstaller 单文件改为目录式资源，避免每次启动先解压完整运行时。

本轮没有生成最终 Tauri/NSIS 安装包，也没有安装或发布。备用电脑上已有的
`dist/windows-20260905-installer-b790/returned/xianyu-workbench_1.0.1_x64-setup.exe`
不包含本轮修复，只保留为旧基线，不能作为当前交付件。

## 工作副本和验证输入

- 工作副本：`/Users/geno/Documents/xianyu app/xianyu-windows-20260905`
- 分支：`codex/windows-desktop-20260905`
- 本轮起点：`13abd3031e3be9c547eb3915cd10139cd648a57f`
- Windows 主机：小呆电脑 `DESKTOP-RMIV2F3` / `Administrator`
- 隔离运行根：`C:\Users\Administrator\codex-builds\xianyu-workbench\windows-fixes-20260905-0415`
- 源码快照：328 个非忽略文件，SHA256
  `ff89a13c474e5307919a8c08c9162c364898d949f4c143da74f7ee7718c173c3`
- Rust 通知测试串行修复后单独同步的文件 SHA256：
  `78c47d031914b7fc7b0802fbfafb98ce8e530b6787c65ab9df08c8399adc6d24`
- 最终 UI 验收脚本单独同步的文件 SHA256：
  `aae1904b6994fee7dad931c2caaac6fdebbea79eecf0b4e761e1ab8d74c3a9eb`
- 构建所需的生成图标单独校验，`icon.ico` SHA256：
  `7fd7b0b8dd4024f6dffa7861b38ec0833637e44ae6a06e0233010602a13b8dc4`

快照未包含环境文件、Cookie、密钥、正式数据库、上传文件或浏览器档案。所有运行数据都在新的隔离目录内。

## 改动说明

### Windows 通知点击

- Windows 使用 `notify-rust` 保留通知句柄，在通知主体被点击时把原有不透明目标放入有界队列。
- 原有轮询任务读取该队列，唤起主窗口并调用 `/desktop/notifications/activate`。
- 后端继续解析并校验 `account_id + chat_id + buyer_id`，未登录、过期、跨账号或无归属目标不会直接进入前端。
- Linux/其他平台仍走原插件路径；macOS 原生通知路径未改。

### Windows 安全保存登录

- 新增 Advapi32 `CredReadW`、`CredWriteW`、`CredDeleteW`、`CredFree` 封装。
- 密码以 UTF-16LE 凭据 blob 保存，用户名使用凭据用户名字段；服务名仍为
  `com.genoooool.xianyuworkbench.login`。
- 保存只发生在登录已成功且后端再次核对用户名和密码后；读取接口仍受桌面 bootstrap Cookie 和同源检查保护。
- 不可用或系统拒绝时返回明确错误，没有文件、数据库或 `localStorage` 明文替代。

### 无需重启即可读取修改

- 消息中心每次重新变为活动页时重新读取快捷短语，异步返回在页面离开后不会覆盖状态。
- 自动发货规则验证覆盖“保存后同一运行进程立即读取”以及另一账号仍使用自己的作用域。
- 固定回复已有测试继续覆盖保存新版本后废弃旧选择并立即读取新内容。

### Windows 启动

- Windows 的 PyInstaller 默认布局改为 `onedir`，完整目录放入 Tauri `resources/backend/`。
- Tauri 在 Windows 直接启动 `backend/xianyu-backend.exe`；健康检查、启动状态、退出回收和兼容回退逻辑保留。
- 新增 `tauri.windows.conf.json`，Windows 不再同时捆绑旧 onefile sidecar。

## 验证结果

| 检查 | 结果 |
| --- | --- |
| Mac Python 针对性回归 | 73 项通过 |
| 前端生产构建 | 通过，静态资源已重新生成 |
| 桌面源码合同 | 通过，包含 Windows 目录式资源配置 |
| 小呆电脑 Rust 全套测试 | 5 项通过；1 项需专用签名 fixture，按项目原设置忽略 |
| Windows Credential Manager | 在活动的 Administrator 桌面会话中创建、读取、更新、清除全部通过，测试项已清除，临时计划任务已撤销 |
| Windows 目录式后端构建 | 通过；109.215 秒；1,404 个文件；342,533,040 字节 |
| 目录式后端主 EXE | 21,586,339 字节；SHA256 `4e9b464591e579c6054fcc32864cef251414a10dad8fbb6ec68771935e7569ee` |
| 冻结后端烟雾 | worker、空白 Chromium、异常/正常退出回收、`/health`、桌面 bootstrap 全部通过 |
| Windows 离线 UI | 通知目标跨店隔离、会话恢复、快捷短语切回即刷新、订单买家名与 ID 均通过；无页面脚本错误 |
| 启动到健康可用，旧 onefile | 5 轮平均 4.504 秒，范围 4.495–4.527 秒 |
| 启动到健康可用，新 onedir | 5 轮平均 2.257 秒，范围 2.237–2.267 秒；约减少 49.9% |

离线 UI 截图已取回到
`dist/windows-fixes-20260905-0415/returned-ui/`，Windows 与 Mac 的 SHA256 相符。

## 小呆电脑工具链

- 隔离工具链：`C:\Users\Administrator\codex-builds\toolchains\xianyu-windows-20260905`
- Rust/Cargo：1.98.1，目标 `x86_64-pc-windows-msvc`
- PyInstaller：6.16.0
- 已按项目 `requirements.txt` 补齐冻结后端依赖。
- 已安装与项目 Playwright 1.60.0 匹配的 Chromium/Headless Shell 1223，位置为工具链内的 `playwright` 目录。
- `activate-toolchain.ps1` 只修改当前 PowerShell 进程，不修改系统 PATH。

## 尚未完成的真实桌面边界

- 尚未在真正弹出的 Windows 通知横幅上进行鼠标点击。通知响应队列和后端/前端导航两段已分别原生编译与离线验证，最终安装件仍需点一次真实通知确认系统激活行为。
- 尚未构建或安装包含本轮修复的 NSIS 包，因此也未验证安装后资源路径、托盘操作、覆盖安装和卸载。
- 没有登录真实闲鱼账号、发送消息、执行订单或发卡，也没有接触正式数据。
- `verify_notification_ui.py` 的完整跨平台脚本仍包含 macOS 专属提示音断言；Windows 本轮使用针对四项问题的离线 UI 验收入口。
- 当前安装链仍未配置 Authenticode 签名；本轮没有推送或发布。

后续若获准最终打包，应以本轮提交重新建立干净快照，在小呆电脑生成 NSIS，先做隔离安装，随后在可操作桌面上完成通知点击、记住登录、快捷短语/自动发货热更新、启动速度和托盘退出的人工验收。
