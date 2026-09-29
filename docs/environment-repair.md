# 环境补全的反馈闭环

初次 Completion 先生成任务开始前的候选，Sufficiency 在只读沙盒中判断上下文并采集任务所需的执行探针。编排层对每个有效候选最多追加两轮处理，不修改原始任务或验收义务。

- 上下文充分且探针齐备：直接进入原有候选选择与 TaskFit。
- 上下文充分、只缺探针收据：只让 Sufficiency 复查并补采探针，不调用 Completion 改写文件。
- 上下文缺口、完整性问题或真实探针执行失败：将缺失上下文、完整性诊断、环境合同错误、全部探针收据和检查解释交回 Completion，在上一轮候选上增量修复，再独立复核。
- Completion 无法修复、明确基础设施故障、连续两轮工作区及诊断均无变化，或修复轮次耗尽：停止该候选的修复，保留具体原因并继续原有候选选择。不会把耗尽次数写成通过，也不把基础设施故障写成不可重建。

只读审查和报告任务的探针应检查必要源码可读、分析工具可用、临时目录中的报告生成可重复。只有任务依赖编译或程序运行时才要求对应构建能力，缺少构建清单本身不能证明审查任务不可执行。

## Terminal 通用阶段职责

两种 Completion 角色和 Sufficiency 共用 agents.roles 中的 TERMINAL_TASK_START_RESPONSIBILITIES。这份职责以用户目标所需的实际操作为依据，区分待补齐的初态条件与留给 solver 的目标变更；FILE/NON_FILE 只描述验收产物，不能决定任务是否需要执行程序，也不能把 FILE 修改列表当作所有环境依赖。

这些职责对 terminal 的所有 session 相同；具体源码、数据、依赖和探针由 agent 根据输入证据产生。源码修复应针对阶段职责、工具协议或数据契约的共性缺陷，不增加会话编号或业务文件名的专用流程。泛化验证需要在同一冻结版本上检查不同任务及未据此调过代码的会话；单样本通过和普通回归均不能证明 domain 已具备稳定交付能力。

## 不变量

修复前核对候选所有文件的内容摘要。每轮保留上一轮模型补全文件的来源和证据引用；未改文件不会丢失。运行声明省略时继承，显式返回新数组时替换旧声明，显式空数组表示清除；避免纠正依赖版本后仍并存冲突版本。原 Replay 仍是唯一原始观测来源：完整文件不可覆盖，部分文件默认保留已观察片段。必要的采集损坏纠正可声明 capture_repairs，每项给出 old_text、new_text 和 reason；旧片段须在原始 PARTIAL 中唯一定位且不重叠，应用替换后其余观测字节仍须出现在候选中。写入继续引用原有证据，候选及修复声明标为 MODEL_COMPLETED，不声称是历史原文。工具写入、最终 JSON、返修种子和 Sufficiency 上下文共用这一契约。Sufficiency 反馈只是诊断，不能作为新证据，也不授予预解任务或生成目标产物的权限。

这个闭环仅处理有效 Completion 候选的后续缺口；初次 Completion 未完成、协议不合法或沙盒无法启动，仍显式报告对应阶段故障。探针 PASS 只说明进程退出及工作区保护检查通过；应结合实际输出和任务要求判断能力，不能把只打印错误的零退出当成可执行证明。工具首次调用前明确工作区相对路径、TRACEFORGE_WORKSPACE、TRACEFORGE_PROBE_SCRATCH 和 assert/非零失败方式。

“无进展”摘要包含工作区、上下文合同错误及全部探针的代码/类型/状态；补齐 load、reset、dependency 属于进展，随机编号、说明文案、顺序和重复收据不属于进展。不会据此增加修复轮数或自动接受环境。

## 产物位置

原始 `completion/` 与第一次 `sufficiency/<candidate>/` 保留。后续轮次写入：

~~~
tasks/<task_id>/environment_repairs/<candidate>/
├── repair_audit.json
├── 001/
│   ├── feedback.json
│   ├── completion/       # 仅需要改环境时存在
│   └── sufficiency/
└── 002/                 # 仅确实继续修复时存在
~~~

`repair_audit.json` 记录各轮动作、工作区、阶段产物路径、最后诊断和停止原因；任务级 manifest 的 `environment_repair_audit` 提供同样入口。后续 TaskFit 和 Verifier 使用最后一轮候选快照及复核结果，历史快照保持不变。RED-only 路径可能继续处理 REVIEW 候选；进入下游不表示环境已经合格。

普通回归使用确定性 agent 替身验证状态转换与证据保护；真实模型产物质量和可解性必须另外运行真实沙盒流程确认。

## 当前依赖执行边界

dependencies/runtime_constraints 保留为声明，不把自由文本转成命令。对有 requirements.txt 的 Python 初态，Sufficiency 准备阶段在目标 AGS 中下载 wheel，固定直接和间接依赖版本及 SHA256，随后离线安装并保留收据；宿主机不执行项目安装。

冻结依赖放在候选 workspace 之外的 python_runtime/。Verifier 角色、Bundle 的 agent 环境和独立无网 verifier 使用同一份锁定内容，Harbor 在角色启动前执行环境中的 setup.sh。原声明或 wheel 内容变化时拒绝复用；安装失败保留环境错误，不能算任务失败或语义不可重建。目前自动准备仅覆盖根目录 requirements.txt 且依赖有适配目标 Python 的 wheel，其他依赖方式不声称已支持。
