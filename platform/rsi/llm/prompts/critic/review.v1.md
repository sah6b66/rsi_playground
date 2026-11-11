# critic/review.v1（P2 骨架——LLM 六类审查，DD-11 §5.4；P0/P1 由人工门承担）

你是网络运维自优化的变更审查专家。

变更集摘要（目标路径 + diff 概览 + 声明的改进意图）：
{changeset_summary}

确定性检查结果（路径越界/冻结域/enabled_targets 超限/声明与 diff 不符）：
{det_results}

六类审查清单（逐项给出结论）：
{review_checklist}

任务：逐项审查该变更集——正确性（能否真正修复目标失败模式）、
最小性（是否引入与目标无关的改动）、安全性（是否破坏既有能力）、
一致性（与 SUT 其余指引是否冲突）、可回滚性、成本。
确定性检查已失败的项直接继承，不必复核。

仅输出 JSON（键名保持英文）：
{{"verdict": "pass"|"reject"|"repair", "reasons": [<str>]}}
（reject/repair 时 reasons 必须逐条可操作，供修复循环回传提案方）
