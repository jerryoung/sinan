# QMT 部署指南(Windows ECS)

大 QMT 跑在 Windows ECS 上,司南在本地 Mac。两条通道各司其职:

| 通道 | 文件 | 用途 | 节奏 |
|---|---|---|---|
| targets 文件薄壳 | qmt_shell/shell_strategy.py | 每日自动调仓(实盘执行主通道) | 14:45 执行 / 15:05 回写 |
| API socket 转发 | qmt_shell/rpc_server.py + qmt_sdk.py | 随时查询行情/账户、手动下单、调试 | 常驻 |

模拟盘/实盘:在 QMT 客户端把模型绑到模拟或实盘账号时决定,两个脚本都
只如实上报(fills 的 `trade_mode` 字段),司南读取展示,不做二次猜测。

## 一、QMT 侧部署(ECS)

1. 大 QMT → 新建模型 → 粘贴 `shell_strategy.py`,改头部常量
   (STRATEGY_NAME / ACCOUNT / 目录;目录用与本地同步的盘,如坚果云/Syncthing);
   每个策略一个模型实例,绑各自账号。
2. 再建一个模型粘贴 `rpc_server.py`(常驻转发服务),按下节配置 HOST/TOKEN。
3. 运行模式选 模拟交易 或 实盘,与账号绑定一致。

## 二、远程访问安全模型(必读)

协议是明文 TCP:**传输安全交给隧道,应用层 token 是第二道锁,不是唯一的锁。**

### 方案 A:SSH 隧道(推荐,零暴露)

ECS 上启用 OpenSSH Server(Windows 设置 → 可选功能 → OpenSSH 服务器,
或 `Add-WindowsCapability -Online -Name OpenSSH.Server`),安全组只开 22 端口
(并建议限源 IP)。`rpc_server.py` 保持 `HOST = "127.0.0.1"` 不变。本地:

```bash
ssh -N -L 58620:127.0.0.1:58620 user@<ECS公网IP>
```

之后本地一切照常(`qmt.connect("127.0.0.1", 58620, token=...)`)——
流量全程加密,QMT 端口对公网零暴露。配合 `autossh` 可保持长连。

### 方案 B:Tailscale(推荐,免隧道运维)

两端安装 Tailscale 登录同一账号;`rpc_server.py` 的 `HOST` 改绑 ECS 的
`100.x.y.z` 虚拟网卡 IP,并配置强 `TOKEN`(≥32 位随机);本地
`settings.yaml` 的 `qmt_rpc.host` 填该 IP。公网不可见,私网可达。

### 方案 C:直接暴露公网端口(不推荐)

仅当:ECS 安全组把 58620 收紧到你的固定出口 IP + `TOKEN` ≥32 位随机 +
`ALLOW_TRADE = False`(只读,禁下单)三者同时满足才可接受。
明文协议意味着链路上的观察者可见请求内容——交易通道请回到方案 A/B。

### 服务端强制与开关

- 非 `127.0.0.1` 绑定必须**同时**配置非空 `TOKEN` + 非空 `ALLOW_IPS`
  白名单,缺一拒绝启动——白名单是网络层物理隔离,token 是应用层口令,
  两层独立起效;
- `ALLOW_IPS` 支持单 IP 与 CIDR 网段:Tailscale 场景填本机的 `100.x` IP
  (最严)或 `["100.64.0.0/10"]`(全 Tailscale 网段);直暴场景填家庭/办公
  固定出口 IP。**ECS 安全组同样要收紧到相同来源——两层白名单互为备份**
  (安全组防的是端口扫描,应用层防的是安全组误配);
- token 恒时比较(防时序侧信道);非白名单连接在握手层直接断开并记日志;
- `ALLOW_TRADE = False` 把通道降为只读:行情/账户查询照常,
  `passorder`/`cancel` 一律拒绝——远端调试建议默认只读,要下单时再开。

## 三、本地侧配置(司南)

1. `设置` 页(或 settings.yaml)`qmt_rpc` 段配 host/port
   (SSH 隧道场景就是 127.0.0.1:58620);
2. token 写入本机 `~/.qmt_rpc_token`(单行):

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))" > ~/.qmt_rpc_token && chmod 600 ~/.qmt_rpc_token
```

   ECS 侧 `rpc_server.py` 的 `TOKEN` 填同一串。**token 严禁写进仓库/配置/日志**
   (与 `~/.tushare_token` 同一纪律)。
3. 使用:

```python
from qmt_shell import qmt_sdk as qmt
qmt.connect_from_settings()          # 读 settings.qmt_rpc + ~/.qmt_rpc_token
accs = qmt.get_trade_detail_data("8888888888", "STOCK", "account")
```

## 四、安全清单(上实盘前逐项过)

- [ ] rpc_server 未直接暴露公网(SSH 隧道或 Tailscale)
- [ ] TOKEN ≥32 位随机,两端一致,`~/.qmt_rpc_token` 权限 600
- [ ] 远端调试期 `ALLOW_TRADE = False`,确认要下单才打开
- [ ] ECS 安全组最小开放(方案 A 仅 22;方案 B 零公网端口)
- [ ] fills/targets 同步盘目录不含任何凭证
- [ ] 影子模式已并行跑 2~4 周,`trade_mode` 显示与预期一致
