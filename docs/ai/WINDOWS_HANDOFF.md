# Windows 交付（2026-09-11）

- 1.0.5 已同步所选账号的人工验证修复，运行源码为 `82e78a3`；复用已验收的 Windows 原生程序和内置浏览器。
- 默认人工验证在所选账号的专用官网窗口进行，包含扫码保持登录确认、持久验证等待、验证成功后保存并关闭窗口。
- 106 项相关 Python 测试、离线界面和有头浏览器夹具、10 个冻结模块源码匹配、空库启动及退出清理通过。安装文件与受检候选一致，原有 37 张数据表、结构和配置保持不变。
- 实际账号验证已覆盖重新扫码保存和消息连接；平台要求人脸或滑块时仍需用户本人完成。单次恢复不代表长期免验证。
- 私有账号状态、截图、数据快照和恢复路径只保留在本地交付记录，不作为公开发行附件。

以下为历史记录，当前版本以上述交付状态及正式 Release 为准。

# Windows 交接（2026-09-05）

## 当前状态

Windows修复已由根任务整合到8642dec，并在默认主机“小呆电脑”
`DESKTOP-RMIV2F3` 完成原生构建、覆盖安装、真实桌面启动和离线 UI 验证：

1. Windows 通知点击保留原通知的账号与会话目标，交回后端做登录账号归属校验，再打开对应会话。
2. 登录页支持“记住账号和密码”，只使用当前 Windows 用户的 Credential Manager，没有明文回退。
3. 回到消息中心时重新读取快捷短语；自动发货和固定回复继续在每次执行时读取数据库，并保留账号/商品隔离。
4. Windows 后端从 PyInstaller 单文件改为目录式资源，避免每次启动先解压完整运行时。

已从1.0.1升级到1.0.2，安装位置`D:\闲鱼工作台`，当前程序保持退出。最终安装包为
`dist/windows-final-1.0.2-20260905-0445/returned/闲鱼工作台_1.0.2_x64-setup.exe`，
SHA256 `8c182ef4146051202af91bc81a8a1254cfeeac551f7c02e6e195f081dbf62966`；未发布。

## 工作副本和验证输入

- 工作副本：`/Users/geno/Documents/xianyu app/xianyu-windows-20260905`
- 分支：`codex/windows-desktop-20260905`
- Windows修复提交：`6495bfd24e3979f3403b8dbec803c02b09a4f1af`
- 最终根提交：`8642decec8e6cab91c14b7a65986c7d50727ac89`
- Windows 主机：小呆电脑 `DESKTOP-RMIV2F3` / `Administrator`
- 最终隔离运行根：`C:\Users\Administrator\codex-builds\xianyu-workbench\wf102-0445`
- 最终源码快照：329 个受控文件，SHA256
  `acbf8c781e4c6ef1c6f39797ff8cf1692fd8b5d77fc9017fd7a1b27e829c52c9`
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
| 最终NSIS | 1.0.2，309,650,690字节，SHA256 `8c182ef4…dbf62966`；Authenticode未签名 |
| 覆盖安装 | 原D盘路径1.0.1→1.0.2；程序/数据双备份通过；注册表和主程序版本一致 |
| 安装版交互桌面 | 后端1.58秒就绪；记住登录复选框可见；凭据元数据未变；退出无残留进程 |
| 最终真实数据 | 结构、37张表内容、配置与安装前一致；买家消息0，经营动作0 |

离线 UI 截图已取回到
`dist/windows-fixes-20260905-0415/returned-ui/`，Windows 与 Mac 的 SHA256 相符。

## 小呆电脑工具链

- 隔离工具链：`C:\Users\Administrator\codex-builds\toolchains\xianyu-windows-20260905`
- Rust/Cargo：1.98.1，目标 `x86_64-pc-windows-msvc`
- PyInstaller：6.16.0
- 已按项目 `requirements.txt` 补齐冻结后端依赖。
- 已安装与项目 Playwright 1.60.0 匹配的 Chromium/Headless Shell 1223，位置为工具链内的 `playwright` 目录。
- `activate-toolchain.ps1` 只修改当前 PowerShell 进程，不修改系统 PATH。

## 安装结果、备份与剩余边界

- 程序备份：`D:\codex-backups\xianyu-workbench-before-1.0.2-20260905-0525`；数据备份：`C:\Users\Administrator\codex-builds\xianyu-workbench\wf102-0445\backup-before-install-20260905-0525`。完整结果在本机`dist/windows-final-1.0.2-20260905-0445/returned/FINAL_RESULT.json`。
- 安装版交互启动会按既有逻辑刷新`cookies.value`并推进`sqlite_sequence`。验收已保留启动后数据库副本，再从受检安装前备份恢复并逐表确认；以后用户正常启动仍可能再次刷新。
- 仍未在真正弹出的Windows通知横幅上鼠标点击。通知响应队列、后端归属校验和前端精确跳转已分别原生/离线验证，下一条真实新消息由用户实点即可。
- 没有登录、保存测试凭据、发送消息、执行订单/发卡或改变经营配置。离线UI外网完成请求为0；真实桌面启动没有做全网监控，因此不声称其完全无网络请求。
- 当前安装链仍未配置Authenticode签名，手动安装可能显示未知发布者；没有推送或发布Release。
