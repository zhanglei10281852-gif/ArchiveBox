# CrawlSchedule 时间槽执行权（持久领取）- 产品需求文档

## Overview
- **Summary**: 为每条 `CrawlSchedule` 的具体 cron 时间槽引入数据库持久化的执行权（claim）机制，保证主服务与独立 runner 并发运行时，同一到期计划不会派发两份相同工作；普通归档每个时间槽只物化一个归属该槽的 `Crawl`，计划型维护每个时间槽只成功执行一次。
- **Purpose**: 消除 `archivebox schedule --run-all` 与后台轮询（`run_pending_crawls(daemon=True)`）在多进程/多实例下对同一到期时间槽的重复派发；同时保证进程崩溃、写入失败、维护失败后时间槽可识别、可接管，并在管理员禁用/删除/改期计划时杜绝旧执行冒充新配置提交。
- **Target Users**: 同时运行主服务与独立 runner（如 `archivebox server` + `archivebox schedule --foreground` / `archivebox run --daemon`）的自托管 ArchiveBox 用户；管理计划任务的管理员。

## Goals
- 每个 `(CrawlSchedule, cron 时间槽)` 拥有数据库中可持久验证的唯一执行权记录。
- 领取（claim）→ 执行 → 确认（succeeded）全流程由短事务/条件 UPDATE（CAS）驱动，SQLite 与 PostgreSQL 均正确，不引入文件锁或基于定时器的选举。
- `schedule --run-all` 与后台轮询共用同一领取/派发代码路径与规则。
- 领取者进程退出、领取后物化写入失败、维护执行失败时，时间槽保持可识别，租约过期后允许其他进程安全接管（含幂等认领地坑）。
- 已确认成功的时间槽在进程重启后不得再次执行；下一合法周期严格按当前 cron 表达式推进。
- 领取期间管理员禁用、删除或改期（cron/config/template）计划，旧执行不能提交为新配置下的运行；改期后的下一次合法周期按更新后的 cron 计算。
- 单实例正常调度、手动立即运行、计划展示（CLI/API/UI）以及历史派生 Crawl（无时间槽记录）保持兼容。

## Non-Goals
- 不做多机编排器选举、不引入文件租约（遵循 AGENTS.md 并发契约；未来多机以 PostgreSQL 行级领取为边界）。
- 不补齐停机期间错过的多个历史周期：守护进程停机很久后恢复时，只为最近一个到期槽执行一次（与现状“一次到期派发一次”一致）。
- 不改变已物化 `Crawl` 的归档执行/重试生命周期（Crawl 进入既有队列后仍由现有 runner 领取处理）。
- 不改变计划维护（`run_scheduled_maintenance`）的具体工作内容（文件系统迁移入队 + 搜索索引回填）。
- 不提供新的面向用户的管理页面（时间槽记录可通过模型/数据库识别即可；admin 只读展示为可选项）。

## Background & Context
- 现状：`CrawlSchedule.dispatch()`（archivebox/crawls/models.py）无任何并发保护：
  - 普通归档：`enqueue()` 直接 `Crawl.objects.create(...)`；
  - 维护：直接同步调用 `run_scheduled_maintenance()` 并把 `modified_at` 前移。
- 两个派发方彼此独立：CLI `schedule --run-all`（archivebox/cli/archivebox_schedule.py）与守护轮询 `run_pending_crawls`（archivebox/services/runner.py 约 2318-2322 行）都执行“枚举启用计划 → `is_due()` → `dispatch()`”。两个进程同时到期时各自物化 Crawl / 各跑一次维护。
- 到期判定基于 `last_run_at`（update 类取 `modified_at`，crawl 类取最新派生 Crawl 的 `created_at`，无派生时回退 template/创建时间），崩溃重启后无法区分“已派发未确认”与“从未派发”。
- 代码库已有可直接复用的成熟模式：
  - `ModelWithQueue.claim_for_worker()`（archivebox/workers/models.py）：以 `retry_at` 旧值为条件的 CAS UPDATE 租约；
  - `importers.service.enqueue_due()`（archivebox/importers/service.py）：一个短事务内“条件 UPDATE 推进 next_run_at + 持久 enqueue”，并以 enabled/旧 next_run_at 作为守卫；
  - runner 中 Crawl/Snapshot 的 STARTED 租约心跳（`ACTIVE_STATE_LEASE_SECONDS=60`，10s 心跳）。
- AGENTS.md 明确：SQLite 保持并发短写；网络/hook/文件系统工作不得包在数据库事务内；协调必须走数据库支撑的 per-crawl/per-snapshot 领取，禁止文件租约与定时器选举。

## Functional Requirements
- **FR-1（时间槽标识）**: 每个启用的计划在每个 cron 触发时刻 `slot_at`（`<= now` 的最近一次 cron fire）至多对应一条持久的执行记录，记录包含：所属计划、槽时间、状态（claimed/succeeded/failed）、领取令牌、租约起止、领取者标识（主机/pid）、尝试次数、完成时间、错误信息。
- **FR-2（唯一物化）**: 普通归档计划一次领取成功后，只允许创建一个 `Crawl`，且该 Crawl 可经唯一关联回溯到具体时间槽；重复/并发领取不得创建第二个 Crawl。
- **FR-3（维护单次成功）**: 计划型维护在同一时间槽内只允许一个领取者把结果确认为 succeeded；租约有效时其他派发方跳过；确认后任何重复派发（含重启后）均跳过。
- **FR-4（共用规则）**: `schedule --run-all`（含 API `/cli/schedule`）与 `run_pending_crawls` 后台轮询必须调用同一领取/派发服务函数，不允许各自保留独立派发逻辑。
- **FR-5（手动立即运行）**: `--run-all` 对“尚未到首个合法周期”的计划允许强制执行当前槽一次（等价于把当前 cron 槽标记为已执行）；强制执行后守护轮询不得对同一槽再次派发；下一次运行按 cron 正常推进。
- **FR-6（崩溃/失败接管）**:
  - claimed 记录租约过期后（领取进程退出），其他进程可 CAS 接管同一槽记录（attempts 递增），而不是新建槽记录或重复物化。
  - 普通归档：若接管时发现该槽已有 Crawl（先前已物化但确认写入失败/进程死亡），必须认领既有 Crawl 并补确认，不得再创建；若无 Crawl，则补物化。
  - 维护：维护抛错时槽记录置为 failed（错误可识别、租约立即过期），下一到期轮询可重新领取并重试。
- **FR-7（重启幂等）**: succeeded 槽记录是重启后唯一的权威锚点；存在 succeeded 的槽永不再执行；到期/下一运行时间从最近 succeeded 槽（无记录时沿用现有创建锚点）按 cron 推导。
- **FR-8（配置变更防护）**:
  - 领取后到确认前，计划被禁用：旧执行的确认守卫失败，不产生新 Crawl、不记录 succeeded；维护即使已在跑也不得把该槽确认成功。
  - 计划被删除：级联删除槽记录，旧执行任何确认/物化写入因外键或守卫失败而落空；既有历史派生 Crawl 按现有 SET NULL 语义保留。
  - cron/config/template 被修改（改期）：以计划修订戳（`modified_at`）为守卫，旧执行不得提交；下一轮按新 cron 计算槽时间与下一合法周期；旧槽记录保留为可识别的 failed/失效记录。
- **FR-9（维护租约心跳）**: 维护执行可能跨越多个批次/较长时间，领取者须以短 UPDATE 定期续租（令牌+修订+启用状态共同守卫）；续租失败不强行打断维护本体，但最终确认必然因守卫失败而落空，杜绝双重确认。
- **FR-10（兼容性）**:
  - 单实例下“到期才派发、未到期不派发”的行为与当前一致（`make_latest_schedule_due` 式到期手段继续生效）。
  - CLI `schedule --show`/默认输出、API、admin 中的计划展示（next_run 等）保持可用且语义一致；历史派生 Crawl（槽关联为空）正常列出与处理；`CrawlSchedule.enqueue()` 现有冻结配置行为不变。

## Non-Functional Requirements
- **NFR-1（并发正确性）**: 所有领取/确认在 SQLite（并发 CLI+server 短写）与 PostgreSQL 上均成立；不依赖 `select_for_update`（SQLite 空操作），以唯一约束 + CAS 条件 UPDATE + 必要的单条短事务为正确性基础。
- **NFR-2（短事务）**: 普通归档“领取+物化+确认”为一条短事务（无网络/文件系统/hook）；维护本体在事务外执行，仅领取/续租/确认为短写。
- **NFR-3（可观测）**: 槽记录在数据库中可直接查询识别（计划、槽时间、状态、领取者、attempts、错误）；CLI `--run-all` 的汇总输出保持现有风格并反映实际派发/跳过数量。
- **NFR-4（最小改动）**: 遵循现有代码模式（claim_for_worker / importers 领取 / runner 心跳），不新增包装层/抽象层；迁移为新增表与可空字段，不破坏既有数据。

## Constraints
- **Technical**: Django ORM，同时支持 SQLite（默认，本地文件系统）与 PostgreSQL；使用 `uv run` 生态；遵守 AGENTS.md：禁止 mock/monkeypatch/假进程，测试用真实 CLI、真实 DB 行、真实文件与既有 fixture；不得弱化/跳过既有测试。
- **Business**: 保持 dev 分支；不发布任何包/镜像/tag。
- **Dependencies**: `croniter`（已用于 cron 计算）；现有 `Crawl`/`CrawlSchedule` 模型、runner、CLI、API 表面。

## Assumptions
- 停机恢复只执行最近一个到期槽，不逐槽补齐历史（与现状一致，见 Non-Goals）。
- 槽时间以启用时区下的 cron 触发时刻为准（`croniter.get_prev(datetime)` 对 `<= now` 的最近一次触发），与现有 `next_run_for_schedule` 使用相同的 aware datetime 口径。
- 普通归档的“槽成功”以 Crawl 行持久物化为准；其后归档本身的成败/重试归属既有 Crawl 生命周期，不影响槽的单次派发语义。
- 租约时长取 60s、维护心跳间隔约 15s（与 runner 现有 ACTIVE_STATE_LEASE_SECONDS 口径一致），属实现常量可调。

## Acceptance Criteria

### AC-1: 并发派发同一到期槽只物化一个 Crawl
- **Type**: `rule`
- **Given**: 一个已到期的普通归档计划，两个真实 OS 进程同时执行 `archivebox schedule --run-all`（或分别经 run-all 与后台轮询路径）。
- **When**: 两个进程并发完成领取/物化。
- **Then**: 数据库中该时间槽只有一条执行记录、只存在一个归属该槽的派生 Crawl；另一进程报告跳过/未新增。
- **Pass Condition**: 并发执行后 `CrawlScheduleRun` 中该 `(schedule, slot_at)` 恰 1 行且 succeeded；关联该槽的 Crawl 恰 1 个。
- **Evidence**: 新增并发 CLI 子进程集成测试（真实 data dir + 真实 sqlite），断言行数与 Crawl 归属。

### AC-2: 时间槽执行权持久、可验证、可互斥
- **Type**: `rule`
- **Given**: 任一启用计划的到期槽。
- **When**: 进程 A 领取后租约未过期，进程 B 尝试领取同一槽。
- **Then**: B 领取失败（唯一约束/CAS），不产生第二条 claimed 记录；记录含令牌、租约截止时间、领取者、attempts。
- **Pass Condition**: 直接对真实 DB 顺序/并发调用共享领取函数：有效租约内第二次领取返回 None 且行仍为 1；记录字段齐全可查询。
- **Evidence**: 模型级测试断言 CAS 结果、记录字段与行数。

### AC-3: 计划型维护每个时间槽只成功一次
- **Type**: `rule`
- **Given**: 一个到期的维护类计划（SCHEDULE_KIND=update）。
- **When**: 同一槽被重复派发（连续两次或并发两个派发方）。
- **Then**: 仅一个领取者执行并确认 succeeded；第二次/另一进程跳过；重启后再次派发仍跳过。
- **Pass Condition**: 该槽恰 1 条 succeeded 记录；重复派发不新增执行、不重复确认；`run_scheduled_maintenance` 实际副作用只发生一次（以真实排队的维护行/索引结果计数为证）。
- **Evidence**: 模型级 + CLI 级测试，断言 run 状态/计数与维护副作用计数。

### AC-4: run-all 与后台轮询共用同一套领取规则
- **Type**: `rule`
- **Given**: 同一到期计划集合。
- **When**: 分别经由 CLI `--run-all`、API `/cli/schedule {run_all:true}`、`run_pending_crawls(daemon=True)` 轮询派发。
- **Then**: 三者调用同一共享领取/派发服务，唯一约束与接管/守卫行为完全一致；不存在绕过该服务的直接 `enqueue`/维护调用。
- **GIVEN 路径检查**: 代码中 CLI 与 runner 的计划派发仅经由共享函数（grep/代码审查证据）。
- **Pass Condition**: 三条入口的集成/模型测试均出现“同槽第二次被跳过”；旧 `CrawlSchedule.dispatch` 直调路径被收敛或改为共享实现。
- **Evidence**: 测试 + 代码结构证据。

### AC-5: 领取后进程退出允许安全接管，不重复物化
- **Type**: `rule`
- **Given**: 槽记录为 claimed 且 `claim_until` 已过期（模拟进程死亡）。
- **When**: 另一进程/下一轮轮询接管：(a) 无 Crawl；(b) 已存在归属该槽的 Crawl（确认写入失败场景）。
- **Then**: (a) 复用同一行（attempts+1）并补物化一个 Crawl；(b) 认领既有 Crawl 补确认成功，不新增 Crawl。
- **Pass Condition**: 两种场景最终均恰 1 个 Crawl、同一槽记录（同一主键）、状态 succeeded、attempts 反映接管。
- **Evidence**: 模型级测试：手工构造过期 claimed 行（含/不含关联 Crawl）后运行真实接管路径。

### AC-6: 维护失败后槽保持可识别并可重试
- **Type**: `rule`
- **Given**: 维护领取者执行 `run_scheduled_maintenance` 抛错。
- **When**: 派发路径完成失败收尾。
- **Then**: 槽记录为 failed、记录错误信息、租约立即可被接管；下一到期轮询重新领取同一槽并重试成功。
- **Pass Condition**: failed→再次领取（attempts 递增）→succeeded 的真实状态转换可被断言（失败以真实异常路径触发，不使用 mock：可经不可写/无效目标或既有维护失败夹具构造）。
- **Evidence**: 模型级测试断言状态机与错误信息。

### AC-7: 重启后已成功周期不再执行
- **Type**: `rule`
- **Given**: 某槽已存在 succeeded 记录（模拟进程重启：全新 ORM 查询/新 CLI 进程）。
- **When**: 后台轮询或 `--run-all` 再次扫描该计划。
- **Then**: `is_due` 为 False（当前槽已成功），共享派发跳过；不新增 Crawl/维护。
- **Pass Condition**: 成功记录写入后，新进程/新查询下 `is_due()` 为 False，派发返回跳过；下一合法 `next_run_at` 严格等于成功槽之后的下一次 cron fire。
- **Evidence**: 模型级 + CLI 子进程测试。

### AC-8: 领取期间禁用/删除/改期计划时旧执行不能提交
- **Type**: `rule`
- **Given**: 槽已被领取（普通归档在物化短事务内；维护在执行/确认前）。
- **When**: 管理员分别 (a) 禁用计划；(b) 删除计划；(c) 修改 cron 或 config/template（改期）。
- **Then**:
  - (a) 不产生新 Crawl、维护不确认成功；
  - (b) 槽记录随计划级联删除，旧执行的物化/确认全部落空，历史派生 Crawl 仍按 SET NULL 保留；
  - (c) 旧执行不提交（守卫失败/短事务回滚），无“旧槽产出的新配置 Crawl”；下一轮按新 cron 得出新槽并正常派发一次，旧槽留可识别记录。
- **Pass Condition**: 三种真实 DB 状态序列下断言：Crawl 行数、run 状态、新 cron 下 `is_due/next_run_at`、历史 Crawl 保留情况。
- **Evidence**: 模型级测试（真实事务与条件 UPDATE，无 mock）。

### AC-9: 单实例调度、立即运行、计划展示与历史数据兼容
- **Type**: `rule`
- **Given**: 既有测试夹具与既有库（存在历史派生 Crawl、无槽记录）。
- **When**: 执行现有 schedule CLI/API/UI 场景：创建、show、clear、到期经 server 真实归档、未到期 add 不触发计划、run-all 立即执行。
- **Then**: 既有行为全部保持（必要时仅更新被本需求刻意改变的断言：维护同槽重复执行两次 → 一次）；历史 Crawl 列表/详情正常；新建计划未到期不被轮询派发，run-all 强制执行一次后轮询不再重复。
- **Pass Condition**: `test_cli_schedule.py`、`test_api_v1_cli_schedule.py`、`test_ui_admin_crawl.py`、`test_ui_add_view_runtime.py`、`test_frozen_crawl_config.py`、`test_cli_update_reindex_snapshots.py` 等相关测试（按本需求更新断言后）全部通过；迁移在既有库上可执行。
- **Evidence**: `uv run pytest` 相关测试输出与迁移应用结果。

### AC-10: 并发契约与代码模式遵循度
- **Type**: `rubric`
- **Dimension**: 实现是否严格遵循仓库并发契约（DB 支撑的领取、短 autocommit/CAS、无文件租约/选举、长工作在事务外、SQLite+PostgreSQL 可行）并贴合既有 claim/心跳模式且改动聚焦
- **Scale**: 1-5
- **Anchors**: 1 = 引入文件锁/睡眠重试/放宽唯一约束或长事务包裹维护；3 = 规则正确但另起并行机制或有冗余抽象；5 = 完全复用 claim_for_worker/importers/runner 心跳既有模式，最短事务，跨后端成立，无多余包装
- **Pass Threshold**: >= 4
- **Evidence**: 独立代码审查（迁移、模型、CLI、runner diff）与 `uv run prek run --all-files` 结果。

## Open Questions
- 无阻塞性开放问题。以下按 Assumptions 处理，如用户不认可可在审批时纠正：
  - [ ] 停机错过多个周期时只补最近一个槽（不逐周期补跑）。
  - [ ] 普通归档的槽成功点定义为 Crawl 持久物化（归档本体成败归 Crawl 生命周期）。
