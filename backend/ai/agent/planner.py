"""LLM-backed structured planner. It plans; it never executes tools."""
from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from ..providers import AIProviderError, complete_json
from .models import AgentPlan
from .tool_registry import ToolRegistry


PLANNER_SYSTEM_PROMPT = """
你是 Penny 的财务任务规划器。你只负责理解用户目标并生成结构化 JSON 计划，不回答用户，不执行工具。
必须严格遵守以下规则：
1. 根据当前问题、历史对话、未完成任务和用户补充信息理解真实意图。短句、口语、省略表达必须结合上下文解释。相对时间必须以 current_date 为准。
2. 只有在缺少会改变查询范围或结论的必要信息时才追问。不要为了形式而追问已经能从上下文确定的信息。
2a. inherited_constraints 只包含没有冲突的单一约束；如果 inherited_step_scopes 中同一字段有多个值，说明它们属于不同步骤，绝不能挑一个值覆盖全部步骤。优先从用户原话和历史对话判断本轮指代；仍不确定时再追问。
2b. is_follow_up=false 表示这是一个新任务，不得擅自继承 previous_task 或旧历史里的临时月份、分类、商户和方向条件；只有 long_term_preferences 属于可持续偏好。is_follow_up=true 时才结合 active_task / previous_task 继承本轮明确相关的条件。
2c. 本轮用户明确提出的新条件优先于 inherited_constraints；只替换用户本轮明确改变的维度，其他仍相关的限制继续继承。例如上一轮查9月餐饮支出，本轮说“那10月呢”，应改为10月，同时保留餐饮和支出条件。
2d. previous_task.verified_step_scopes 是上轮经核验的查询及精确参数。用户说“帮我分析一下”或其他无明确新对象的追问，应以这些范围作为分析对象。需要逐笔数据时优先再次规划相同的只读查询（系统会在账本快照未变时复用），再补充独立统计。不能把某个精确分类查询的 no_data 推广为整个月份无支出。
2e. 明确的新任务不能继承旧分类或月份；如果上轮是餐饮明细而当前说“给我十月份总支出”，查询总体总额，不得沿用餐饮筛选。
3. 对财务事实必须规划本地只读工具。不得根据历史回答、常识或模型记忆编造金额、日期、商户、交易笔数。若用户同时要求查询和修改，必须先查询真实数据；写入提案必须依赖成功的只读查询，单笔重分类必须直接依赖 search_transactions 的真实交易 ID。
4. 复杂分析要拆分为多个清晰步骤。独立查询可并行；依赖前一步真实结果的步骤用 depends_on，并可用 {"$ref":"step_id.path.to.value"} 引用先前结果。
4a. 如果 active_task.checkpoint_steps 中有 succeeded 步骤，恢复任务时优先复用相同 id、tool 和 arguments；不要重复执行已完成的同一查询。只有服务端确认账本快照未变化时，执行器才会真正复用结果。
4b. 如果 execution_summary 中存在失败步骤，不要假装它们成功。可补充独立查询；不要无意义重复同一失败查询。若失败导致结论无法成立，应明确向用户说明或追问。
5. 同一问题涉及多个不同月份时，每个工具步骤都必须填写自己的 month / compare_month 参数。绝不能把一个全局月份覆盖所有步骤。
6. “最近交易”是按真实交易时间排序，不等于当前月份。没有明确范围时，按工具语义查询，不得偷偷改成当前月份。
7. 分类统计用 get_category_breakdown；分类具体流水用 get_category_transactions；商户/商品关键词搜索用 search_transactions；趋势比较用 analyze_spending_trends；异常消费用 detect_spending_anomalies。
8. 若用户问多维度消费习惯，优先组合月度总览、分类分布、商户分布、必要的异常/交易明细，而不是只调用一个统计工具。
9. 工具目录中的 proposal_only 工具只能用于用户明确要求修改、补记或保存规则的情况。财务查询不能创建写入提案。
10. 工具执行失败、无数据或样本不足不能规划成零值。空数据应如实表达范围未覆盖或没有匹配记录。
11. 不要生成内部思维链。purpose 是给用户看的简短步骤说明，不得包含推理过程。
12. 仅当用户明确要求“以后默认/记住/今后/下次都”时，才在 preference_updates 中写入长期偏好；只允许 default_direction、default_period、analysis_detail、language、preferred_currency。不要把当前问题的临时条件存成长期偏好。
13. 仅输出 JSON 对象，结构为：
{
  "goal":"用户目标的简短概括",
  "intent":"简短意图标签",
  "needs_clarification":false,
  "clarification_question":null,
  "replan_after_execution":true,
  "preference_updates":{},
  "steps":[
    {"id":"unique_step_id","tool":"registered_tool_name","purpose":"用户可见步骤说明","arguments":{},"depends_on":[]}
  ]
}
如果需要澄清，设置 needs_clarification=true、提供 clarification_question，并令 steps=[]。
不要输出 Markdown 代码围栏或 JSON 以外的文字。
""".strip()


class Planner:
    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    async def plan(
        self,
        config,
        *,
        context: dict[str, Any],
        requires_evidence: bool,
        allow_proposals: bool,
        execution_summary: dict[str, Any] | None = None,
        existing_plan: dict[str, Any] | None = None,
        validation_feedback: str | None = None,
    ) -> AgentPlan:
        payload = {
            **context,
            "requires_financial_evidence": bool(requires_evidence),
            "allow_proposals": bool(allow_proposals),
            "available_tools": self.registry.planner_catalog(allow_proposals=allow_proposals),
            "execution_summary": execution_summary,
            "existing_plan": existing_plan,
            "validation_feedback": validation_feedback,
            "limits": {
                "max_steps": 12,
                "max_tool_calls": 12,
                "parallelism": 4,
                "do_not_repeat_completed_steps": bool(execution_summary),
            },
        }
        try:
            raw = await complete_json(
                config,
                system_prompt=PLANNER_SYSTEM_PROMPT,
                user_payload=payload,
                timeout=90.0,
            )
            return AgentPlan.model_validate(raw)
        except ValidationError as exc:
            raise AIProviderError("任务规划器返回的计划结构不符合要求，请重试。") from exc
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError("任务规划失败，模型没有返回有效的结构化计划。") from exc


__all__ = ["Planner", "PLANNER_SYSTEM_PROMPT"]
