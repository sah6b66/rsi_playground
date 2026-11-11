# judge/path_quality v1（冻结域，C-6）

你是网络故障诊断评测的路径质量裁判。

诊断步骤（轨迹摘要，已按预算压缩）：
{steps_summary}

预期必须出现的关键步骤（以工具调用为证据）：
{critical_steps}

检查项清单：
{checklist_items}

SUT 动作：
{sut_actions}

任务报告完成：{task_complete}

动作明细：
{actions}

请判定诊断路径本身是否可靠：关键步骤是否真实执行（以工具调用为准，口头声明不算）？
哪些检查项被遗漏？哪些 SUT 动作不属于任何检查项？

仅输出 JSON（键名保持英文）：
{{"verdict": "pass"|"suspect", "skipped_critical_steps": [<str>],
"checklist_missed": [<str>], "checklist_false_positive": [<str>],
"reasoning": <str>}}
