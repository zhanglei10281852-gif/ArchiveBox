# CrawlSchedule 时间槽执行权（持久领取）- 实施计划

## Task 1: 时间槽纯计算函数
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Completion Evidence**:
  - `current_slot_for_schedule` 已加入 archivebox/crawls/schedule_util.py；WSL(Linux) venv 实测边界：00:00:00→当日 00:00、00:00:01→当日 00:00、前一日 23:59:59→前一日 00:00；hourly/`*/5` 整点归属正确；成功槽后 next fire 正确（11-03 槽→11-04 00:00）。直接 `get_prev` 在整点返回前一时刻，证实双步计算必要。

## Task 2: CrawlScheduleRun 模型与迁移
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Completion Evidence**:
  - archivebox/crawls/models.py 新增 `CrawlScheduleRun`（唯一约束 `(schedule, slot_at)`、`(status, claim_until)` 索引、token/租约/attempts/claimed_by/error 字段）；`Crawl` 新增 nullable OneToOne `schedule_run`（related_name="crawl"）。
  - 迁移 archivebox/crawls/migrations/0020_schedule_run_claims.py；`manage.py makemigrations --check` 无缺失；`test_migrations_fresh.py` 12/12 通过；既有库（0019）经真实启动迁移后 SQL 核对：新表、唯一约束、claim 索引、crawls_crawl.schedule_run_id（FK SET NULL）全部就位。

## Task 3: 共享领取/派发服务（核心）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2
- **Completion Evidence**:
  - `CrawlSchedule.dispatch_due(force)` 为唯一共享入口；`CrawlScheduleRun.claim_slot` 以唯一约束+CAS UPDATE 领取/接管（无 select_for_update/文件锁）；普通归档“领取+物化+守卫确认”在一条短事务内，修订戳（schedule.modified_at）+is_enabled 守卫，失败回滚不留 Crawl；维护事务外执行，`_MaintenanceLeaseHeartbeat` 15s CAS 心跳/60s 租约，异常→failed 可接管，守卫失效→failed 不冒充成功。
  - 证据：`test_schedule_claims.py` 13/13 通过（互斥领取、过期接管、孤儿 Crawl 认领、禁用/删除/改期守卫、重启幂等、真实 DB 故障 failed→重试成功）。

## Task 4: CLI / API / 后台轮询接入共享路径
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 3
- **Completion Evidence**:
  - CLI `--run-all` 与 runner daemon tick 均仅调用 `dispatch_due`（grep 确认生产代码无其它派发分叉；API /cli/schedule 复用 CLI 函数）；`_active_schedules()`/daemon queryset 经 `with_run_state()` 单条聚合注释避免逐行查询。
  - 证据：`test_cli_schedule.py` 非 server 用例 7/7；并发 run-all 两个子进程测试通过；API view 测试通过；admin 29/29。

## Task 5: 并发集成测试与兼容性测试
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 4
- **Completion Evidence**:
  - 新增 archivebox/tests/test_schedule_claims.py 共 13 用例：纯槽函数 1、模型级真实 DB 10（含禁用/删除/改期守卫、接管矩阵、维护真实故障）、真实 OS 子进程并发 2（crawl 双 run-all 恰 1 个 Crawl+1 succeeded；maintenance 双 run-all 恰 1 succeeded；二次 run-all 输出 Enqueued 0）。全部通过。
  - 按新语义更新 test_cli_update_reindex_snapshots.py：同槽两次 dispatch 仅 1 条 succeeded run、不重复排队；新增断言全通过（该用例剩余 ArchiveResult 计数失败为 musl 环境钩子 shebang 不兼容的既有问题，未改动代码前同样失败）。
  - 兼容性：frozen_crawl_config 9/9、ui_admin_crawl 29/29、api_v1_cli_schedule view 1/1、migrations_fresh 12/12。server/守护/browser 类用例在当前 musl/BusyBox 环境无法运行（二进制与 daemon 化限制，属环境基线，非本变更引入）。

## Task 6: 全量验证与静态检查
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 5
- **Completion Evidence**:
  - 仓库等价 ruff 规则（E/F，line=140）下改动文件无新增告警（仅剩 archivebox_schedule.py 两条既有 E501，diff 确认未触碰）；已修正 CRLF（Edit 工具曾把 LF 文件写成 CRLF，全部转回 LF，diff 仅剩真实改动）。
  - 跨后端：全部协调均为 ORM 原生 CAS UPDATE/唯一约束（SQLite 实测；PostgreSQL READ COMMITTED 下 CAS 语义同样成立），无文件锁、无长事务包裹维护、无 sleep 重试。
  - 工作区临时文件（.wsl_*.sh/.py/.log、.venv-linux 等）为验证环境副产物，收尾时清理。
