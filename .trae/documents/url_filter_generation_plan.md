# URL 过滤代次与抓取收敛 Implementation Plan

## Repository Research

### 现状

- 过滤规则存放在 `Crawl.config` 的 `URL_ALLOWLIST` / `URL_DENYLIST`（frozen config，见 [common.py](file:///f:/swe/098842/project-02/archivebox/config/common.py) `build_crawl_config_snapshot`），匹配逻辑在 [crawls/models.py](file:///f:/swe/098842/project-02/archivebox/crawls/models.py) `Crawl.url_passes_filters / _pattern_matches_url`。
- 两个修改入口：
  1. 管理员表单 `CrawlAdminForm.save()` → `set_url_filters()` → `Crawl.save()` → `apply_crawl_config_filters()`（[crawls/admin.py:607-628](file:///f:/swe/098842/project-02/archivebox/crawls/admin.py#L607-L628)）。
  2. “排除此域名” `exclude_domain_view` → `Crawl.exclude_domain()` → 写 denylist + `apply_crawl_config_filters()`（[crawls/admin.py:1172-1184](file:///f:/swe/098842/project-02/archivebox/crawls/admin.py#L1172-L1184)）。
- `apply_crawl_config_filters()` 当前直接对 queued/started/paused 且不匹配的 snapshot 调 `snapshot.cancel_running_hooks()`（**该方法在代码库中不存在，started 行命中即 AttributeError**）并 queryset `.delete()` 硬删。
- 已存在成熟的持久删除队列：`Snapshot.DELETING_STATE="deleting"`、`SnapshotQuerySet.request_delete()`（status=deleting, retry_at=RETRY_AT_MAX）、`Snapshot.delete()`（杀进程→删文件→硬删行）、`Snapshot.delete_requested()` 批处理（OSError 时保留 deleting 行重试），runner 在 [runner.py:2280-2316](file:///f:/swe/098842/project-02/archivebox/services/runner.py#L2280-L2316) 后台线程/每轮循环消费它，重启后继续（已有 `test_runner_deletion.py` 覆盖）。普通 admin/API 删除即走此队列（API）或同步硬删（admin 单条）。
- 调度只认 RUNNABLE_STATES：`run_pending_crawls` 的 active_snapshots 查询、`CrawlRunner.load_run_state()`、`start_processing()`/`seal()` 的 CAS 全部排除 deleting；`Snapshot.seal()` CAS 仅 OPEN_STATES。deleting 行不会被正常调度/seal。
- 写入边界：
  - `Crawl.urls` 追加只有 `Crawl.add_url()`（现在用内存 config 判定，stale 实例可能用过时规则）；修剪只有 `prune_urls()`。
  - 种子创建：`Crawl.create_snapshots_from_urls()` 与 runner `CrawlRunner.create_initial_snapshots()`（后者实际也走 `create_discovered_snapshots(depth=0)`）。
  - 递归发现：`Crawl.create_discovered_snapshots()`；提交侧由 `services/snapshot_service.py project_discovered_snapshots()` 在 parser ArchiveResult 完成/Snapshot 完成时投影。
  - runner 领取：`_run_due_snapshot_locked()`（单 snapshot 路径）与 crawl 隔离路径的 `SnapshotService.on_SnapshotEvent`。
  - runner 提交：`finalize_completed_snapshot()`（seal、downloaded_at、index）与 `services/archive_result_service.py`（ArchiveResult 落库、title 更新、发现投影、`mark_archiveresult_started`）。
- Crawl 最新迁移 `crawls/0019`；Snapshot 最新迁移 `core/0060`。

### 关键缺陷（本次修复）

1. 规则修改无代次：并发/旧页面解析可在改规则后把同域链接经 `add_url`/发现重新入队。
2. started snapshot 收敛路径调用不存在的 `cancel_running_hooks()`，且在 HTTP 请求内同步硬删（违背“长活属于 runner”约定）。
3. deleting snapshot 仍可被迟到 ArchiveResult 事件更新（落结果、改 title、投影发现），`schedule_plugin_run` 还可能把 deleting 翻回 queued。
4. 表单保存与排除域名是两条近似但不同的流程；响应只有 `removed_urls/deleted_snapshots`，不反映接受/取消/待清理。

## 设计：过滤代次（filter generation）

- `Crawl.url_filter_version`（PositiveBigIntegerField，default=0）：当前规则代次。仅当归一化后的 allowlist/denylist **实际变化**时 CAS +1。
- `Snapshot.url_filter_version`（PositiveBigIntegerField，default=0）：该 snapshot 被接受入队时的代次。种子/发现创建时盖当前 crawl 代次；历史行与其它创建路径默认 0。
- 复核规则（runner/提交/发现统一）：
  - `snapshot.url_filter_version == crawl.url_filter_version` → 接受（快路径，规则自接受后未变）。
  - 代次不同 → 用 DB 最新 crawl config 重新跑 `url_passes_filters`：通过则把 snapshot 代次 CAS 刷新为当前代次（规则放宽，保留既有行，不做任何自动恢复）；不通过则收敛到 deleting。
  - 因此：sealed 历史行默认不动；放宽规则不会误删，也不会自动恢复已清理行，只影响之后的新提交/新发现。

## Files and Modules

- `archivebox/crawls/models.py`
  - 新增 `url_filter_version` 字段；新增统一变更流程 `apply_url_filter_change(allowlist, denylist)`；重写收敛逻辑 `apply_crawl_config_filters()`（prune urls + open snapshots → request_delete，sealed 保留）；`exclude_domain()` 改走统一流程；`add_url()` 与种子/发现创建在写入前刷新 config+代次并给新 Snapshot 盖代次。
- `archivebox/core/models.py`
  - 新增 `url_filter_version` 字段；新增 `accepted_under_current_url_filters()`、`reconcile_url_filter_generation()`、幂等收敛 helper；`schedule_plugin_run` 拒绝从 deleting 翻状态。
- `archivebox/crawls/admin.py`
  - `CrawlAdminForm.save()` 与 `exclude_domain_view` 统一调用 `apply_url_filter_change()`（含 raw config JSON 直接改过滤项的归一化 diff）；`save_model` 用 message 展示接受/取消/待清理数量；JSON 响应补充计数字段。
- `archivebox/services/runner.py`
  - `_run_due_snapshot_locked()` 领取后先跑 `reconcile_url_filter_generation()`。
- `archivebox/services/snapshot_service.py`
  - `project_discovered_snapshots()`、`finalize_completed_snapshot()`、`on_SnapshotEvent` 在写入前按当前代次复核；deleting/被拒即丢弃迟到结果、不 seal、不投影发现，转为 request_delete。
- `archivebox/services/archive_result_service.py`
  - `_save_archiveresult_event_to_db()` 与 `mark_archiveresult_started()` 对 deleting/被当前规则拒绝的 snapshot 直接丢弃事件（不落 ArchiveResult、不改 title/output_size、不投影发现）。
- `archivebox/api/v1_core.py`
  - PATCH snapshot 禁止把 deleting 改回活动态（409）；archiveresult 上传端点对 deleting snapshot 返回 409。
- 新迁移：
  - `archivebox/crawls/migrations/0020_crawl_url_filter_version.py`
  - `archivebox/core/migrations/0061_snapshot_url_filter_version.py`
- 测试：
  - 更新 `tests/test_ui_admin_crawl.py` 中排除域名端点用例（新异步收敛语义 + 新响应字段）。
  - 新增 `tests/test_filter_generation.py`：代次 bump 语义、queued/paused/started 收敛、迟到结果不复活、sealed 保留、放宽不恢复、领取闸门、ONLY_NEW/深度/容量兼容性。

## Implementation Steps

1. **迁移与模型字段**：新增两个字段与迁移（default=0，历史行自动回填）。
2. **Crawl 统一变更流程**（依赖 1）：
   - `normalized_url_filters()`；CAS 循环实现 `apply_url_filter_change()`（条件 UPDATE：`filter(pk=, url_filter_version=读取代次).update(config=..., url_filter_version=F(..)+1, modified_at=now)`，冲突重读重试，最多若干次）；“有效修改”判定 = 归一化 pattern 列表不同。
   - 重写 `apply_crawl_config_filters()`：`prune_urls` 保留；open（queued/started/paused，排除 deleting）不匹配行**批量 `request_delete()`**（started 计入 cancelled，queued/paused 计入 stopped），不再同步 `.delete()`；统计 `accepted_snapshots`（仍通过的 open 行）、`pending_delete_snapshots`（crawl 下 deleting 实际行数）；sealed 行完全不碰。
   - `exclude_domain()` 组装 denylist 后调用统一流程，返回 `domain/created/changed/removed_urls/accepted_snapshots/cancelled_snapshots/stopped_snapshots/pending_delete_snapshots/deleted_snapshots(=转换数，兼容旧键)`。
3. **Snapshot 盖代次与闸门**（依赖 1、2）：
   - `create_snapshots_from_urls()` 每行前刷新 config 与代次，新行盖戳；`create_discovered_snapshots()` 开头刷新 config+代次、构造 Snapshot 时盖戳；`add_url()` 写入前刷新 config+代次复核。
   - `accepted_under_current_url_filters()`：fresh crawl 行；代次相等直接 True；否则用最新规则判定，通过则 CAS 刷新 snapshot 代次并 True，不通过 False。
   - `reconcile_url_filter_generation(lock_seconds=60)`：仿 `reconcile_parent_lifecycle`，仅处理非 sealed/non-deleting 行；拒绝时先 claim 锁、二次确认后 `request_delete()` 并返回 True（跳过本轮）；通过则返回 None。
   - `schedule_plugin_run`：deleting 直接 no-op。
4. **runner/服务接入**（依赖 3）：
   - `_run_due_snapshot_locked()` 在 parent reconcile 后调用代次闸门。
   - `on_SnapshotEvent` 拥有尝试前做复核，拒绝即收敛且不登记 ownership（completion 投影随之自然丢弃）。
   - `finalize_completed_snapshot`：deleting 直接返回；拒绝则 request_delete 并返回（不写 downloaded_at/不 seal/不写 index）。
   - `project_discovered_snapshots`：deleting/拒绝 → 收敛 + 返回 []。
   - ArchiveResult 两个投影入口：deleting/拒绝 → 收敛 + 丢弃。
5. **admin/API 入口统一**（依赖 2、3）：
   - `CrawlAdminForm.save()`：先正常 `instance.save()` 落其它字段，再比较 DB 旧归一化规则与提交后规则；不同则调 `apply_url_filter_change()`；新 crawl 正常 save（初始代次 0）。结果挂到 form 上供 `save_model` message。
   - `exclude_domain_view` 返回完整计数 JSON。
   - API 两处 deleting 防护返回 409。
6. **测试与验证**（见下）。

## Dependencies and Considerations

- 不改任何过滤匹配语义（pattern 归一化、denylist 优先、空 allowlist 放行全部）、递归深度、ONLY_NEW、CRAWL_MAX_URLS/SIZE/TIMEOUT 判定；无并发/失败时行为与现在一致。
- started snapshot 的进程取消复用既有链路：request_delete 后 `Snapshot.delete_requested()`→`Snapshot.delete()` 对 RUNNING 的 archiveresult Process `kill_tree()`，杀不掉抛 OSError → 保留 deleting 重试；服务重启后 runner 继续消费。不新造文件租约/选举机制（遵守 AGENTS.md 并发契约）。
- 所有 HTTP 请求路径只做短 UPDATE，不做文件 IO/进程等待。
- CAS bump 用短 autocommit UPDATE，不开长事务；SQLite/Postgres 均兼容。
- 代次刷新（放宽后采纳）只在 snapshot 被实际领取/提交时发生，不做批量 sweep。
- 无关 Crawl 不受影响：代次与规则都是 Crawl 级；snapshot 自身 config 不含 URL 过滤键（仅 PERMISSIONS 等），复核仍传 snapshot 以兼容合并语义。
- `Crawl.delete()`（删整个 crawl，django admin action/API DELETE）走 CASCADE 硬删，语义不变。
- 无 mocks/monkeypatch：测试用真实 ORM、真实文件删除（`Snapshot.delete_requested()`）、真实 runner 函数；started 取消的端到端重进程场景沿用 `recursive_test_site` 风格仅在必要时添加。

## Validation

- `uv run pytest archivebox/tests/test_config_URL_filters.py archivebox/tests/test_config_ONLY_NEW.py archivebox/tests/test_config_MAX_limits.py archivebox/tests/test_ui_admin_crawl.py archivebox/tests/test_filter_generation.py -x`
- 相关回归：`test_recursive_crawl_1/2/3.py`、`test_crawl_runner*.py`、`test_runner_deletion.py`、`test_api_v1_core_snapshots.py`、`test_ui_admin_snapshot.py`、`test_cli_seed_urls.py`、`test_non_ascii_urls.py`
- `uv run prek run --all-files`（或至少变更文件的 lint/格式）
- 关键断言：
  - 仅有效修改 bump；表单与排除域名走同一方法；无变化不收敛。
  - queued/paused 被拒 → deleting 且不在活动调度查询内；started 被拒 → deleting，`delete_requested()` 真正杀进程/删文件/硬删；失败保留 deleting 可重试。
  - deleting snapshot 的迟到 ArchiveResult 事件/parser 发现零写入；sealed 行与其输出保留；放宽规则不自动重建已删行。
  - 响应含 accepted/cancelled/stopped/pending_delete 实际计数；crawl 计数与实际 DB 状态一致。

## Risks

- **运行中 CrawlRunner 与删除队列竞争**：锁内 claim + 二次确认、deleting 状态对所有调度 CAS 不可见、事件投影丢弃三重保证；删除队列本就为杀活进程设计（test_runner_deletion 已验证 OSError 重试）。
- **raw config JSON 直改过滤项**：表单保存时对归一化前后做 diff，同样进入统一流程，绕过不了代次。
- **历史 snapshot 代次为 0**：首次规则变更后在下一次领取/提交时按最新规则重新判定一次（sealed 除外），语义即“改规则后停止未完成工作”，符合需求；通过则无损继续。
- **响应字段扩展**：保留 `ok/domain/created/removed_urls/deleted_snapshots` 旧键，仅新增字段，兼容现有 UI/脚本。
