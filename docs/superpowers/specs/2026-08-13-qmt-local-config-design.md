# QMT 服务器本地配置设计

## 目标

把 QMT 脚本的版本生命周期与服务器私有配置生命周期分开。以后可以整份替换
`SINAN_QMT.py`，无需再次修改 Token、IP 白名单或服务器路径；配置变更在重启
QMT 策略后生效。

## 配置边界

QMT 脚本只保留安全、通用的代码默认值，不保存服务器私有值。沿用既有
`C:\sinan` 目录体系，全部机器配置统一保存在一个本机文件中：

- `C:\sinan\config\qmt.json`：共享目录、实盘推送参数、RPC 参数、Token 和
  IP 白名单。

`qmt.json` 的结构固定为：

```json
{
  "share_dir": "C:\\sinan\\var\\runtime",
  "rpc": {
    "enable": true,
    "host": "0.0.0.0",
    "port": 58620,
    "token": "替换为至少32位的随机Token",
    "allow_trade": true,
    "allow_ips": ["120.245.101.210"]
  },
  "live_push": {
    "enable": true,
    "period": "5nSecond"
  }
}
```

Token 只允许出现在这份服务器本地 JSON 中，不允许进入脚本、日志、异常信息或
共享目录。配置文件应仅允许运行 QMT 的 Windows 用户读取。迁移时轮换此前曾进入
粘贴内容的 Token。

## 加载模块与启动流程

脚本内部提供一个小接口 `load_local_config()`，隐藏路径推导、JSON 读取、默认值、
类型检查和安全校验。模块启动时只调用一次，配置不会在请求处理中热加载。

加载顺序：

1. 构造代码内安全默认值，其中共享目录仍缺省为
   `C:\sinan\var\runtime`；
2. 若 `C:\sinan\config\qmt.json` 不存在，则自动创建父目录和默认配置文件；默认
   保留实盘推送，但设置 `rpc.enable=false`、空 Token 和空白名单，避免首次启动时
   暴露未鉴权端口；
3. 从固定路径 `C:\sinan\config\qmt.json` 读取全部机器配置；
4. 校验最终配置后启动实盘推送和 RPC；
5. 后续修改文件，通过停止并重新启动 QMT 策略生效，无需重启整个 QMT 客户端。

QMT Python 运行环境不依赖 PyYAML，因此使用标准库 JSON。脚本仍保留
`C:\sinan\var\runtime` 作为共享目录缺省值，便于本机既有安装平滑迁移。

## 安全与错误处理

- 远程监听时，Token 必须至少 32 位且白名单不能为空；否则 RPC 不启动。
- RPC 配置错误只输出明确告警并停用 RPC，不中止策略主体、targets 批处理或实盘
  状态推送。
- JSON 无法解析、字段类型错误、端口越界或白名单格式错误时，日志指出字段和配置
  文件路径，不包含 Token 内容。
- 自动创建默认配置后，日志提示文件路径和“填写 Token、白名单并启用 RPC 后重启
  策略”，策略当次继续运行且 RPC 保持关闭。
- 启动日志输出生效配置路径、监听地址、白名单、交易权限和 `token_length`，便于
  排查实际读取的是哪份配置。
- `qmt.json` 建议仅允许运行 QMT 的 Windows 用户读取。

## 交付与迁移

仓库提供无秘密的 `qmt.local.example.json` 和一次性 PowerShell 初始化命令。首次
首次启动会自动创建 `C:\sinan\config\qmt.json`；部署文档说明如何编辑它。以后
更新只替换 QMT 脚本。现有设置页和 macOS 侧 `~/.qmt_rpc_token` 的使用方式不变，
两端 Token 内容保持一致。

## 验证

- 单元测试覆盖缺省值、完整配置、缺文件、非法 JSON、非法字段和 Token 去空白；
- 桥接测试覆盖远程配置不安全时仅停用 RPC，策略初始化仍继续；
- 验证脚本源码中不再存在实际 Token 或固定白名单；
- Windows 手工验收：创建配置后启动策略、通过设置页验证 RPC、替换脚本后再次
  启动并验证无需修改任何私有参数。
