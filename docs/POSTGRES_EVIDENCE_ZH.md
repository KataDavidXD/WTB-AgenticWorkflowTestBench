# PostgreSQL 执行证据与发布消费支持

## 范围与接入

PostgreSQL 支持 attempt admission / immutable events /
reconciliation、两个 runtime 的共同 collector、policy / material CAS、批准消费和
外部投递账本。仅安装 LangGraph 的 PostgreSQL checkpointer 不会给这些新表提供支持。

```sh
python -m pip install -e ".[postgres-evidence]"
```

```python
import os
from wtb.application.services.evidence_collector import EvidenceCollector
from wtb.infrastructure.evidence.postgres_store import (
    PostgresEvidenceStore, PostgresPublicationSink,
)
from wtb.infrastructure.evidence.external_delivery import ExternalDelivery

with PostgresEvidenceStore(
    os.environ["WTB_EVIDENCE_POSTGRES_DSN"],
    schema="wtb_evidence",       # 一个逻辑证据域的专用 schema
    lock_timeout_ms=30_000,
    connect_timeout=10,           # 秒；连接和首次池就绪的等待界限
    pool_min_size=1,
    pool_max_size=4,
    pool_timeout=30.0,            # 秒；每次借用连接的最长等待
) as store:
    collector = EvidenceCollector(store)
    publication = PostgresPublicationSink(store)
    delivery = ExternalDelivery(publication)
    # 在此完成业务，并等待使用这些服务的 worker 退出。
```

数据库须预先存在；连接角色须有创建该专用 schema / 表和读写权限。也可由同一受信
任角色预建 schema。业务对外权限仍由调用方限制，连接角色不是授权 actor。所有共同
消费批准的进程必须配置相同 database + schema。schema 名限小写字母开头、数字和下划线、
最长 63 字符，拒绝 public / pg_* / information_schema。不要把 DSN 写进日志。

这是显式实例化的可选服务；没有改变 Bench 默认后端、既有 SQLite 构造函数、runtime
协议、operator 判定语义，也不会自动迁移既有 SQLite 数据。额外依赖为 psycopg 3 与 psycopg-pool；
无需为证据存储安装 LangGraph。旧 `langgraph-postgres` extra 的职责保持不变。

## 实现与一致性边界

`SQLEvidenceStore` 与 `SQLPublicationSink` 共享全部状态机、collector 投影、snapshot
校验、批准消费及恢复逻辑。SQLite 保留 `BEGIN IMMEDIATE`；PostgreSQL 只实现连接、
参数绑定、BYTEA 和事务边界。二进制产物无文本转码，generation / material version
使用 BIGINT。既有 SQLite 数据无需 schema 迁移。

PostgreSQL 每个存储实例管理独立的有界连接池；每次操作借用连接并开启独立事务，
在事务内取得专用 schema 的稳定 advisory lock，
再读取／更新相关状态。锁覆盖 evidence、policy、material、approval、effect 和 outbox，
也覆盖尚不存在的行以及并发首次建表。使用 READ COMMITTED，等待锁之后的读取看到
上一持锁者刚提交的撤权、head 更新和消费结果。异常及进程断连回滚未提交事务。
锁等待超时向调用者报告数据库异常，不返回批准、也不自动重发。
如果提交确认因断连而丢失，应查询或重放同一个 approval；不要通过另建批准来猜测
原消费是否发生。同一个 approval 的已提交本地 effect 会以幂等结果返回。

同一 schema 内读写仍然串行，没有按 execution 分片锁。连接复用不会取消事务、锁或
消费时的新鲜状态检查。它支持多个客户端共享同一个 PostgreSQL 一致性边界，但不
提供高吞吐、数据库故障切换、跨地域或任意外部 API exactly-once 的保证。直接 SQL
写者如果绕过该锁，不在保证范围内；运行时也不应随意删除或修改这些内部表。

构造函数等待连接池就绪，在一个持锁事务内创建 schema、检查版本并创建 evidence 表；失败时回滚、关闭池并抛错。请求路径没有建表或版本查询。publication / outbox 的表仍在对应服务构造阶段初始化。在线外部 schema 迁移不受支持；须停写、关闭实例、迁移后重新初始化，不能修改运行中实例的 schema。

使用 `with` 或在 `finally` 中调用 `store.close()`。close 幂等且不可重新打开，关闭空闲连接、拒绝等待者和新借用；在途事务完成后归还的连接也关闭。需要完成式停机时，调用方先等待业务 worker 结束，再 close。实例与池仅在创建它们的进程内使用；其他进程创建自己的实例。异常（包括 BaseException）回滚，归还时无残留事务；坏连接丢弃后替换，业务操作本身不自动重试。

连接池使用 psycopg 官方[ConnectionPool 的生命周期与归还规则](https://www.psycopg.org/psycopg3/docs/api/pool.html)。

事务 sink 的真实 effect 是 `publication_effects` 中的 bytes。Git push / HTTP 调用
仍在外部 sink：先持久化 claim，未知结果只 lookup；明确 absence 后才可显式重试，
复用原幂等键并核对新鲜授权。外部 provider 自己保证最终 guard、幂等和 effect 的
原子边界。PostgreSQL 不会替 Cordis 扩大这个保证。

参考 PostgreSQL 官方[事务锁说明](https://www.postgresql.org/docs/16/explicit-locking.html)
和 [READ COMMITTED 可见性](https://www.postgresql.org/docs/16/transaction-iso.html)。

## 合同测试与复现

SQLite 和 PostgreSQL 共享状态机实现；PostgreSQL fixture 重跑相同合同断言，并补充
真实连接、事务、连接池和多进程行为检查。

| 风险 | 检查内容 |
|---|---|
| SQLite 与 PostgreSQL 状态机漂移 | 共享源码、同一套精确 attempt / 重复 / 乱序 / 冲突 / reconciliation 测试 |
| 等待事务锁后仍使用旧授权 | 查询 `pg_locks` 证明 publisher 正在等待；撤权提交后必须拒绝 |
| 首次建表竞争与缺失行 CAS | 多连接并发 bootstrap；并发 head / generation CAS |
| 同一批准被多进程消费 | 两个独立 Python 进程提交同一批准，仅一个 new_effect |
| 进程丢失留下半次消费 | 真实子进程在 commit 前／后 `os._exit`；回滚或幂等重放 |
| 超时造成绕过验证 | 锁超时失败；解除后才允许一次合法 effect |
| BYTEA / JSON 参数混淆 | 二进制 effect 断言、Unicode、引号、问号、百分号绑定 roundtrip |
| generation 32 位溢出 | 跨 `2**31` 的 CAS |
| outbox 恢复丢失 sink 身份 | 重开账本、错误 provider 拒绝；原 provider lookup 恢复 |
| 外部 effect 已提交、WTB 未收回执 | PostgreSQL outbox + 独立 SQLite provider；进程退出后 lookup，effect 数仍为 1 |
| 不同后端导致 runtime 转换逻辑复制 | 原 DSH SDK 与 LangGraph 测试只替换 store fixture，实际运行并重开证据库 |
| 可选依赖侵入默认包 | 已安装 wheel 在无 psycopg / LangGraph 时可导入 PG 模块并初始化 SQLite；安装 psycopg 后初始化 PG 三组表 |

设置专用测试数据库，然后从仓库根目录运行。以下为 PowerShell 示例；DSN 使用
自己的专用测试凭据，不要提交到仓库或复制进日志：

```powershell
python -m pip install -e ".[postgres-evidence,test]"
$env:WTB_EVIDENCE_POSTGRES_DSN = 'postgresql://USER:PASSWORD@HOST:5432/TEST_DATABASE'
$env:WTB_REQUIRE_EVIDENCE_POSTGRES = '1'
python -m pytest -q tests/unit/evidence tests/integration/evidence_postgres
python scripts/validate_postgres_evidence.py --output pg-results.json --n 100
```

DSH 真实测试需要 [DeepSeek Harness 集成](../DEEPSEEK_HARNESS.md) 所列的匹配 source
checkout 与已构建 SDK；没有这一前提不能把 skip 当通过。完整 S0/M0 引用接口也要求
匹配的 source SDK，安装 `deepseek-harness-source` extra 本身不会提供它。测试和比较
脚本只清理本次随机生成的 schema。CI 服务矩阵使用 PostgreSQL 16 / 17，环境缺失时
报错；DSH source gate 需要另外配置其前提。

执行证据与外部恢复语义见[架构说明](RUNTIME_EVIDENCE_ARCHITECTURE_ZH.md)，material
generation 的批准绑定与部署要求见[升级说明](e1-material-version-upgrade.md)。
