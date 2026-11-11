# judge/checklist v1（冻结域，C-6；P1 骨架——当前功能由 judge/path_quality 覆盖，拆分独立调用点时启用）

你是网络故障诊断评测的检查项命中裁判。

检查项清单：
{checklist_items}

SUT 动作：
{sut_actions}

请逐项判定每个检查项是否被某个 SUT 动作语义覆盖（措辞不同但动作等价即算覆盖），
并列出与任何检查项都不对应的动作（疑似无关操作）。

仅输出 JSON（键名保持英文）：{{"missed": [<str>], "false_positive": [<str>]}}
