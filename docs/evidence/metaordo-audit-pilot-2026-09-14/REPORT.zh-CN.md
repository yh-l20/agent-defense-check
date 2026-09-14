# 元序星核合作试点：HostModel 合成契约验证

本报告记录 Issue #6 的本地合成接口验收。测试源码固定为 `5ee51c8b2030489475e8794bde8497eeb3908db2`；产品组件与主干基线 `876043ae1bac95d83ec122a97f39a45fabfb4a45` 相同，本次没有改动产品代码。

## 独立复现环境

- Ubuntu 24.04.3 LTS / x86_64，WSL2 内核 `5.15.167.4-microsoft-standard-WSL2`，Python 3.12.3。
- Linux 独立运行：5 项通过，0 失败、0 跳过。Windows 运行：5 项全部因 Linux Unix socket 要求跳过，不作为 Windows 协议验收。
- 真实组件：`yuanxingmu.broker.Broker`、`yuanxingmu.gateway_network.HostModel`、`yuanxingmu.sdk_model_store.ModelStore`、`yuanxingmu.sdk_runtime._SdkOutputGuard`。
- 替代组件：测试内 Unix HTTP 客户端模拟 Worker；上游为进程内合成 HTTP 网关，监听 `127.0.0.1` 的随机端口。没有使用真实 Agent 沙箱、真实元序星核网关、生产密钥或真实模型。
- 测试只允许连接自身临时目录的 Unix socket 和该环回网关；这是测试进程内的连接限制，不是对操作系统网络命名空间隔离的验收。

在独立检出目录切换到上述测试提交后运行：

```sh
git rev-parse HEAD
env -i PATH=/usr/bin:/bin HOME=/tmp LANG=C.UTF-8 \
  python3 -m unittest discover -s tests -p 'test_yuanxingmu_metaordo_pilot.py' -v
```

## 实际断言

| 用例 | 核心断言 | 独立结果 |
|---|---|---|
| 请求头、认证及响应边界 | 上游收到宿主构造的 `corr-<32位小写hex>`、版本 `1.0` 和合成宿主认证值；Worker 伪造值不能覆盖它，自定义伪造头被剥离；响应回显头不传给客户端；connect 尝试、服务端 accept、HTTP 请求分别为 1，journal 记录完成。 | PASS |
| 非法元数据 | 无效关联格式返回 HTTP 500；provider 恰调用 1 次，未创建 ticket；上游连接尝试、accept、请求和记录队列均为空，ModelStore journal 字节不变。 | PASS |
| 请求前撤权 | 使用独立任务，在请求前撤权；返回宿主拒绝正文，provider 和 `begin` 均不调用，上游各项计数为 0，journal 字节不变。 | PASS |
| 回调期间撤权 | 使用另一个新建任务，由 provider 回调内触发撤权；provider 必须恰调用 1 次，`begin` 不调用，上游各项计数为 0，journal 字节不变。 | PASS |
| 显式 Replay | 从真实首次成功请求得到响应，再请求 `/v1/chat/completions/replay`；状态及正文字节与首次响应完全相同，provider 和 `begin` 不调用，连接尝试、accept、HTTP 请求为 0，journal 字节不变。 | PASS |

两个撤权时机由独立的测试初始化隔开，避免复用已经撤权的任务而根本未进入 provider。计数分别测量客户端连接尝试、网关真实 accept 和 HTTP handler 接收；零新 ID 的结论来自 provider 不被调用的断言，不是根据输出中未出现 ID 推断。

## 结论范围

这些结果只覆盖固定源码与本地模拟网关之间的请求头、拒绝及缓存协议行为，不认证真实网关互操作性、全系统执行隔离、日志不可篡改、生产可靠性或新增发行版支持。测试与报告不新增响应头透传功能，也不改变已发布 a2 安装包。
