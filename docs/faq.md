# 闲鱼工作台常见问题

本文适用于本仓库桌面版1.0.3。版本变更以[Release](https://github.com/genoooool/xianyu-super-butler/releases/latest)和[版本记录](../CHANGELOG.md)为准。

## 闲鱼工作台是什么？与闲鱼超级管家有什么关系？

闲鱼工作台（Xianyu Workbench）是面向闲鱼卖家的社区开源工具，用于多账号消息接待、固定QA、AI回复、人工接管、商品和订单管理、数字商品发货。本仓库基于[23Star/xianyu-super-butler](https://github.com/23Star/xianyu-super-butler)维护，上游又继承了xianyu-auto-reply的相关工作。它不是闲鱼官方客户端，也不代表平台承诺接口长期可用。

## 支持 Windows、Mac、Linux 和 NAS 吗？

本仓库桌面发行物覆盖Windows x64和macOS Apple Silicon。Linux、NAS可以参考[部署说明](deployment.md)运行源码或Docker服务；网页部署和桌面应用有不同的登录与更新方式。本Release不提供已验证的Intel Mac或Linux桌面安装包。

## 下载哪个版本？上游 Docker 镜像包含本仓库的修复吗？

桌面版从[本仓库Releases](https://github.com/genoooool/xianyu-super-butler/releases/latest)下载。README中保留的`ghcr.io/23star/xianyu-super-butler`属于上游预构建镜像；需要本分支修复时，应从本仓库源码自行构建，而不是假设上游镜像含有相同改动。

## 多账号消息会混到一起吗？

消息中心可以显示所有在线店铺的会话，但读取、发送、重试和通知跳转仍按“账号+会话”定位。界面显示所属店铺，不把不同账号下相同的会话ID当成同一条会话。这是软件的隔离机制，不是对平台永不异常的保证。

## 人工回复后，AI会自动恢复吗？

人工接管会关闭对应会话的AI，直到你显式重新开启。店铺客服总开关可以暂停整店自动客服；重新打开总开关不会批量打开已经手动关闭的会话。正在生成的旧回复会在发送前重新检查状态。

## 固定QA和知识库有什么区别？

固定QA用于发送已配置的原文和原图，AI负责选择适合的答案。知识资料用于结合上下文组织回复。支持商品、店铺和共用资料，导入格式为TXT/Markdown；不应把没有实现的PDF、Word或OCR导入当成现有能力。

## AI模型免费吗？支持哪些模型？

软件支持配置兼容OpenAI协议的模型服务，需要自行填写服务地址、模型与API密钥。软件不附赠模型额度；是否收费、是否兼容和响应质量取决于所选服务。固定回复与AI回复是不同配置，不要求每条消息都调用模型。

## 能保证自动发货不重复、AI绝不出错吗？

不能。系统按平台明确成功回执处理发送和发货状态，对未知或部分成功结果不自动盲重发；它不提供跨重启的完整“恰好发送一次”账本。复杂议价、售后和异常订单仍应人工核对。

## 闲鱼授权只能用24小时吗？重启后必须扫码吗？

工作台登录与闲鱼授权是两层状态。1.0.3取消桌面端本次运行内固定24小时的工作台登录期限，并增加闲鱼保持登录和静默续期。重启会结束工作台会话，但不会因这一动作主动删除已保存的闲鱼授权。平台授权能否继续使用取决于凭证和平台验证结果，不能保证永久有效。详见[授权失效与续期](desktop-login.md)。

## 登录密码、闲鱼Cookie和聊天数据存在哪里？

选择记住的桌面工作台账号密码存入macOS Keychain或Windows Credential Manager。闲鱼授权Cookie和业务数据保存在本机应用数据库中，并不等同于系统凭据库加密保存。

- macOS数据根：`~/Library/Application Support/com.genoooool.xianyuworkbench/`。
- Windows数据根：`%LOCALAPPDATA%\com.genoooool.xianyuworkbench\`。
- 数据库位于数据根的`data/xianyu_data.db`，配置为`global_config.yml`。

闲鱼登录、收发消息和订单同步会请求平台；启用模型服务时会向配置的模型服务发送相应内容。备份和日志可能含敏感信息，提交Issue前只保留脱敏错误、系统及版本号，不上传Cookie、密码或完整数据库。

## 如何升级和反馈问题？

macOS可通过应用内软件更新检查本仓库的签名更新；Windows从Release下载新版安装器。备份最新数据后升级。反馈时在[Issues](https://github.com/genoooool/xianyu-super-butler/issues)注明操作系统、版本、复现步骤与脱敏错误；授权问题请区分工作台登录失败、平台需重扫和网络异常。

## 使用什么许可证？

源码采用[AGPL-3.0](../LICENSE)，具体权利与义务以许可证全文为准。
