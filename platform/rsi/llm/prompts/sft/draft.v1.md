# sft/draft.v1（P1 骨架——SFT 准入报告起草，DD-11 §5.5；P0 由模板人工填写）

你是网络运维自优化的 SFT（总结性知识转移）报告起草专家。

缺陷簇清单（B 类缺陷——指引缺失，且多轮优化无收益）：
{defect_clusters}

优化尝试历史（已尝试的方向与结果）：
{opt_history}

任务：起草 SFT 报告——说明该缺陷类别为何应停止自优化、转入知识补充/人工介入，
附样本清单与已尝试方向摘要，供决策者评审。语气客观，以数据为依据。

仅输出 JSON（键名保持英文）：
{{"defect_class_summary": <str>, "sample_run_ids": [<str>],
"attempted_directions": [<str>], "recommendation": <str>,
"knowledge_gaps": [<str>]}}
