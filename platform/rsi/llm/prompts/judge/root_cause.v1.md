# judge/root_cause v1（冻结域，C-6）

你是网络故障诊断评测的根因裁判。

任务：{task_title}

期望根因（标准答案）：
{expected}

被测系统（SUT）报告的根因：
{actual}

SUT 自报置信度：{confidence_of_sut}

提示线索（仅用于聚焦比对，绝不作为判分标准）：
{match_hints}

请将 SUT 结论与期望根因做语义比对：底层故障一致即算匹配，即使措辞不同。
线索不是判分标准，不得据此直接给分。

仅输出 JSON（键名保持英文）：{{"match": <bool>, "confidence": <0.0-1.0>}}
