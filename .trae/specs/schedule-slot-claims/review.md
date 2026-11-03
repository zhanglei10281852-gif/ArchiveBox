# CrawlSchedule 时间槽执行权（持久领取）— 独立代码审查报告

- 审查日期：2026-11-03
- 审查基线：分支 `dev`，工作树改动（HEAD=`4a2c35fc`，ArchiveBox 0.9.74rc234）
- 审查方式：fresh context 只读审查 + WSL（`cycle-foundation-docker`，musl/BusyBox）内真实 pytest；未修改任何被审查代码
- 审查范围：
  - [schedule_util.py](file:///f:/swe/098842/project-01/archivebox/crawls/schedule_util.py)
  - [models.py](file:///f:/swe/098842/project-01/archivebox/crawls/models.py)
  - [0020_schedule_run_claims.py](file:///f:/swe/098842/project-01/archivebox/crawls/migrations/0020_schedule_run_claims.py)
  - [archivebox_schedule.py](file:///f:/swe/098842/project-01/archivebox/cli/archivebox_schedule.py)
  - [runner.py](file:///f:/swe/098842/project-01/archivebox/services/runner.py)（2318-2327）
  - [test_schedule_claims.py](file:///f:/swe/098842/project-01/archivebox/tests/test_schedule_claims.py)
  - [test_cli_update_reindex_snapshots.py](file:///f:/swe/098842/project-01/archivebox/tests/test_cli_update_reindex_snapshots.py)

---

## 一、Review Checkpoints

### CP-1 并发互斥（唯一约束 + CAS，SQLite / PostgreSQL READ COMMITTED） — **PASS**

证据：

1. 唯一物化锚点：`CrawlScheduleRun.Meta.constraints` 的 `(schedule, slot_at)` 唯一约束（[models.py:1662-1667](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1662-L1667)）+ `Crawl.schedule_run` OneToOne（[models.py:441-447](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L441-L447)）。
2. 首次领取走 INSERT 抢唯一键，`IntegrityError` 后降级为对既有行的条件 UPDATE（[models.py:1703-1742](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1703-L1742)）：
   - SQLite：写者全局串行，后到者 INSERT 立即拿唯一冲突或等待写锁；随后在 autocommit 新语句读到已提交行；租约未过期返回 None。
   - PostgreSQL READ COMMITTED：并发 INSERT 同键时后到者在唯一索引上等待，先到者提交后才收到 duplicate key（回滚则自己成功）；接管 UPDATE 同行互斥，获胜方提交后落败方按**行新版本**重新校核 `status__in/slot + claim_until__lte=now`，新租约必然 > now → 0 行（[models.py:1726-1739](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1726-L1739)）。
3. 确认再设一道 token CAS：只有领取/接管成功的持有者知道 `claim_token`（[models.py:250-260](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L250-L260)、[290-300](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L290-L300)），陈旧持有者即使漏过租约判断也无法确认。
4. 未使用 `select_for_update`；无两个 Crawl / 两次 succeeded 的漏网路径（含陈旧所有者苏醒场景：其 token 已被接管者替换，确认 0 行；其未提交 Crawl 随守卫异常回滚，详见 CP-2）。
5. 实测：真实双子进程 `schedule --run-all` 并发后全局恰 1 行 run（succeeded）、恰 1 个带槽 Crawl，第三次 run-all 输出 `Enqueued 0`（test_schedule_claims.py:388-430，通过）。

### CP-2 事务边界与维护租约心跳 — **PASS（附 1 条 minor）**

证据：

1. 普通归档：认领（[models.py:1704-1715](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1704-L1715)）与“孤儿 Crawl 检查 → 物化 → 守卫确认”全部在一条 `transaction.atomic()` 短事务内（[models.py:244-269](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L244-L269)）；事务内只有 ORM 短写，无网络/文件/hook。守卫不匹配抛 `_SlotCommitGuardMiss` 回滚，Crawl 不可能半留；OneToOne 冲突兜底 `IntegrityError → skipped`。
2. 维护：`run_scheduled_maintenance()`（[archivebox_update.py:131-146](file:///f:/swe/098842/project-01/archivebox/cli/archivebox_update.py#L131-L146)）在事务**外**同步执行（[models.py:278-288](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L278-L288)）；领取、15s 心跳续租、最终确认都是单条短 UPDATE（[models.py:388-393](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L388-L393)），常量 60s 租约 / 15s 心跳与既有 `ACTIVE_STATE_LEASE_SECONDS=60` 口径一致。
3. 心跳线程使用 Django 线程本地连接（`ConnectionHandler` 基于 `threading.local`），`connections.close_all()` 只关闭本线程连接（[models.py:397-400](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L397-L400)），不影响主线程；`stop()` join 有 5s 上限（[models.py:402-405](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L402-L405)）。
4. 维护异常 → failed + `claim_until=now` + 错误信息（截断 1000 字）后重抛（[models.py:281-288](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L281-L288)）；守卫失配 → failed 且不冒充成功（[models.py:304-312](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L304-L312)）；计划删除时 UPDATE 匹配 0 行，天然落空。
5. 残余风险见 Finding M1（心跳线程对“非守卫类”DB 异常无防护），不影响“绝不双重确认”，FR-9 字面要求成立。

### CP-3 接管幂等（过期 claimed / failed / 孤儿 Crawl / attempts / 重启锚点） — **PASS**

证据：

1. 接管复用同一主键行，仅 `attempts=F("attempts")+1`、换新 token/租约/owner、清 error/finished_at（[models.py:1726-1742](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1726-L1742)）；succeeded 行永不被接管（[models.py:1722-1723](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1722-L1723)）。
2. 无 Crawl：接管后补物化，同一行 attempts=2、succeeded、恰 1 个 Crawl（test_schedule_claims.py:209-224，通过）。
3. 有孤儿 Crawl：事务内先 `Crawl.objects.filter(schedule_run=run).first()` 认领导（[models.py:246-248](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L246-L248)），不新增（test:227-252，通过）。新代码路径本身把 Crawl 插入与确认放在同一事务，孤儿只可能来自升级窗口/手工构造，属防御性必要分支。
4. failed 行 `claim_until<=now` 立即可被同一槽重新领取（test:343-379：真实 `ALTER TABLE RENAME` 制造异常 → failed/error/attempts=1 → 重领 attempts=2 → succeeded → 第三次 skipped，通过）。
5. 重启幂等：`last_run_at = 最近 succeeded 槽 or 旧启发式锚点`（[models.py:127-159](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L127-L159)）；succeeded 后 `is_due()` False，`next_run_at` 严格为成功槽之后的下一次 cron fire（test:142-146、158-160，通过）。
6. claimed/failed 但未成功的槽不推进锚点，保证过期后仍可被接管（[models.py:170-181](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L170-L181)）。

### CP-4 配置变更守卫（禁用 / 删除 / 改期 cron/config/template） — **PASS（附 1 条 minor 可观测性建议）**

证据：

1. 领取前读 `is_enabled`（[models.py:1695-1697](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1695-L1697)），独立于 revision 生效（测试用不带 modified_at 的 `.update(is_enabled=False)` 验证，test:193-198 通过）。
2. 修订戳 `modified_at` 在领取时读取、在最终确认时以 JOIN 条件复核（crawl 与 maintenance 两条路径一致）；任何 `save()`（含 cron/config/template/notes 编辑）经 `auto_now` 推进修订，CLI `--clear` 显式带 modified_at（[archivebox_schedule.py:50-53](file:///f:/swe/098842/project-01/archivebox/cli/archivebox_schedule.py#L50-L53)）。
3. 禁用：crawl 侧确认 0 行 → 回滚不留 Crawl（test:260-281 通过）；maintenance 侧最终 CAS 0 行 → failed，不确认 succeeded。
4. 删除：`CrawlScheduleRun.schedule` CASCADE；`Crawl.schedule`/`Crawl.schedule_run` SET NULL（迁移 [0020:35,46](file:///f:/swe/098842/project-01/archivebox/crawls/migrations/0020_schedule_run_claims.py#L35-L46)）；删除发生在领取后/物化前时 Crawl INSERT 触发 FK IntegrityError → 被捕获 skipped（[models.py:265-267](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L265-L267)）；历史派生 Crawl 保留（test:320-334 通过）。
5. 改期：旧 token+旧 revision 提交失败；新 cron 下 `current_slot/next_run_at` 立即按新表达式计算并只派发一次（test:284-317 通过；边界纯函数测试 test:33-55 覆盖整点归属）。
6. 残余可观测性问题见 Finding M2（crawl 侧守卫失配后 run 行短期保持 claimed），不产生错误提交。

### CP-5 展示 / 历史兼容 — **PASS**

证据：

1. 无 run 行的历史库：`_legacy_last_run_at()` 与旧实现逐行一致（update 类取 `modified_at`，crawl 类取最新派生 Crawl `created_at`，再回退 template/created_at；对比 HEAD 版 models.py）；`is_due` 经 `current_slot > anchor` 与旧 `next_run_at <= now` 在全部整点边界等价（croniter `get_next` 严格递增）。
2. `enqueue()` 冻结配置逻辑（剔除 SCHEDULE_KIND、复制 urls/config/depth/tags/persona/label/notes/created_by）原样保留（[models.py:337-356](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L337-L356)），新物化路径仅多挂 `schedule_run`；`test_frozen_crawl_config.py` 9/9 通过。
3. 历史 Crawl（`schedule_run=NULL`）允许存在；OneToOne 仅约束槽派生 Crawl。
4. CLI `--show`/默认输出字段未变，`_active_schedules()` 经 `with_run_state()` 单条聚合注释（LEFT JOIN + GROUP BY）避免逐行查询（[archivebox_schedule.py:44-47](file:///f:/swe/098842/project-01/archivebox/cli/archivebox_schedule.py#L44-L47)）；admin 展示未引用被删字段，29/29 通过。
5. 迁移为纯新增（新表 + 可空 OneToOne + 索引/约束），`makemigrations --check --dry-run` → `No changes detected`；每次测试初始化真实建库均成功应用 0020。
6. `make_latest_schedule_due`（回拨 template.created_at）在无成功槽时仍走旧锚点生效；新建计划未到期不被 force=False 派发（test:118-127 通过）。
7. 旧 `dispatch()` 收敛为 `dispatch_due(force=True)` 的兼容包装（[models.py:229-236](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L229-L236)），生产代码中无其它直调 enqueue/维护的派发分叉（grep 证据：仅 CLI、runner 两个生产入口）。

### CP-6 跨后端与并发契约（AGENTS.md / AC-10 rubric） — **PASS（评 5/5）**

证据：

1. 全部协调原语为 ORM 原生唯一约束 + autocommit/CAS 短 UPDATE（[settings.py:236-242](file:///f:/swe/098842/project-01/archivebox/core/settings.py#L236-L242) 明确 SQLite 使用 deferred 短事务 + busy_timeout），无文件锁、无定时器选举、无领取路径上的 sleep/重试循环；`OperationalError` 仅导致本轮跳过，下轮从持久状态重判（[models.py:218-221](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L218-L221)），不是 sleep 重试。
2. 模式复用：领取/接管形态对齐 `ModelWithQueue.claim_for_worker()` 与 `importers.service.enqueue_due()` 的“守卫 CAS + 一条短事务”，心跳对齐 runner STARTED 租约心跳（同步维护本体用守护线程是合理适配，非常设包装层）。
3. 改动聚焦：新增面仅一个 NamedTuple、一个私有异常、一个心跳类（FR-9 必需），无 shim/alias/多余抽象；ruff（仓库等价 E/F、line=140）对改动文件无新增告警（仅 archivebox_schedule.py 2 条 E501，HEAD 基线同样存在：HEAD 版 138/143 行）。
4. 测试遵守真实进程/真实 DB/真实文件/真实异常（ALTER TABLE）约束，无 mock/monkeypatch/假钩子。
5. SQLite 实测全绿；PostgreSQL READ COMMITTED 下互斥语义按 CP-1 分析成立（无后端方言 SQL、无依赖 SQLite 锁行为的写法）。

---

## 二、AC-1 ~ AC-10 覆盖表

| AC | 结论 | 证据 |
|---|---|---|
| AC-1 并发同槽只物化一个 Crawl | PASS | `test_two_concurrent_run_all_clis_materialize_one_crawl`（两个真实 OS 子进程）：全局 run 恰 1 行且 succeeded、带槽 Crawl 恰 1 个；接续 run-all 输出 `Enqueued 0`。首次运行因 DrvFs 下 `archivebox init` 与并行 WSL 命令争用 I/O 触发 fixture 60s 超时（环境抖动，非断言失败），单独重跑 55.67s 通过 |
| AC-2 领取持久、字段齐全、互斥可验证 | PASS | `test_active_lease_blocks_second_claim`（第二次 claim 返回 None、行=1、token/owner/lease/attempts 齐全）、`test_disabled_and_deleted_schedule_cannot_be_claimed`；模型字段 models.py:1632-1654 |
| AC-3 维护每槽只成功一次 | PASS | `test_two_concurrent_run_all_clis_run_maintenance_once`（双进程：1 行 succeeded、attempts=1）；reindex 测试新增断言：同槽第二次 `dispatch()` 不新增 run（count==1）、`stale_snapshot.retry_at` 两次一致（副作用不重复），该用例仅在下游 search 钩子 ArchiveResult 断言失败（环境问题，见第四节） |
| AC-4 三入口共用同一领取规则 | PASS | 生产 grep 仅 CLI（archivebox_schedule.py:131）与 runner（runner.py:2327）调用 `dispatch_due`；API `/cli/schedule` 复用同一 CLI 函数（v1_cli.py:106-120,258），test_api_v1_cli_schedule 2/2；旧 `dispatch` 改为共享实现的包装 |
| AC-5 死亡接管不重复物化（含孤儿 Crawl） | PASS | `test_expired_claim_is_taken_over_and_materializes_single_crawl`（attempts=2、同主键、1 Crawl）、`test_takeover_adopts_orphaned_crawl_without_duplicating_it`（认领既有 Crawl、不新增） |
| AC-6 维护失败可识别可重试 | PASS | `test_maintenance_failure_is_retriable_and_then_succeeds_once`：真实重命名 `core_snapshot` 表触发 OperationalError → failed/claim_until 已过期/error 含 OperationalError/finished_at 非空/attempts=1 → 同槽重领 attempts=2 succeeded → 第三次 skipped |
| AC-7 重启不重跑、next 严格推进 | PASS | `test_poll_path_skips_fresh_schedule_and_dispatches_once_due`（成功后 reload 即新进程语义，is_due False、二次 force=False skipped）、`test_force_run_materializes_current_slot_before_first_period`（next_run_at == 成功槽后下一次 fire，二次 force skipped） |
| AC-8 禁用/删除/改期旧执行不得提交 | PASS | `test_stale_owner_cannot_commit_after_disable_then_reenable_takes_over`（禁用后无 Crawl、run 仍 claimed；重新启用+租约过期后接管成功）、`test_reschedule_rejects_old_slot_commit_and_advances_on_new_cron`（旧提交落空、新 cron 新槽只跑一次、next_run 正确）、`test_deleting_schedule_cascades_runs_but_keeps_derived_crawl`（级联删 run、Crawl 双 SET NULL 保留） |
| AC-9 单实例/立即运行/展示/历史兼容 | PASS | test_cli_schedule 过滤后 7/7；test_ui_admin_crawl 29/29；test_frozen_crawl_config 9/9；test_api_v1_cli_schedule 2/2；迁移 --check 干净；纯槽函数边界 1/1；reindex 用例仅余环境基线失败 |
| AC-10 并发契约与代码模式（rubric，阈值≥4） | PASS（5/5） | 见 CP-6：唯一约束+CAS+短事务、无文件锁/选举/sleep 重试、维护事务外、复用既有 claim/心跳模式、跨后端成立、改动聚焦；ruff 无新增告警 |

---

## 三、Findings

### Blocker
无。

### Major
无。

### Minor

**M1. 维护心跳线程对“非守卫类”DB 异常无防护，线程静默死亡后存在维护本体被重复执行的窗口**

- 位置：[models.py:382-400](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L382-L400)
- 推理：`_beat()` 循环内的续租 UPDATE 没有 `try/except`。若续租语句抛出的不是“守卫不匹配（renewed==0）”而是瞬时 `OperationalError`（SQLite 持锁超过 busy_timeout、连接被重置等），异常逃出线程目标函数，心跳线程结束（仅经 threading excepthook 打一条堆栈到 stderr）；`guard_lost` 保持 False（且该属性主线程从不读取）。此时若维护本体运行时间超过剩余租约（60s）且另一编排方正好轮询，后者可 CAS 接管同一槽并再次执行 `run_scheduled_maintenance()`——维护**副作用**可能发生两次。旧所有者完成后的最终确认因 token 已更换必然 0 行，不可能双重 succeeded（FR-9 的“杜绝双重确认”成立），且维护是幂等批处理（入队迁移行 + 回填缺失索引），但 FR-3“实际副作用只发生一次”在这一极端窗口下不成立。触发需同时满足：续租 SQL 真异常 + 维护 >60s + 并发轮询；冻结/长 GC 停顿导致租约过期的同类接管窗口是租约机制本身允许的 at-least-once 语义，spec 明确接受。
- 建议修复：单拍续租包一层 `except OperationalError: continue`（配合 wait 间隔退避，失败不翻转 guard_lost、不退出线程）；或将瞬时异常与 `renewed != 1` 区分记录。无需改变最终确认逻辑。

**M2. crawl 侧守卫失配后 run 行短期残留为带活跃租约的 claimed，状态可观测性弱于维护侧**

- 位置：[models.py:261-264](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L261-L264)（对照维护侧 304-312 的 failed 收尾）
- 推理：禁用/改期导致确认 0 行时抛 `_SlotCommitGuardMiss` 回滚后，run 行仍为 `claimed`、`claim_until` 还在未来 60s。期间 DB 直查无法把它与“真正在飞”的领取区分开（只能比对 claim_until）；若改期后的新 cron 不再包含该物理触发时刻，该行将永远停留在 claimed（过期但非 failed），与 FR-8(c)“旧槽记录保留为可识别的 failed/失效记录”的字面期望略有出入（测试 test:272、313 断言 CLAIMED，属实现者有意选择）。功能安全：禁用后 claim_slot 前置 enabled 检查拦截；改期同刻槽位重合时，租约过期后由新 revision 接管，最坏只是该槽延迟 ≤60s 执行一次。
- 建议修复（可选）：守卫失配回滚后，像维护路径一样在事务外补一条 `UPDATE ... SET status=failed, claim_until=now, error=...`（WHERE token 匹配），使失效行立即可识别、可接管。

### Suggestion

**S1. `_MaintenanceLeaseHeartbeat.guard_lost` 是只写不读的死状态**

- 位置：[models.py:376](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L376)、[models.py:394-396](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L394-L396)
- 最终确认本身已用 token+enabled+revision CAS 独立判定，`guard_lost` 从未被主线程读取。建议删除，或在主线程收尾时据其输出一条可观测日志（与 M1 一并处理更自然）。

**S2. 维护失败重抛会终止整个 daemon tick（与改动前行为一致，非回归）**

- 位置：[models.py:288](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L288) → [runner.py:2326-2327](file:///f:/swe/098842/project-01/archivebox/services/runner.py#L2326-L2327)（循环无逐计划异常隔离）
- failed 状态已在重抛前持久化，进程重启（或下一次 CLI/轮询）即可按 AC-6 接管重试；旧 `dispatch()` 在同一位置同样不捕获，行为未变差。若希望 daemon 在维护真实失败后继续扫描本 tick 后续计划，可在 runner 的 for 循环内对单个 schedule 做异常隔离并记日志，属增强而非本需求必须。

**S3. revision 守卫依赖 `modified_at` 微秒级可区分**

- 位置：[models.py:254](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L254)、[294](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L294)、[1698](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L1698)
- 理论上同一微秒内两次保存会使旧 revision 仍相等；但 `auto_now` + DB 往返与磁盘写使实际碰撞概率可忽略，且 is_enabled 为独立第二守卫。无需改动，记录在案。

---

## 四、测试运行结果摘要

环境：WSL `cycle-foundation-docker`（musl/BusyBox v1.37），venv `.venv-linux`（Python 3.13.16），DrvFs `/mnt/f`。

| 命令（均在 WSL 内，`--basetemp=/tmp/pytest-ab-review*`，`-p no:cacheprovider -p no:xdist`） | 结果 |
|---|---|
| `pytest archivebox/tests/test_schedule_claims.py` | **13/13 PASS**（首轮 12 passed + 1 error：`archivebox init --quick` fixture 60s 超时，发生在与并行 WSL I/O 命令争用期间；单独重跑该用例 55.67s 通过，断言全绿，总耗时 337.63s） |
| `pytest archivebox/tests/test_cli_schedule.py -k 'not over_server and not one_shot'` | **7 passed, 2 deselected**（262.46s） |
| `pytest archivebox/tests/test_ui_admin_crawl.py` | **29 passed**（21.29s；15 条 Django70 get_actions 弃用告警为既有基线） |
| `pytest archivebox/tests/test_frozen_crawl_config.py` | **9 passed**（121.17s） |
| `pytest archivebox/tests/test_api_v1_cli_schedule.py`（AC-4 补充） | **2 passed**（92.16s） |
| `pytest archivebox/tests/test_cli_update_reindex_snapshots.py::test_update_schedule_backfills_missing_search_index_without_reopening_snapshot` | **1 FAILED — 判定为环境基线，与本变更无关**：本变更新增的全部断言（succeeded run==1、二次 dispatch 后 run 总数==1、stale_snapshot.retry_at 不变）通过；失败点在文件 400-407 行 `search_backend_sqlite` ArchiveResult SUCCEEDED/NORESULTS 计数 0==1。直接执行钩子脚本复现：`/usr/bin/env: unrecognized option: S`（钩子 shebang 为 `#!/usr/bin/env -S abxpkg run --script ... python3`，本发行版 `/bin/env` 为 BusyBox 不支持 `-S`），即任务书所述改动前同样失败的 musl 钩子不兼容 |
| `.venv-linux/bin/ruff check --select E,F --line-length 140 <全部改动文件>` | 仅 2 条 E501（archivebox_schedule.py:146/151），HEAD 基线同样存在（HEAD 138/143 行），**无新增告警**；migrations 在 pyproject ruff exclude 内 |
| `python archivebox/manage.py makemigrations --check --dry-run` | `No changes detected`（exit 0） |

环境不可用项（按任务书甄别，未计入发现）：playwright/浏览器/`server --daemonize`、ldap；search_backend_sqlite 子进程钩子（BusyBox `env -S`，已实证）。

## 五、总体结论

并发互斥、短事务/租约、接管幂等、配置变更守卫、历史兼容与跨后端契约六个检查点全部 PASS，AC-1~AC-10 全部满足（rubric 5/5）。无 blocker、无 major；2 条 minor（心跳线程瞬时异常防护、crawl 侧失配行可观测性）与 3 条 suggestion 均为加固性质，不阻断合入。

---

## 六、第 2 轮复核（修复增量）

- 复核日期：2026-11-03
- 复核范围：实现者针对 M1/M2 的修复增量（[models.py](file:///f:/swe/098842/project-01/archivebox/crawls/models.py) 与 [test_schedule_claims.py](file:///f:/swe/098842/project-01/archivebox/tests/test_schedule_claims.py)，测试文件为未跟踪新文件故直接通读现版）；fresh context 只读，未改代码。

### M1（心跳瞬时异常防护 + 收尾告警）— **修复正确，PASS**

1. `_MaintenanceLeaseHeartbeat._beat()` 的续租 UPDATE 现包有 `except OperationalError: continue`（[models.py:408-419](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L408-L419)）：瞬时持锁/连接抖动不再杀死心跳线程，下一拍（固定 15s 节拍，非紧重试循环）继续续租；不翻转 `guard_lost`，而禁用/改期/被接管仍通过下一拍 `renewed == 0` 如实检出（token/revision/enabled CAS 语义不变）。
2. “掩盖真错误”评估：SQLite 下 `no such table` 等也属 OperationalError，但维护本体在主线程同步做真实 DB 访问，库不可用时本体先抛 `BaseException` → 既有失败收尾（failed+claim_until=now+error 截断 1000 字）并重抛，不会被心跳的 except 吞掉；最终确认 CAS 在主线程无包裹，其异常行为与修复前一致。心跳重试受维护本体时长与 `stop()`（5s join、daemon 线程）上界约束，不会无限空转。
3. 连接泄漏评估：线程使用 Django 线程本地连接，异常后由 Django 按可用性在下次查询时重连；线程退出路径（stop 置位或 guard_lost 返回）的 `finally: connections.close_all()`（[models.py:423-426](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L423-L426)）保持不变，无泄漏。
4. `_run_maintenance_slot` 守卫失配收尾在 `confirmed != 1` 且 `heartbeat.guard_lost` 时新增 `logger.warning`（[models.py:320-325](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L320-L325)），顺带消除了第 1 轮 S1 的“只写不读”问题；failed 收尾与 DISPATCH_SKIPPED 返回不变。

### M2（crawl 侧守卫失配后独立 autocommit UPDATE 置失效）— **修复正确，PASS**

1. 事务边界核查：`claim_slot` 的 INSERT/接管 UPDATE 均已独立 autocommit（[models.py:539-578](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L539-L578)），物化事务回滚不撤销 claim 行，故回滚后 `filter(pk=run.pk, claim_token=run.claim_token).update(...)`（[models.py:271-277](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L271-L277)）在禁用/改期场景必然命中本持有者行（token 未被触碰），置 failed、claim_until=now、finished_at=now、error 文案；Crawl 插入随 `_SlotCommitGuardMiss` 回滚，无半留。
2. 删除计划场景：SQLite 下删除已 CASCADE 删 run 行，随后 `Crawl.objects.create(schedule=self, schedule_run=run)` 触发 FK IntegrityError → 走既有 `except IntegrityError` 分支 skipped（[models.py:279-281](file:///f:/swe/098842/project-01/archivebox/crawls/models.py#L279-L281)）；PostgreSQL 下若删除与守卫 UPDATE 并发，JOIN schedule 得 0 行 → guard miss → 回滚后的 UPDATE 因 run 已被 CASCADE 删除而匹配 0 行，静默落空，安全。该 UPDATE 不涉 FK/唯一列，自身无新异常面。
3. 防误覆盖：WHERE 带 `claim_token`，若极端情况下租约已过期且槽位已被新 owner 接管（token 已换），UPDATE 匹配 0 行，不会覆盖新 owner；`attempts` 不重置，接管时 +1（测试实测 attempts=2）。改期后新旧槽位重合时由“等 ≤60s”变为立即接管，严格优于旧行为。
4. 异常分支顺序正确：`_SlotCommitGuardMiss` 直接继承 Exception，不遮蔽 IntegrityError；异常在 `atomic()` 外被捕获，补 UPDATE 处于干净 autocommit 状态。

### CP-1~CP-6 回归

- CP-1 互斥/CP-3 接管：claim_slot 未改；failed+claim_until=now 立即可被同主键 CAS 接管（接管 UPDATE 的 `status__in` 含 FAILED），仅加强、无回退。
- CP-2 维护租约：M1 后心跳更健壮，最终确认 token+enabled+revision CAS 未动，仍不可能双重 succeeded。
- CP-4 守卫：crawl 侧与维护侧收尾现已对齐（failed/expired/error），失配行立即可识别可接管。
- CP-5 展示/兼容、CP-6 并发契约：无展示/迁移/CLI 改动；未引入文件锁、选举或领取路径上的 sleep 重试（心跳为既有 15s 固定节拍）。

### 测试与 lint（WSL `cycle-foundation-docker`，musl，venv `.venv-linux`）

| 命令 | 结果 |
|---|---|
| `python -m pytest archivebox/tests/test_schedule_claims.py -q -p no:xdist -p no:cacheprovider --basetemp=/tmp/pytest-ab-r2 -k "not concurrent"`（PATH 前置 `.venv-linux/bin`） | **11 passed, 2 deselected**（190.67s），含禁用/改期守卫两个更新用例与 `maintenance_failure` |
| 同上单跑 `-k maintenance_failure -v` | **1 passed**（23.10s）：真实 `ALTER TABLE RENAME` 触发 OperationalError → failed/claim_until 过期/error 含 OperationalError → 同槽接管 attempts=2 succeeded 的链路具名通过 |
| `.venv-linux/bin/ruff check --select E,F --line-length 140 archivebox/crawls/models.py archivebox/tests/test_schedule_claims.py` | **All checks passed!**（`import logging` 与模块级 `logger` 无未用/排序类告警） |

测试更新核查：禁用用例（test:260-282）与改期用例（test:285-319）改为断言旧 run=FAILED、claim_until 已过期、无 Crawl 残留，重新启用/新 cron 后无需等租约立即接管（attempts=2、恰 1 个 Crawl、新槽只派发一次），断言针对真实 DB 行与真实 CLI 初始化，无 mock/monkeypatch，与修复语义一致。

### 新发现

**无 blocker、无 major、无 minor。** M1/M2 修复与第 1 轮建议一致且实现严谨，未引入新问题；既有 CP-1~CP-6 结论维持 PASS。
