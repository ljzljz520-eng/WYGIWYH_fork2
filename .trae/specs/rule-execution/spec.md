# 规则执行（RuleExecution）与事务化规则引擎 - 产品需求文档

## Overview
- **Summary**: 为交易规则系统引入以「交易事件、交易版本、规则版本」唯一标识的 `RuleExecution`（及每动作的 `RuleActionExecution`）。信号在 `transaction.on_commit` 时把携带版本的事件入队；工作任务锁定目标交易，使用与界面试运行完全相同的规划器生成动作计划并执行。单条规则默认在一个事务内原子应用全部动作，也可通过规则上的执行模式开关显式选择「规则级隔离」（动作逐条独立提交、失败不牵连其他动作）。每个动作记录幂等结果与来源关系；重复事件直接返回既有执行结果；试运行展示即将修改、创建或拒绝的对象，并支持携带冻结上下文令牌的真实提交。
- **Purpose**: 解决现有规则在并发编辑、删除、队列重复投递与异步乱序到达下副作用重复、结果互相覆盖、预览与真实执行不一致的问题；为规则执行提供可审计、可追溯、可重放的记录。
- **Target Users**: 配置交易规则的最终用户（依赖规则正确执行）；运维与开发者（需要执行历史、过期判定与安全迁移）。

## Goals
- 以（交易, 事件类型, 交易版本, 规则, 规则版本）为幂等键，事件重复投递不产生重复副作用。
- 规则默认原子应用全部动作：任一动作失败时该规则全部动作回滚，并留下包含失败动作与输入版本的执行记录。
- 支持显式「规则级隔离」执行模式，兼容历史的逐动作尽力继续行为。
- 在并发编辑、软/硬删除、乱序到达时判定事件是否过期，且 upsert 的行锁范围最小。
- 试运行复用同一规划器，冻结求值上下文（时间、随机源），保证试运行展示集合与真实提交结果一致。
- 保持现有规则顺序（order, id）与跨规则继续执行行为。
- 迁移后历史队列任务不重复产生副作用。

## Non-Goals
- 不引入持久化 OutboundEvent 出站表与独立 relay（经确认采用 `on_commit` 直接入队 procrastinate）。
- 不做规则定义内容的历史版本快照存储；`rule_version` 仅为单调版本号，执行细节以当次 `RuleExecution`/`RuleActionExecution` 记录为准。
- 不改变规则表达式语言（simpleeval）的能力边界与可用字段集合。
- 不提供跨用户/跨账户批量补跑历史规则的管理工具。
- 不改造 API（DRF）层的规则资源接口。

## Background & Context
- 现状：[rules/tasks.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/tasks.py) 中的 `check_for_transaction_rules` 既是 worker 任务又是试运行实现；[rules/signals.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/rules/signals.py) 在信号中立即 `.defer()`（未等提交，回滚事务也会入队）。
- 信号由表单/视图/查询集在保存后显式发送（[transactions/forms.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/transactions/forms.py#L250-L264)、[transactions/models.py](file:///Users/kkcarrot/swe-project/WYGIWYH_fork2/app/apps/transactions/models.py#L59-L143)）。
- `Transaction` 仅有 `updated_at`，无显式版本；`TransactionRule` 无版本字段。
- 规则按 `order_by("order", "id")` 选取；单动作异常当前被捕获后继续执行后续动作；规则触发器不匹配则跳过该规则。
- 任务队列为 procrastinate 3.8（表 `procrastinate_jobs`，状态枚举含 todo/doing/succeeded/failed/cancelled/waiting；支持 `RetryStrategy(retry_exceptions=[...], max_attempts=...)`）。
- 已确认的关键决策：①「规则级隔离」= 规则上的执行模式开关；②出站机制 = `on_commit` 直接入队，无出站表；③新增真实提交按钮与端点；④交易与规则各新增单调 `version` 字段。

## Functional Requirements

- **FR-1（版本标识）**: `Transaction` 与 `TransactionRule` 各新增单调递增的 `version`（正整数，初始为 1，每次保存自增）；`save(update_fields=...)` 与查询集批量路径的版本自增必须实际持久化。事件入队载荷携带保存后的交易版本；`RuleExecution` 记录交易版本与规则版本。
- **FR-2（执行记录）**: 新增 `RuleExecution` 与 `RuleActionExecution`。`RuleExecution` 以（交易标识, 事件类型, 交易版本, 规则标识, 规则版本）唯一；状态包含 pending/completed/failed/stale/skipped。规则或交易被删除后历史记录仍可保留（标识列 + 可空外键）。
- **FR-3（原子应用）**: 默认执行模式下，一条规则的全部动作在同一内层事务（savepoint）中应用；任一动作抛出异常时全部动作、源交易修改、已创建派生交易均回滚；外层事务提交一条 `failed` 执行记录，包含失败动作标识、错误信息与输入版本。
- **FR-4（规则级隔离模式）**: 规则可显式选择隔离执行模式；每个动作使用独立 savepoint，单动作失败仅回滚该动作，执行继续，最终该执行为 completed，各动作状态分别记录（applied/failed/skipped）。
- **FR-5（幂等与重复事件）**: worker 处理事件时，对每条适用规则以唯一键原子认领（INSERT ... ON CONFLICT DO NOTHING）。同一事件第二次投递时，已终态的执行直接返回既有结果，不再次执行动作，派生交易不重复创建；并发的同键认领若遇到 pending 执行，报告可重试冲突。
- **FR-6（过期判定）**: worker 锁定目标交易后判定：当前版本 > 事件版本（乱序旧事件）、目标已软删而事件为 create/update、目标行已不存在且非硬删事件载荷，均判定为过期，写 `stale` 执行记录且不产生副作用；当前版本 < 事件版本属于异常时序，按可重试冲突处理。
- **FR-7（最小锁范围）**: upsert 动作先执行只读搜索，命中后仅对命中的单行 `SELECT ... FOR UPDATE`（NOWAIT）；未命中则创建。锁冲突（`LockNotAvailable`）或创建时唯一约束冲突（`IntegrityError`，如 internal_id）转换为可重试异常；不得对交易表加表锁或大范围锁。
- **FR-8（冻结求值上下文）**: 表达式中的 `datetime.now/today/utcnow`、`date.today` 由冻结上下文提供固定时间，`random()`/`randint()` 由固定种子的随机源产生。上下文可序列化为 `{evaluated_at, seed}` 并用 Django signing 签发为令牌；同令牌两次规划得到完全相同的求值结果。
- **FR-9（规划器复用）**: 试运行、提交端点与 worker 使用同一个只读规划器：评估触发器 → 按现有顺序规则合并排序动作 → 产出动作计划，每个动作效果分类为 modify/create/reject（reject 指触发器不匹配、动作 filter 守卫不满足或过期导致不应用），并给出用于展示的对象快照与用于比对的指纹。
- **FR-10（试运行与提交端点）**: 试运行端点以固定交易版本进行规划，返回分组展示（将修改/创建/拒绝的对象）与冻结上下文令牌；新增提交端点校验规则版本与交易版本未漂移后，以同一冻结上下文重新规划并真实执行，返回真实 id；版本或规则漂移时返回明确的过期/变更错误且无副作用。
- **FR-11（on_commit 入队）**: 信号处理器改为注册 `transaction.on_commit` 回调，在业务事务提交后 defer 新任务（携带事件类型、交易 id、交易版本、用户 id；删除事件携带序列化载荷与删除时版本）；事务回滚则不入队。删除旧任务 `check_for_transaction_rules`，新任务名为 `process_transaction_event`。
- **FR-12（顺序与继续执行）**: 规则仍按 (order, id) 选取；触发器不匹配的规则跳过；一条规则失败（原子模式回滚）或跳过时不阻塞后续规则；sequenced 规则的动作间名称更新、自定义动作排序、删除事件仅处理 upsert 动作等既有语义保持不变。
- **FR-13（迁移安全）**: 数据迁移将队列中旧任务（task_name='check_for_transaction_rules'，状态 todo/waiting）置为 cancelled；部署后旧载荷不会被新 worker 执行；迁移后若旧事件以新机制重放，凭借已存在的执行记录幂等返回，不重复产生副作用。
- **FR-14（来源关系）**: `RuleActionExecution` 记录动作类型、动作标识、顺序、状态、错误与效果明细（modify/create/reject + 目标标识与前后值快照）；派生交易以整数列记录生成它的动作执行 id（不引入跨应用外键环），形成可导航的来源关系。

## Non-Functional Requirements
- **NFR-1（锁与吞吐）**: 行锁持有时间尽量短，仅锁目标行；同一事件对多条规则的执行串行认领，避免死锁（统一按规则 id 顺序处理）。
- **NFR-2（正确性回归）**: 现有全部测试须继续通过（被替换内部实现除外）；新增场景均有自动化测试，且测试可在 PostgreSQL 15 上稳定重复运行。
- **NFR-3（可观测）**: worker 日志记录事件键、过期判定原因、重试冲突原因；执行记录在 Django admin 可读。
- **NFR-4（兼容）**: 不改变现有页面路由的整体结构；试运行页面在新增提交能力的同时保留视觉/日志两个标签。

## Constraints
- **Technical**: PostgreSQL 专属能力（FOR UPDATE NOWAIT、ON CONFLICT、django.signing）；Django 5.2 + procrastinate 3.8 + Python ≥ 3.11；worker 内须继续通过 thread-local 写入当前用户。
- **Business**: 规则执行结果必须可审计：原子失败也要保留记录；历史执行记录不因规则/交易删除而丢失关键标识。
- **Dependencies**: procrastinate 任务表在部署时已存在；数据迁移依赖 `procrastinate_jobs` 表结构。

## Assumptions
- 升级期间处于 doing 状态的旧任务可接受其按旧代码结束（或因任务注销而失败一次），数据迁移仅取消 todo/waiting 的旧任务。
- 试运行 updated 事件时，预览表单中的假设修改（input patch）被序列化进令牌；提交时在锁定的基础版本上应用该补丁并生成触发状态，随后规则执行；该真实保存触发的 on_commit 任务会命中已完成执行并幂等返回。
- 不同规则并发创建无 internal_id 的相似交易时，允许各自创建（与现有语义一致）；可重试冲突仅覆盖行锁竞争与唯一约束冲突路径。

## Acceptance Criteria

### AC-1: 原子模式下第二动作失败时全部回滚并留下失败执行记录
- **Type**: `rule`
- **Given**: 一条默认（原子）模式规则，含两个动作：动作 1 修改源交易字段并已创建一条派生交易；动作 2 在执行时必然抛出异常；交易版本 V、规则版本 R。
- **When**: worker 处理该事件版本 V。
- **Then**: 动作 1 的修改、源交易字段变更与已创建的派生交易全部回滚（数据库中不存在）；存在一条 `RuleExecution`，状态 failed，其唯一键含事件类型与交易版本 V、规则版本 R，记录中包含失败动作标识与错误信息。
- **Pass Condition**: 回滚后源交易相关字段等于事件前值；派生交易计数为 0；failed 执行记录的 failed_action、transaction_version=V、rule_version=R 字段可断言。
- **Evidence**: 自动化测试（TransactionTestCase，直接调用执行入口）断言 ORM 状态与记录字段。

### AC-2: 同一交易事件重复投递只产生一个执行且第二次返回既有结果
- **Type**: `rule`
- **Given**: 事件（交易 T, 类型 created, 交易版本 V）匹配一条规则，该规则创建一条派生交易；队列将同一载荷投递两次（第一次完成后第二次到达）。
- **When**: 依次执行两次工作任务。
- **Then**: `RuleExecution` 计数为 1；派生交易仅创建一次；第二次调用返回第一次的 completed 执行结果（同一 id、同一状态、同一效果集合）。
- **Pass Condition**: RuleExecution.objects.filter(唯一键).count() == 1；派生交易 count == 1；两次返回的执行 id 相同且状态 completed。
- **Evidence**: 自动化测试连续两次以相同载荷调用任务入口并断言。

### AC-3: 乱序版本不覆盖新结果；并发 upsert 串行成功或报告可重试冲突
- **Type**: `rule`
- **Given**: 场景 A：交易版本 V2 已处理完成，版本 V1 的同事件任务后到达。场景 B：两个 worker/线程并发对同一 upsert 动作的同一目标行执行更新或创建。
- **When**: 场景 A 执行 V1 任务；场景 B 并发执行两个 upsert（NOWAIT 行锁 / 创建唯一约束竞争）。
- **Then**: A：V1 任务不修改任何数据，产生 `stale` 执行记录，V2 的结果与数据保持不变。B：两个操作要么经行锁排队串行成功（最终状态一致、无重复行），要么后到者抛出可重试异常（procrastinate `RetryStrategy` 可识别并重试），不存在半应用状态或重复派生行。
- **Pass Condition**: A 可断言 stale 记录原因、V2 目标字段未变；B 可断言“串行成功时目标行仅一条且值为后提交值”或“捕获可重试异常类型”，两分支必居其一，且任一情况下无异常残留行。
- **Evidence**: 自动化测试：版本号构造乱序；并发用线程 + 真实 PG（select_for_update nowait）触发，断言两分支条件。

### AC-4: 固定版本试运行后立即提交，修改与创建集合一致
- **Type**: `rule`
- **Given**: 交易 T 固定版本 V、规则版本 R；表达式含时间/随机调用；试运行产生 modify/create 集合与冻结上下文令牌，随后立即提交，期间无其他编辑。
- **When**: 调用试运行端点规划，再以返回令牌调用提交端点。
- **Then**: 提交被接受并真实执行；试运行展示的修改集合（目标标识 + 字段前后值指纹）与创建集合（动作标识 + 序号 + 字段值指纹）与真实提交结果逐项一致；创建对象在提交结果中带真实 id。
- **Pass Condition**: 规划效果指纹集合 == 提交效果指纹集合（modify、create 分类后比较）；提交后对应对象在库中可查；若 V 或 R 已漂移则端点返回错误且无副作用（反向场景另测）。
- **Evidence**: 自动化测试：调用规划服务与提交服务/端点，比较指纹集合；对漂移输入断言错误路径。

### AC-5: on_commit 后才入队，回滚事务不产生任务
- **Type**: `rule`
- **Given**: 会触发规则信号的交易保存；测试使用 procrastinate InMemory/测试连接器计数。
- **When**: 业务事务回滚时无任务入队；业务事务提交后恰有一个 `process_transaction_event` 任务入队，载荷含交易版本。
- **Then**: 回滚分支任务计数为 0；提交分支任务计数为 1 且载荷 transaction_version 为保存后版本。
- **Pass Condition**: 两种分支下计数与载荷字段可断言。
- **Evidence**: 自动化测试包裹 `transaction.atomic()` 并触发回滚/提交，检查测试连接器任务。

### AC-6: 冻结求值上下文保证时间与随机值在预览和执行中一致
- **Type**: `rule`
- **Given**: 表达式使用 `datetime.now()`、`date.today()`、`random()`、`randint(...)`；同一冻结令牌（evaluated_at, seed）。
- **When**: 用该令牌两次构建规划；并与一个新（未冻结）上下文对比。
- **Then**: 同令牌两次规划中上述表达式求值结果逐项相同；新上下文可产生不同结果（证明冻结有效）；令牌签名被篡改时拒绝。
- **Pass Condition**: 同令牌结果序列相等；篡改令牌抛签名校验异常。
- **Evidence**: 自动化测试直接构造冻结上下文进行规划并比较求值序列。

### AC-7: 并发编辑、软删、硬删与乱序到达均能判定过期
- **Type**: `rule`
- **Given**: 四种时序：①事件版本 V 入队后、处理前交易被编辑至 V' > V；②目标已软删而到达 create/update 事件；③目标已硬删且非硬删载荷事件；④旧版本事件晚于新版本处理。
- **When**: worker 锁定目标行并判定。
- **Then**: 四种情形均不产生副作用，各自产生 `stale` 执行记录且原因可区分；硬删事件（携带删除时版本与序列化载荷）在目标行不存在时仍正常执行可执行动作。
- **Pass Condition**: 每个情形断言无修改/创建、stale 状态与原因枚举；硬删删除事件断言其动作正常应用。
- **Evidence**: 自动化测试逐情形构造并断言。

### AC-8: 规则级隔离模式下单动作失败不影响其他动作
- **Type**: `rule`
- **Given**: 隔离模式规则含三个动作，动作 2 必然失败，动作 1/3 可成功。
- **When**: worker 执行该规则。
- **Then**: 动作 1、3 的效果持久化，动作 2 仅回滚自身；`RuleExecution` 为 completed；三条 `RuleActionExecution` 状态分别为 applied/failed/applied，失败记录含错误信息。
- **Pass Condition**: 三个动作效果与状态字段均可断言。
- **Evidence**: 自动化测试。

### AC-9: 每个动作记录幂等结果、效果分类与来源关系
- **Type**: `rule`
- **Given**: 规则含 edit 动作、upsert 命中（modify）、upsert 未命中（create）、filter 守卫不满足（reject）各一。
- **When**: 一次事件执行完成。
- **Then**: 每个动作有一条 `RuleActionExecution`，记录动作类型/动作标识/顺序/状态；效果明细含分类（modify/create/reject）、目标标识、前后值快照；被创建的派生交易上记录了生成它的动作执行 id；重复执行同键不新增动作记录。
- **Pass Condition**: 动作记录计数、分类、目标 id、派生交易来源列可断言；重复事件后记录总数不变。
- **Evidence**: 自动化测试。

### AC-10: 迁移取消历史任务且不重复产生副作用
- **Type**: `rule`
- **Given**: `procrastinate_jobs` 中存在旧任务名的 todo/waiting 任务（及 doing 任务对照）；执行本次数据迁移。
- **When**: 迁移应用后检查任务状态，并模拟旧载荷事件以新机制到达。
- **Then**: todo/waiting 旧任务变为 cancelled；doing 任务不被修改；代码库中不存在旧任务注册；旧事件重放时若已有执行记录则幂等返回，无新增副作用。
- **Pass Condition**: SQL 状态断言与代码检索断言；重放后派生行计数不变。
- **Evidence**: 迁移测试（迁移器应用/取消）+ 代码检索证据。

### AC-11: 规则顺序与跨规则继续执行保持兼容
- **Type**: `rule`
- **Given**: 同一事件匹配三条规则（不同 order），其中规则 2 在原子模式下失败、规则 3 触发器不匹配之外另有规则 4 可成功；另含 sequenced 规则与自定义动作顺序规则。
- **When**: worker 处理事件。
- **Then**: 规则严格按 (order, id) 处理；规则 2 回滚不阻塞规则 4；规则 3 写 skipped；sequenced 规则后续动作可见前序修改；自定义排序规则的动作按 (order, id) 交错执行。
- **Pass Condition**: 处理顺序（由执行记录时间/顺序与效果断言）、各规则终态及既有动作排序语义可断言。
- **Evidence**: 自动化测试。

### AC-12: 规划器单一复用与结构质量
- **Type**: `rubric`
- **Dimension**: 试运行、提交、worker 三条路径复用同一规划器与同一效果/指纹模型的程度；旧任务巨函数被消除、职责分层清晰度。
- **Scale**: 1-5
- **Anchors**: 1 = 三处仍各有一份规划逻辑；3 = 规划器被复用但效果模型存在双份转换；5 = 单一只读规划器 + 单一效果模型驱动全部入口，执行器仅负责落库，无重复分支。
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查（模块结构、调用关系）+ 调用点检索证据。

### AC-13: 锁范围与并发设计质量
- **Type**: `rubric`
- **Dimension**: upsert/worker 锁定范围是否最小、锁顺序是否统一、可重试冲突语义是否完整。
- **Scale**: 1-5
- **Anchors**: 1 = 存在表锁或全量 FOR UPDATE；3 = 行锁但持有跨多语句且错误分类不完整；5 = 仅锁单行、NOWAIT、统一顺序、锁/唯一冲突均转换为可重试异常并有测试。
- **Pass Threshold**: >= 4
- **Evidence**: 代码审查 + AC-3/AC-7 的并发测试证据。

## Open Questions
- 无（关键歧义已通过用户确认解决；次级行为见 Assumptions）。
