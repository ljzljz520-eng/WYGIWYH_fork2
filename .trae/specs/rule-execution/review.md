# RuleExecution 规则执行引擎 — 最终审查报告（review.md）

- 日期：2026-10-05
- 范围：以「交易事件 × 交易版本 × 规则版本」唯一标识的 RuleExecution；事务化规则引擎全链路（信号 → worker → 规划器 → 执行器 → 试运行 → 提交）
- 依据：[spec.md](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/.trae/specs/rule-execution/spec.md)（AC-1~AC-13）、[tasks.md](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/.trae/specs/rule-execution/tasks.md)（12 任务全部 completed）

## 1. 实现摘要

| 组件 | 文件 | 职责 |
|---|---|---|
| 单调版本 | [versions.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/common/functions/versions.py)、[transactions/models.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/transactions/models.py)、[rules/models.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/models.py) | Transaction / TransactionRule 各加正整数 version，save/update/bulk_update 单调递增，重入安全 |
| 执行记录 | [rules/models.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/models.py#L467) | RuleExecution（唯一键 rule_execution_unique_key）与 RuleActionExecution（逐动作状态/effects），*_ref 稳定列 + SET_NULL |
| 冻结上下文 | [evaluation.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/services/evaluation.py) | 冻结 evaluated_at/seed，datetime.now/date.today/random 全部可重放；签名令牌 |
| 只读规划器 | [planner.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/services/planner.py) | 三入口共用 build_plan；Effect/快照/指纹；兼容规则顺序、sequenced、继续执行 |
| 执行器 | [executor.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/services/executor.py) | 原子（单 savepoint 全回滚）/ 隔离（逐 savepoint）两种 execution_mode；upsert 单行 NOWAIT；失败可审计 |
| Worker | [jobs.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/jobs.py) | on_commit 出站事件消费；锁行、过期判定（out_of_order/newer_version/soft_deleted/row_missing/future_version）、claim 幂等 |
| 信号 | [signals.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/signals.py) | transaction.on_commit 后 defer；软/硬删标记；dca 同步保留 |
| 预览令牌 | [preview_token.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/services/preview_token.py) | 签名绑定上下文/事件/引用/版本/指纹/补丁，600s 过期 |
| 视图/模板 | [views.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/views.py)、[dry_run/](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/templates/rules/fragments/transaction_rule/dry_run) | 试运行展示 modify/create/reject 分组；新增提交端点与按钮 |
| 迁移 | rules 0019/0020/0021、transactions 0051/0052 | 版本字段、新模型、取消历史 todo 任务（procrastinate 3.x 枚举） |

旧实现 `apps/rules/tasks.py`（check_for_transaction_rules + DryRunResults）已删除，全仓无残留注册（TR-7.4）。

## 2. 独立只读审查与处置

委托独立 agent 在真实 PostgreSQL 上完成一次只读审查 + 实证（apps.rules 76 测试独立运行通过）。发现问题与处置：

| 级别 | 问题 | 处置 |
|---|---|---|
| Blocker | 原子失败后 trigger 内存对象脏、后续规则将回滚修改复活落库 | executor 失败路径 refresh_from_db；补 MultiRuleRollbackTests |
| Major | 缺「当前版本 < 事件版本」可重试分支 | jobs 补 RetryableEventError 分支；补 FutureVersionTests |
| Major | updated 提交未真正应用 input_patch | materialize_input_patch 物化后执行；补 updated 提交端到端测试 |
| Major | token 不绑定补丁/版本/引用，指纹比对可被自洽篡改 | 新增签名预览令牌，提交只信任令牌 |
| Major | 永久删除误标软删、Model.delete 无信号 | 显式 hard_delete=True，补硬删信号与两条测试 |
| Minor | stale 路径无日志 | 补 info 日志（事件键 + 原因） |

审查报告中的 Minor 残余项（B-5 跨规则 out_of_order 语义属当前设计、B-6 行锁跨规则持有、B-7 无变更也 bump version 与旧实现兼容、B-8 搜索/加锁窗口符合 FR-7）经评估可接受，均有注释或测试覆盖。审查中「isinstance(str) 死代码」一条经历史实证（message_dict 可能为纯字符串）不成立，保留。

## 3. 最终验收准则映射

| AC | 结论 | 关键证据 |
|---|---|---|
| AC-1 单规则失败全回滚 + 审计 | 满足 | AC1RollbackTests；executor savepoint + detail 版本/动作 |
| AC-2 重复投递只产生一个执行 | 满足 | AC2DuplicateDeliveryTests（同 id、派生 1）、test_worker |
| AC-3 乱序/并发冲突判定 | 满足 | AC3OutOfOrderTests、test_upsert_locking（真实线程 PG）、FutureVersionTests |
| AC-4 试运行与提交一致 | 满足 | created 与 updated 两条端到端用例（指纹逐项 + 补丁物化 + 真实 id） |
| AC-5 on_commit 入队 | 满足 | SignalEnqueueTests（提交 1/回滚 0） |
| AC-6 冻结上下文一致性 | 满足 | test_frozen_evaluation（同令牌一致/异种子分叉/篡改拒绝） |
| AC-7 删除与过期场景 | 满足 | 四类 stale + 硬删执行 + 永久删除 hard 标记测试 |
| AC-8 隔离模式 | 满足 | test_executor（逐 savepoint、trigger 恢复、completed） |
| AC-9 动作记录与来源 | 满足 | test_executor、generated_by_action_execution_id 反查 |
| AC-10 历史任务迁移 | 满足 | 0021 SQL 测试、replay 幂等测试 |
| AC-11 顺序与继续执行 | 满足 | 多规则 worker 测试 + MultiRuleRollbackTests |
| AC-12 单一规划器（≥4） | 5/5 | 三入口共用 build_plan，Effect/指纹模型唯一 |
| AC-13 并发/迁移质量（≥4） | 5/5 | 窄锁 NOWAIT、冲突转换、迁移链有序 |

## 4. 测试结果（最终）

- `apps.rules`：**81 个全部通过**（真实 PostgreSQL 15，docker wygiwyh-test-pg:55432）
- `apps.api`：**109 个全部通过**
- `apps.transactions`：51 个中 21 个失败（1 failure + 20 errors），均为 `DjangoViteAssetNotFoundError`（main.js manifest 缺失），在未改动的干净代码树同样复现，与本次变更无关
- `manage.py check`：仅 2 个预存前端资产警告

## 5. 残留风险与说明

1. 前端构建产物缺失（manifest/build 目录），试运行快照采用纯 Django 模板渲染（transaction_snapshot.html）；当前代码树无 c-transaction.item 组件定义。
2. executor 末尾对 trigger 无条件 save 并 bump version（兼容旧语义，已在代码注释/tasks.md 记录）。
3. upsert 只读搜索与加锁之间的窗口：目标硬删导致规则 failed、目标变化按 FR-7 last-writer-wins，规格明确接受。
4. 未创建 git commit（遵循要求）。
