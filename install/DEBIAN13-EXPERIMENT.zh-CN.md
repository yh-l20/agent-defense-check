# Debian 13 实验安装验收候选

这是供 Issue #14 实机验证的显式实验入口，**尚未完成 Debian 13 双框架安装验收，不是新增的正式支持声明**。现有 installer `0.4.0a2` 下载文件没有此开关，也没有被替换。候选默认仍只接受 Ubuntu 24.04；Debian 13 必须传入 `--experimental-debian13`。

## 准入与边界

- 精确限定 `ID=debian`、`VERSION_ID=13`、x86_64、普通非 root 用户，当前解释器必须与 `/usr/bin/python3` 指向同一系统 Python 3.13。没有改为自动信任任意 `sys.executable`。
- 系统依赖须预先准备，包括可用的 Python 3.13/venv、CA 证书与 `/usr/bin/bwrap`。如固定依赖在该机器上还需要构建工具，应在原始失败记录中说明；不能推导只装 bwrap 就一定完成双框架安装。
- 实验开关不能与 `--system-deps` 同用；该组合在创建安装目录、读取下载清单或下载之前失败。本安装器不进入 apt/sudo 或 AppArmor 自动配置分支，也不放宽全局 userns 限制。框架依赖构建脚本仍在安装用户权限下运行，这不是安装脚本沙箱。
- 安装中仍运行真实的隔离检查，失败即停止。Node、OpenClaw、Hermes、uv 的版本与校验逻辑保持原有固定输入，不因平台实验放宽。
- 安装记录包含 `experimental_platform: debian13`，后续重试或完整安装复验也必须显式选择同一实验范围。安装根目录仍必须是 Linux 家目录下的新子目录，已有未知目录不能被接管。

本机单元测试中的发行版信息是合成输入，只验证准入和拒绝路径，不能证明 Debian 的 Node/glibc、AppArmor 或原生框架已经兼容。

## 实机执行顺序

在普通用户的独立检出中使用维护者给出的**完整候选提交 SHA**。先记录 `git rev-parse HEAD`、`/etc/os-release`、`uname -srvm`、系统 Python 路径/版本、glibc 与 bwrap 版本，并说明裸机、虚拟机、容器或 WSL；不能只用发行版名称推断内核与安全策略。

以下示例使用尚不存在的新路径。构建器拒绝覆盖已有 zipapp；安装日志和报告需要保留。

```bash
set -euo pipefail
YXMCANDIDATE_EVIDENCE="$HOME/yuanxingmu-debian13-evidence"
YXMCANDIDATE_ROOT="$HOME/yuanxingmu-debian13-trial"
mkdir -m 700 "$YXMCANDIDATE_EVIDENCE"
git rev-parse HEAD > "$YXMCANDIDATE_EVIDENCE/source-sha.txt"
/usr/bin/python3 install/build_zipapp.py \
  --output "$YXMCANDIDATE_EVIDENCE/yuanxingmu-debian13-candidate.pyz" \
  > "$YXMCANDIDATE_EVIDENCE/build-manifest.json"
sha256sum "$YXMCANDIDATE_EVIDENCE/yuanxingmu-debian13-candidate.pyz" \
  > "$YXMCANDIDATE_EVIDENCE/installer-sha256.txt"

/usr/bin/python3 -I "$YXMCANDIDATE_EVIDENCE/yuanxingmu-debian13-candidate.pyz" \
  --experimental-debian13 --install-root "$YXMCANDIDATE_ROOT" --no-shortcut \
  2>&1 | tee "$YXMCANDIDATE_EVIDENCE/install-console.txt"

/usr/bin/python3 -I install/smoke_install.py \
  --install-root "$YXMCANDIDATE_ROOT" --framework both \
  --report "$YXMCANDIDATE_EVIDENCE/smoke-both.json" \
  2>&1 | tee "$YXMCANDIDATE_EVIDENCE/smoke-console.txt"

/usr/bin/python3 -I install/check_reuse.py \
  --experimental-debian13 --install-root "$YXMCANDIDATE_ROOT" \
  --installer "$YXMCANDIDATE_EVIDENCE/yuanxingmu-debian13-candidate.pyz" \
  > "$YXMCANDIDATE_EVIDENCE/reuse.json"
```

构建器的 `source_commit` 绑定已提交源文件；`publishable` 字段表示构建输入来自提交，不表示候选已经正式发行或通过 Debian 验收。本轮使用固定公开 runtime `0.7.0a2` 制品，不传开发 wheel，也不下载独立 Python。

首次安装需要下载固定组件及框架依赖，**不是全流程离线安装**。`smoke_install.py` 使用本地合成模型入口，通过标准要求模型请求为 0，不发送邮件。它只接受尚未创建 workbench 的新安装；失败后先保留原目录及记录，不要删除或伪造失败项来生成通过报告。

## 回传与结论

请在独立 PR 或 Issue #14 关联上述候选 SHA、环境说明、构建清单/哈希、smoke JSON、reuse JSON 和成功/失败摘要。安装目录中的管理链接、凭据和工作资料保持私有，不应整份上传；必要错误日志先检查并去除敏感值。

验收检查实际原生页面、启动、重复启动拒绝、重开、撤权与停止，最后关闭此次创建的服务并保留安装目录。它不证明模型推理、防注入效果、任意任务工具或 Windows 启动器已经验证。只有收到并审查这些实机证据后，才决定候选是否可以合并及后续新增支持范围。
