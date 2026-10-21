# 本地补丁（LOCAL PATCHES）

这个克隆与上游 `Zhengyuuuui/minimax2api` 有差异。差异**只**在本地分支
`local/robust-token-recovery` 上，`main` 仍与上游同一个提交（`0411037`）。

- 远端 `origin` 的 **push 通道已被锁死**（`git remote set-url --push origin DISABLED-local-only`），
  误 push 推不到别人家仓库。
- 本分支**不推**任何远端。要合回上游请自行准备 PR。

## 为什么打这个补丁（2026-10-21 真实事故）

一次真实 DSH 会话（`session-a26a2505-…`）三个回合全失败：先
`minimax llm HTTP 401`，之后 `accounts unusable: minimax-cn, minimax-global`，
号池空了一整个小时。根因不是 MiniMax 侧，而是三个叠加的缺陷：

### 1. 日志把控制流打断（`app/keepalive.py`）

`renew_account` **先写账号行、后报告结果**，而报告是 `print`。stdout 一旦没了
（父进程退出、日志轮转、监督者关掉管道），`print` 抛 `BrokenPipeError`，
于是一次**成功**的续期在调用方看来是**失败**的——请求路径据此把一个凭据完好的
账号"退休"（`status=invalid`）。更糟的是 sweep 的错误处理也是 `print`，
于是 sweep 跟着一起死，之后再没有任何续期。

本机这次事故的触发点正是：后端由一个后台 job 启动，job 的输出管道在中途断开，
日志从 10:02 之后一行都没有——而账号仍在一个小时后才恢复。

**补丁**：`print` 换成 `_log()`（吞掉所有写失败），日志不再能打断控制流。

### 2. 续期抛异常 ≠ 续期被拒（`app/pool.py`）

`pool._record_failure` 把 renewer 抛出的任何异常都当成"续期失败"，直接走退休分支。
但异常只说明**没判定**，不说明凭据死了：代理抽风、日志报错，都不该让号池清空。

**补丁**：renewer 抛异常时走"冷却"分支（同已有的 transient 处理），不退休。

### 3. 被退休的账号没有回来的路（`app/keepalive.py`）

sweep 只在"token 快到期"时才动手，而被误退休的账号 token 还没到期 →
判定 `due=0` → 什么都不做 → 号池一直空到 token 自然到期。

**补丁**：sweep 把"可续期且状态是 invalid"的账号也纳入本轮；续期成功即复位
（`renew_account` 本来就会复位 `status`），续期真失败则保持退休。

## 证据

```powershell
.venv\Scripts\python.exe -m pytest tests/ -q     # 101 passed（上游 97 + 本地 4）
```

`tests/test_local_patches.py` 钉住三个行为：

- stdout 坏掉时，续期**仍然成功**（`renewal_survives_a_dead_stdout`）；
- renewer 抛异常时，账号**不被退休**（`pool_cools_instead_of_retiring_when_the_renewal_raises`）；
- sweep 能**救回**被退休的账号（`sweep_revives_a_retired_account`）。

## 怎么回到上游

```powershell
git checkout main          # 与上游一致，无补丁
# 或者
git checkout -b my-work main && git cherry-pick <patch-commit>
```
