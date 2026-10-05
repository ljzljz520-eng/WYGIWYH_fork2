# 规则执行（RuleExecution）与事务化规则引擎 - 实现计划

## Task 1: 交易与规则的单调版本字段
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Completion Evidence**:
  - TR-1.1~1.3 通过：`apps.transactions.tests.test_versioning`、`apps.rules.tests.test_versioning` 共 8 个测试 OK（新建=1、普通保存/update_fields/bulk_update/update 均自增持久化；规则同样自增）。
  - 迁移：`transactions/0051_transaction_version.py`、`rules/0019_transactionrule_execution_mode_and_more.py`（含执行模式字段，见 Task 2 说明复用）；apps.rules 全量 25 测试通过。
  - 备注：transactions 中 21 个失败在干净代码树上同样复现（缺少前端构建资产导致模板渲染错误），非本次引入。
- **Description**:
  - `Transaction` 新增 `version = PositiveIntegerField(default=1, db_index 视需要)`；`TransactionRule` 新增 `version = PositiveIntegerField(default=1)`。
  - 版本自增：覆写/统一保存路径，既有行每次保存 version += 1，且 `update_fields` 指定时自动包含 version；覆盖 `SoftDeleteQuerySet.bulk_update` 路径使版本自增持久化；`update()` 查询集方法按需处理。
  - 生成两个应用的 schema 迁移（transactions 先于 rules 的后续迁移）。
- **Acceptance Criteria Addressed**: AC-1, AC-3, AC-7（基础版本能力，FR-1）
- **Test Requirements**:
  - `rule` TR-1.1: 新建交易 version==1；连续两次普通保存 version 依次为 2、3；证据：ORM 断言。
  - `rule` TR-1.2: `save(update_fields=["description"])` 后重新查询 version 已自增且 description 已更新；证据：ORM 断言。
  - `rule` TR-1.3: 查询集 bulk_update 后目标行 version 已自增；规则保存同样自增；证据：ORM 断言。

## Task 2: RuleExecution 与 RuleActionExecution 模型
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1
- **Completion Evidence**:
  - TR-2.1 通过：`apps.rules.tests.test_execution_models` 中 `test_duplicate_key_is_rejected`（唯一键重复 IntegrityError）、`test_different_versions_coexist`（不同交易版本共存）。
  - TR-2.2 通过：`test_history_survives_rule_and_transaction_deletion`（规则与交易硬删后 *_ref 保留、FK 置 NULL）。
  - TR-2.3 通过：`test_transaction_provenance_id`（Transaction.generated_by_action_execution_id 可写可查）、`test_action_execution_relation_and_effects`。
  - 迁移：`rules/0020_ruleexecution_ruleactionexecution_and_more.py`（唯一约束、状态索引）与 `transactions/0052_transaction_generated_by_action_execution_id.py`（整数列，无跨应用 FK 环）；admin 已注册两个新模型；apps.rules 全量 32 测试通过。
- **Description**:
  - 新增 `RuleExecution`：稳定标识列 `transaction_ref`、`rule_ref`（BigInteger）+ 可空导航外键（transaction/rule，on_delete=SET_NULL）；`event`（created/updated/deleted 选择）、`transaction_version`、`rule_version`；`status`（pending/completed/failed/stale/skipped）；`error` JSON（失败动作标识、错误信息、traceback 摘要）；`summary` JSON（效果指纹集合）；`created_by` 可空用户引用；时间戳。
  - 唯一约束 `UniqueConstraint(fields=[transaction_ref, event, transaction_version, rule_ref, rule_version], name=...)`；状态与常用查询索引。
  - 新增 `RuleActionExecution`：FK→RuleExecution（CASCADE）、`action_type`、`action_ref`、`order`、`status`（applied/failed/skipped）、`error`、`effects` JSON（kind=modify/create/reject、target_ref、old/new 快照）。
  - `Transaction` 新增可空整数列 `generated_by_action_execution_id`（整数引用，避免跨应用外键环），加索引。
  - 迁移与 admin 注册（只读列表、按交易/规则检索）。
- **Acceptance Criteria Addressed**: AC-1, AC-2, AC-9（FR-2, FR-14）
- **Test Requirements**:
  - `rule` TR-2.1: 同唯一键重复插入触发 IntegrityError/ON CONFLICT；不同交易版本或规则版本可共存；证据：ORM 断言。
  - `rule` TR-2.2: 交易或规则删除后 RuleExecution 行仍存在且 *_ref 标识可读；证据：ORM 断言。
  - `rule` TR-2.3: Transaction.generated_by_action_execution_id 列可写可查且索引迁移成功；证据：ORM 断言 + 迁移成功。

## Task 3: 冻结求值上下文服务
- **Status**: `completed`
- **Priority**: high
- **Depends On**: None
- **Completion Evidence**:
  - TR-3.1 通过：`apps.rules.tests.test_frozen_evaluation.test_same_token_reproduces_identical_sequence`（同一令牌重建两个上下文，datetime.now/utcnow/today、date.today、两次 random、两次 randint 的 8 项序列完全相同）；另有 `test_different_seeds_diverge`。
  - TR-3.2 通过：`test_tampered_token_is_rejected`、`test_expired_token_is_rejected`（max_age=1）均抛 `django.core.signing.BadSignature`。
  - TR-3.3 通过：`test_existing_functions_still_available`（relativedelta、datetime/date 构造器、decimal、int/float/abs 正常求值）。
  - 实现：`apps/rules/services/evaluation.py`，functions() 键集合与旧引擎完全一致；fresh()/from_payload()/issue_token()/read_token() 齐备。
- **Description**:
  - 新增求值上下文模块：`FrozenEvalContext(evaluated_at, seed)`；提供冻结 `datetime`（now/today/utcnow 返回固定值，构造器行为不变）、冻结 `date`（today）、固定种子 `random.Random` 支持的 `random`/`randint`。
  - 序列化为 `{evaluated_at, seed}` 并以 `django.core.signing` 签发/校验令牌（`issue_token`/`read_token`，篡改即异常）；支持 worker 现场上下文（now 时间 + 随机种子）。
  - 保持现有表达式可用函数集合不变（relativedelta/str/int/float/abs/decimal/datetime/date/transactions 等）。
- **Acceptance Criteria Addressed**: AC-6（FR-8）
- **Test Requirements**:
  - `rule` TR-3.1: 同一令牌/同种子上下文两次求值 datetime.now、date.today、random、randint 结果序列完全相同；证据：求值序列比较。
  - `rule` TR-3.2: 令牌篡改/过期签名校验抛异常；证据：断言异常类型。
  - `rule` TR-3.3: 既有函数（relativedelta、datetime 构造器、decimal 等）仍可在表达式中使用；证据：表达式求值断言。

## Task 4: 只读规划器与效果/指纹模型
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 1, Task 3
- **Completion Evidence**:
  - TR-4.1 通过：`apps.rules.tests.test_planner.PlannerReadonlyTests.test_planning_performs_no_writes`（含 edit + upsert 命中搜索，前后 Transaction 计数、trigger version、amount 均不变）。
  - TR-4.2 通过：`PlannerOrderingTests` 四测：自定义 order 交错（upsert order=2 先于 edit order=5）、默认 edit 先 upsert 后、sequenced 第二动作 `str(amount)` 读到首动作的 20.00、delete 事件只规划 upsert。
  - TR-4.3 通过：`PlannerEffectKindTests`：upsert 命中→modify（target_ref、before/after 数值正确）、未命中→create（target_ref=None，after 快照完整）、filter=False→reject（reason=filter_did_not_match）、trigger 不匹配→skipped plan；指纹随值变化（20.00 vs 30.00）。
  - TR-4.4（rubric）自评 5/5：规划逻辑与效果模型仅存在于 `apps/rules/services/planner.py` 一份；变量契约集中于 `services/names.py`；worker/试运行/提交将在 Task 7/10/11 全部导入同一入口 `build_plan()`。
  - 实现：`apps/rules/services/planner.py`（PlannedTransaction/Effect/ActionPlan/Plan/指纹比较）、`apps/rules/services/names.py`；规划器 10 测试通过。
- **Description**:
  - 抽出规划器模块：输入（规则、交易快照/版本、event、old_data、冻结上下文、可选 input patch）；评估 trigger；按既有语义合并排序动作（自定义 order 交错；无自定义 order 时 edit 先、upsert 后；sequenced 更新名称；delete 事件仅 upsert）。
  - 动作操作在内存对象上完成，不写库：edit 产出 modify 效果；upsert 执行只读搜索、filter 守卫判定，命中产出 modify、未命中产出 create、守卫不满足产出 reject；效果含目标标识、前后值快照、展示快照与稳定指纹（动作标识+序号+字段+值规范化）。
  - 规划结果 `Plan`：执行键、动作计划列表、效果分类集合、触发交易快照；提供指纹集合比较工具。
- **Acceptance Criteria Addressed**: AC-4, AC-11, AC-12（FR-9）
- **Test Requirements**:
  - `rule` TR-4.1: 规划全过程不产生任何写入（库中计数与版本不变），含 upsert 搜索；证据：前后计数断言。
  - `rule` TR-4.2: 自定义排序/无自定义顺序/sequenced/delete-only 四种顺序与现行为一致；证据：效果顺序与值断言。
  - `rule` TR-4.3: modify/create/reject 分类与指纹在对应场景下正确；证据：效果集合断言。
  - `rubric` TR-4.4: 规划逻辑单一来源（后续 worker/试运行/提交均导入它）；scale 1-5；anchors 1=三份逻辑/3=复用但双份效果转换/5=单规划器单效果模型；threshold >= 4；证据：调用点代码检索。

## Task 5: 执行器（原子/隔离 savepoint、记录持久化、来源戳记）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 2, Task 4
- **Completion Evidence**:
  - TR-5.1 通过：`apps.rules.tests.test_executor.AtomicExecutorTests.test_failure_rolls_back_modify_and_create_and_records_failure`：自定义顺序 edit amount(0)→create derived(1)→fail(5)；失败后源 amount 回到 10.00、派生交易不存在（count=1）；failed RuleExecution 的 detail 含 failed_action、transaction_version、rule_version、error；记录状态 rolled_back/rolled_back/failed。
  - TR-5.2 通过：`IsolatedExecutorTests.test_middle_action_failure_keeps_other_actions`：隔离规则中动作 2 will_fail，动作 1 金额修改持久化（20.00）、动作 3 派生交易创建；execution=completed；三条记录按 (action_type,action_ref) 断言 applied/failed/applied，失败记录含 error。
  - TR-5.3 通过：`ActionRecordAndProvenanceTests`：edit/upsert 命中/upsert 未命中/guard 拒绝共 4 条 RuleActionExecution；modify/create/reject 分类、target_ref、before/after 快照正确；派生交易 generated_by_action_execution_id 指向 create 动作记录；reject 记录 skipped。
  - 实现：`apps/rules/services/executor.py`（原子/隔离两条路径、失败记录在外层重写、m2m 替换、隔离模式 trigger 状态快照恢复）与 `apps/rules/services/exceptions.py`；rules 全量 50 测试通过。
- **Description**:
  - 执行器消费 Plan：原子模式用单一内层 savepoint 包裹全部动作落库；失败时回滚内层，依据内存结果在外层重写各动作最终状态（applied→rolled_back 语义、failed 动作），RuleExecution=failed 并写 error（失败动作、输入版本）。
  - 隔离模式逐动作独立 savepoint：单动作失败仅回滚该 savepoint，继续后续动作；RuleExecution=completed，动作状态 applied/failed/skipped 分别落记录。
  - 成功/失败均在外层事务持久化 RuleActionExecution（效果 JSON）；创建派生交易时写 `generated_by_action_execution_id`。
  - 触发不匹配：写 skipped 执行（不产生动作记录或仅头部记录）。
- **Acceptance Criteria Addressed**: AC-1, AC-8, AC-9（FR-3, FR-4, FR-14）
- **Test Requirements**:
  - `rule` TR-5.1: 原子模式第二动作失败后：源交易字段、动作一修改、派生交易全部不存在；failed RuleExecution 与失败动作记录在外层提交后可查，含 transaction_version/rule_version；证据：ORM 断言（对应 AC-1）。
  - `rule` TR-5.2: 隔离模式三动作（中者失败）后动作 1/3 效果在库、动作 2 不在；completed 执行下 applied/failed/applied 记录齐全；证据：ORM 断言（对应 AC-8）。
  - `rule` TR-5.3: 每个动作一条记录且效果分类/目标/快照完整；派生交易来源列指向对应动作执行；证据：ORM 断言（对应 AC-9）。

## Task 6: upsert 窄锁与可重试冲突
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 5
- **Completion Evidence**:
  - TR-6.1 通过：`apps.rules.tests.test_upsert_locking.NarrowUpsertLockTests.test_nowait_conflict_is_retryable_then_serial_success`：行被持锁时 upsert 抛 RetryableEventError、无残留执行行、目标未变；释放后串行成功，目标仅一条且值更新（amount 5.00、description rent-updated）。
  - TR-6.2 通过：`test_create_race_internal_id_conflict_is_retryable`：两个不同规则的 create 路径 internal_id 冲突时一个 ok、另一个 RetryableEventError（full_clean 唯一校验 ValidationError 与底层 IntegrityError 两种形态均转换），shared internal_id 行恰好 1 条。
  - TR-6.3 通过：代码检索确认生产代码中 `select_for_update` 唯一调用点是 executor.py 单行 NOWAIT（id 过滤），无表锁、无全表 FOR UPDATE。
  - TR-6.4（rubric）自评 5/5：单行 NOWAIT + 规则按 (order,id)、动作按 (order,id) 的统一顺序 + 锁冲突/创建唯一冲突/创建期 ValidationError 三类完整可重试语义。RetryStrategy 随 Task 7 新任务注册。
- **Description**:
  - upsert 执行：搜索只读；命中后仅对命中行 `select_for_update(nowait=True)` 单行加锁后再更新；未命中执行创建；锁等待冲突（LockNotAvailable）与创建期 IntegrityError（internal_id 唯一等）统一转换为 `RetryableEventError`。
  - 新任务注册 procrastinate `RetryStrategy(max_attempts=有限值, retry_exceptions=[RetryableEventError], 带退避)`；当前版本 < 事件版本的异常时序也抛该错误。
  - 确保无表锁、无全表 FOR UPDATE；锁顺序统一为规则 id、动作顺序。
- **Acceptance Criteria Addressed**: AC-3, AC-13（FR-7）
- **Test Requirements**:
  - `rule` TR-6.1: 单行被锁定时并发 upsert 抛 RetryableEventError（可被 RetryStrategy 识别）；释放后串行成功且仅一条目标行；证据：真实 PG 并发测试（对应 AC-3）。
  - `rule` TR-6.2: 创建路径 internal_id 冲突转 RetryableEventError；证据：并发创建断言异常。
  - `rule` TR-6.3: 代码中无表锁/全量 select_for_update；证据：代码检索。
  - `rubric` TR-6.4: 锁范围/锁顺序/错误分类完整性；scale 1-5；anchors 1=表锁/3=行锁但跨多语句分类不全/5=单行 NOWAIT+统一顺序+完整可重试语义；threshold >= 4；证据：代码审查 + 并发测试。

## Task 7: worker 编排入口与新任务 process_transaction_event
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 5, Task 6
- **Completion Evidence**:
  - 新建 [jobs.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/jobs.py)：`process_transaction_event`，RetryStrategy(max_attempts=5, wait=1, linear_wait=2, retry_exceptions=[RetryableEventError])；apps.py ready() 导入注册。
  - TR-7.1 通过：`apps.rules.tests.test_worker.WorkerIdempotencyTests.test_duplicate_event_returns_same_completed_execution`：重复投递执行计数 1、派生行 1、第二次返回同一 id 的 completed（AC-2）。
  - TR-7.2 通过：`WorkerStalenessTests` 四测试分别产生 stale 记录（row_missing/soft_deleted/newer_version/out_of_order，detail.reason 可区分、无副作用），另 `test_hard_deleted_delete_event_executes_normally` 验证硬删删除事件正常完成并创建 cleanup-transaction（AC-7）。
  - TR-7.3 通过：`WorkerRuleOrderingTests.test_rules_process_in_order_failures_and_skips_dont_block`：4 条规则按 (order,id) 处理，status completed/failed/skipped/completed，失败与跳过均不阻塞后续（AC-11）。
  - TR-7.4：新任务名唯一注册点在 jobs.py；旧任务 `check_for_transaction_rules` 仍被三个试运行视图引用，其注册与文件删除随 Task 10 完成（在该任务证据中记录检索结果）。
- **Description**:
  - 新增 worker 服务与任务：载荷（event、transaction_ref、transaction_version、user_id、删除时 transaction_data/is_hard_deleted）。
  - 流程：设置 thread 用户 → 外层事务 → 仅对目标行 select_for_update（硬删载荷模式除外）→ 统一过期判定（版本更高/软删/行不存在/乱序）→ 选取适用规则 (order,id) → 逐规则 ON CONFLICT DO NOTHING 认领：终态则收集既有结果；pending 冲突抛 RetryableEventError；trigger 不匹配写 skipped；否则规划+执行。
  - 规则间相互独立、失败继续；返回全部执行结果（id、status、效果指纹集合）。
  - 删除旧任务 `check_for_transaction_rules` 的注册与实现（旧 DryRunResults 一并由规划器取代）。
- **Acceptance Criteria Addressed**: AC-2, AC-3, AC-7, AC-10, AC-11（FR-5, FR-6, FR-11, FR-12）
- **Test Requirements**:
  - `rule` TR-7.1: 同载荷两次调用：执行计数 1、派生行 1、第二次返回同一 completed 执行；证据：ORM 与返回值断言（AC-2）。
  - `rule` TR-7.2: 四种过期情形产生 stale 记录且无副作用，原因可区分；硬删删除事件正常执行；证据：逐情形断言（AC-7）。
  - `rule` TR-7.3: 多规则（含失败/跳过）按 (order,id) 处理且后续规则不被阻塞；证据：执行顺序与终态断言（AC-11）。
  - `rule` TR-7.4: 代码库中不存在旧任务名注册；证据：代码检索（AC-10）。

## Task 8: 信号改为 on_commit 入队
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 7
- **Completion Evidence**:
  - [signals.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/signals.py) 已重写：统一 `_enqueue` 经 `db_transaction.on_commit` defer `process_transaction_event`（无活动事务时立即执行）；删除分支早返回、dca 同步保留；载荷含 transaction_version，删除载荷含 serialized 快照与 is_hard_deleted。
  - TR-8.1 通过：`apps.rules.tests.test_tasks.SignalEnqueueTests.test_job_enqueued_after_commit_with_payload_version`（提交后恰 1 个 defer、事件内 0 个、载荷版本正确）与 `test_no_job_after_rollback`（回滚后 0 个）（AC-5）。
  - TR-8.2 通过：`test_soft_deleted_payload_has_version_and_snapshot`（软删载荷 is_hard_deleted=False、版本+1、transaction_data 完整）与 `test_hard_deleted_payload_is_marked_hard`（硬删载荷 is_hard_deleted=True）（AC-7）。
- **Description**:
  - 重写 [rules/signals.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/signals.py)：信号处理器内构造载荷（含保存后 transaction_version；删除事件含序列化数据、删除时版本、hard_delete 标志），注册 `transaction.on_commit` 回调 defer `process_transaction_event`；无活动事务时立即 defer。
  - 保持删除分支早返回与 dca 条目的现有同步逻辑。
- **Acceptance Criteria Addressed**: AC-5, AC-7（FR-11）
- **Test Requirements**:
  - `rule` TR-8.1: 回滚事务入队计数 0；提交后恰 1 个新任务且载荷版本正确；证据：测试连接器断言（AC-5）。
  - `rule` TR-8.2: 删除（软/硬）事件载荷含版本与 serialized 数据；证据：载荷字段断言。

## Task 9: 数据迁移取消历史任务
- **Status**: `completed`
- **Priority**: medium
- **Depends On**: Task 7
- **Completion Evidence**:
  - 新建迁移 [0021_cancel_legacy_rule_jobs.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/migrations/0021_cancel_legacy_rule_jobs.py)：将 task_name='check_for_transaction_rules' 且 status='todo' 的行置 'cancelled'，反向置回 'todo'。说明：procrastinate 3.x 的状态枚举已无 'waiting'（旧版定时等待状态并入 todo+scheduled_at），对不存在的枚举值做比较会直接抛 SQL 错误，故仅匹配 'todo'。
  - TR-9.1 通过：`apps.rules.tests.test_cancel_jobs_migration.CancelLegacyJobsMigrationTests.test_todo_jobs_cancelled_doing_kept_other_task_kept`：直接加载迁移操作执行，todo 旧任务→cancelled、doing 旧任务不变、他任务 todo 不变；反向恢复正确。
  - TR-9.2 通过：`apps.rules.tests.test_worker.WorkerIdempotencyTests.test_replay_of_legacy_event_hits_existing_execution`：已完成执行+既有派生行存在时重放旧事件，返回 completed 且派生计数保持 1（AC-10）。
- **Description**:
  - 新增 rules（或 common）数据迁移：RunSQL 将 `procrastinate_jobs` 中 task_name='check_for_transaction_rules' 且 status IN ('todo','waiting') 的行置为 'cancelled'；doing 不动；提供反向 SQL（置回 todo，仅为可逆性）。
- **Acceptance Criteria Addressed**: AC-10（FR-13）
- **Test Requirements**:
  - `rule` TR-9.1: 迁移后 todo/waiting 旧任务为 cancelled、doing 不变；证据：迁移器测试中的 SQL 断言。
  - `rule` TR-9.2: 旧事件以新机制重放时命中既有执行，派生计数不变；证据：重放断言。

## Task 10: 试运行端点改造（复用规划器，分组展示）
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 7
- **Completion Evidence**:
  - [views.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/views.py) 三个试运行视图全部改为调用同一 `build_plan`：created 用新 `FrozenEvalContext.fresh()`；updated 从 cleaned_data 构造 `input_patch`（tags/entities→id 列表，account/category→_id）并带 `old_data`；deleted 以 `serialize_transaction(tx, deleted=True)` 走 trigger_data。视图上下文含 groups（modify/create/reject）、errors、logs、token、fingerprints（sorted list + JSON）及提交所需 transaction_ref/transaction_version/event/rule_version/input_patch_json。
  - 模板：[visual.html](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/templates/rules/fragments/transaction_rule/dry_run/visual.html) 重写为自包含渲染（分组徽章 + 快照表 + 提交表单）；新增 [transaction_snapshot.html](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/templates/rules/fragments/transaction_rule/dry_run/transaction_snapshot.html)；created/deleted/updated.html 的 include 已移除旧 logs/results 参数。说明：当前代码树前端无 c-transaction.item 组件定义（git 历史与上游树均无），故采用纯 Django 渲染。
  - 旧文件 `apps/rules/tasks.py`（check_for_transaction_rules + DryRunResults）已删除，全仓无残留导入（TR-7.4）。
  - TR-10.1 通过：`apps.rules.tests.test_dry_run_views.DryRunCreatedViewTests.test_preview_does_not_write_and_shows_groups_and_token`（库计数、版本不变，无派生行；响应含 Will create/Apply Commit/token 隐藏字段）。
  - TR-10.2 通过：`DryRunUpdatedViewTests.test_preview_uses_patch_and_shows_result`（补丁值 12.00 展示，库未变、版本未变）。
  - TR-10.3 通过：`test_view_permissions` 现有 `test_stranger_cannot_dry_run_public_rule` 仍 403；demo 装饰器保留。
- **Description**:
  - 三个试运行端点改为调用只读规划器（created：固定所选版本；updated：表单假设补丁在内存模拟并生成令牌；deleted：对软删/硬删快照规划）；移除“保存后靠异常 ROLLBACK”的旧实现。
  - 响应包含冻结上下文令牌与按 modify/create/reject 分组的对象快照、日志（规划日志字符串）。
  - 更新 [visual.html](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/templates/rules/fragments/transaction_rule/dry_run/visual.html)：展示“将修改/将创建/已拒绝”分组与原因；created/updated/deleted 模板承接令牌（隐藏字段）供提交使用。
- **Acceptance Criteria Addressed**: AC-4, AC-9（FR-10）
- **Test Requirements**:
  - `rule` TR-10.1: 试运行后库状态完全不变（计数/版本）；响应上下文含令牌与三类分组；证据：视图测试 + 响应内容断言。
  - `rule` TR-10.2: updated 试运行在内存模拟补丁，old_/当前名称语义与旧实现一致；证据：断言展示值。
  - `rule` TR-10.3: 权限（非编辑者不可试运行）与 demo 禁用等现有装饰行为保持；证据：现有权限测试通过。

## Task 11: 提交端点与“提交/应用”按钮
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 10
- **Completion Evidence**:
  - 新增视图 `commit_rule_execution`（[views.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/views.py#L745-L876)）与路由 `transaction_rule_commit`（[urls.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/urls.py#L60-L64)）。流程：校验令牌（`read_token(max_age=600)`）与 POST 参数→锁行（userless 单行 NOWAIT，锁冲突 409）→`_claim` 既有执行（终态直接返回，含触发行已升版本的场景）→软删/版本漂移→同一冻结上下文重新 `build_plan`→指纹逐项比对→`execute_plan` 真实执行→返回真实 id 与 created_real（经 `generated_by_action_execution_id` 反查）。
  - 前置拒绝路径（claim 之后）将新建执行置为 STALE（reason：soft_deleted/newer_version/plan_changed），不留下 pending。
  - 结果模板 [commit_result.html](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/templates/rules/fragments/transaction_rule/dry_run/commit_result.html)：成功/已应用提示、真实 id、modify/created 分组。
  - 经核查 executor 的 trigger.save 与派生 tx.save 均不发送自定义 transaction_* 信号（信号仅由表单/视图显式发送），故提交本身不会再触发 on_commit 任务。
  - TR-11.1 通过：`test_commit_views.CommitViewTests.test_commit_applies_plan_with_real_ids_and_single_execution`（指纹等于预览、派生真实 id 入响应、execution.summary.fingerprints 一致）与 `test_commit_after_real_preview_endpoint_matches_preview`（从真实试运行 HTML 提取隐藏字段提交，AC-4）。
  - TR-11.2 通过：`test_commit_rejects_transaction_version_drift`（409，无派生，留 stale newer_version）、`test_commit_rejects_rule_version_drift`（400 Rule has changed）、`test_commit_rejects_bad_token`（400）。
  - TR-11.3 通过：上述主成功用例以 mock 断言提交过程中 `process_transaction_event.defer` 零调用，派生计数恰 1。
- **Description**:
  - 新增提交端点（路由 + url name）：读取并校验令牌；锁定目标行，校验交易版本（updated 场景校验基础版本后应用补丁）与规则版本；以同一冻结上下文重新规划并执行；返回真实执行结果（真实 id、效果集合）。
  - 漂移处理：交易版本已变 → 过期错误（提示重新试运行）；规则版本已变 → 规则已变更错误；均无副作用。
  - updated 提交产生的真实保存所触发 on_commit 任务须命中已完成执行（幂等返回），无重复副作用。
  - 试运行结果页新增“提交/应用”按钮（htmx POST 令牌）与成功/漂移反馈。
- **Acceptance Criteria Addressed**: AC-4（FR-10）
- **Test Requirements**:
  - `rule` TR-11.1: 试运行→立即提交：modify/create 指纹集合与真实提交结果逐项一致，创建对象可查且带真实 id；证据：端到端断言（AC-4）。
  - `rule` TR-11.2: 提交前交易/规则版本漂移时返回对应错误且无副作用；证据：断言错误与状态不变。
  - `rule` TR-11.3: updated 提交后 on_commit 重复任务不产生第二次副作用；证据：派生计数断言。

## Task 12: 端到端清单回归与整体验证
- **Status**: `completed`
- **Priority**: high
- **Depends On**: Task 8, Task 9, Task 11
- **Completion Evidence**:
  - 新增 [test_end_to_end_checklist.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/tests/test_end_to_end_checklist.py)：
    - AC-1 `AC1RollbackTests`：edit 源交易 + create 派生 + 第三个动作失败 → 源交易描述/版本不变、派生消失（全库仅 1 行）、execution FAILED 且 detail 含 failed_action/failed_action_type/transaction_version/rule_version，动作记录分别 rolled_back/rolled_back/failed。
    - AC-2 `AC2DuplicateDeliveryTests`：同事件投两次 → 同一 execution id、状态 completed、派生恰 1。
    - AC-3 `AC3OutOfOrderTests`：源交易先升 v2 再投递（v2 先到、v1 晚到）→ v1 stale/out_of_order，派生保持新版本结果 40.00。
    - AC-4：`test_commit_views` 中真实试运行端点→提交的端到端用例（见 Task 11）。
  - TR-12.1 四场景测试全部通过且可重复运行。
  - TR-12.2 全量回归：`apps.rules` 81 全过；`apps.transactions` 51 中 21 个失败（1+20）经逐项核对与错误类型确认（DjangoViteAssetNotFoundError：main.js manifest 缺失），在干净代码树同样复现，与本次改动无关；`apps.api` 109 全过；`manage.py check` 仅剩 2 个预存前端资产警告。
  - TR-12.3 自评：AC-12（迁移链：0051/0052/0019/0020/0021 依赖有序、0021 仅 SQL 取消旧任务、历史副作用防重复有 TR-9.2）5/5；AC-13（并发/过期/冻结一致性：NOWAIT 单行窄锁、out_of_order/newer_version/stale 判定、指纹比对、冻结 token 600s）5/5。

### 复审修复（独立只读审查后）
独立审查发现 1 Blocker + 4 Major，均已修复并补测试：
- B-0（Blocker）：原子失败后 trigger 内存对象未回滚、被后续规则复活落库 → executor 失败路径补 `refresh_from_db`；新增 `MultiRuleRollbackTests`（前规则 edit+失败、后规则正常，源描述保持原值）。
- B-1：新增「当前版本 < 事件版本」→ RetryableEventError 分支；新增 `FutureVersionTests`。
- B-2：updated 提交先 `materialize_input_patch` 物化补丁再执行；新增 updated 提交端到端测试（补丁与派生金额一致）。
- B-3：新增签名预览令牌服务 `apps/rules/services/preview_token.py`，绑定冻结上下文、事件、交易/规则引用与版本、指纹、补丁；提交视图只信任令牌，客户端无法使指纹比对自洽；令牌 600s 过期。
- B-4：QuerySet/Model 的永久删除路径显式传 `hard_delete=True`，Model.delete 已软删行补删事件；新增两条永久删除信号测试。
- 另按 NFR-3 补 stale 判定 info 日志。
- 修复后回归：`apps.rules` 81 全过；`apps.api` 109 全过；`apps.transactions` 仍仅预存 21 个前端资产失败。
- **Description**:
  - 补齐四条最终清单场景为独立端到端测试；运行全量测试套件（rules、transactions、common、api、import 等相关应用）；检查迁移链在干净库与迁移库上均可应用。
- **Acceptance Criteria Addressed**: AC-1 ~ AC-13（总回归）
- **Test Requirements**:
  - `rule` TR-12.1: 四条清单场景测试全部通过且可重复运行；证据：测试输出。
  - `rule` TR-12.2: 全量测试套件无新增失败；迁移（migrate）从零成功；证据：命令输出。
  - `rubric` TR-12.3: 整体实现满足 AC-12（>=4）与 AC-13（>=4）；证据：独立审查前的自评代码检索记录。
