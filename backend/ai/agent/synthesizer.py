"""Natural-language response synthesis from verifier-approved evidence only."""
from __future__ import annotations

from typing import Any

from ..providers import AIProviderError, complete_json


SYNTHESIZER_SYSTEM_PROMPT = """
你是 Penny 的回答生成器。你只能依据输入中 verifier 标记为 verified、no_data、partial 或 proposal 的本轮结果回答。
要求：
- 先给结论，再给关键依据，表达自然，不机械罗列工具调用。
- 所有金额、日期、笔数、商户、交易 ID 必须能在提供的证据结果中找到。金额、笔数、比例使用阿拉伯数字，避免用中文数字绕过核验。
- state=no_data 代表该范围没有可用/匹配记录，不代表金额为 0，不得将空结果解释为零支出。
- 如果 partial=true，必须明确哪些步骤失败、哪些结论暂时不能下；不得把部分结果包装成完整分析。
- proposal 状态仅表示“待确认提案已创建”，绝不表示已经写入或修改账本。
- 如果某个月的某分类或某一工具查询没有记录，只说明这一【精确筛选范围】缺少匹配数据，不代表其他分类、其他查询或整个月份没有流水。多个来源出现矛盾时说明口径差异，不能选择性相信其中一个。
- 若有跨轮次复用结果，这些证据也必须来自快照一致且已通过核验的本轮证据集合。
- 对异常候选只称“待复核/疑似”，不能称为欺诈或确定错误。
- 对交易明细只能引用 evidence 中真实 transactions 数组的记录。商户、分类和交易对象名称必须原样来自证据，不得补写；明确点名商户时写作“商户是「商户名」”，明确点名分类时写作“分类为「分类名」”，以便程序核验。
- 不要引用历史对话中未出现在本轮证据里的数字。
- 不要输出思维链、隐藏推理或工具执行细节。可以用“数据来源”说明本地账本及查询范围。
- used_evidence_ids 必须列出回答中所有财务事实实际依赖的证据编号，不得引用无效编号；只要回答包含财务事实，就不能留空。
- 只返回 JSON：{"answer":"...","used_evidence_ids":["E001"]}。
""".strip()


class ResponseSynthesizer:
    async def synthesize(
        self,
        config,
        *,
        user_message: str,
        goal: str,
        plan: dict[str, Any],
        verification: dict[str, Any],
        validation_feedback: str | None = None,
    ) -> tuple[str, list[str]]:
        payload = {
            "user_question": user_message,
            "goal": goal,
            "plan": plan,
            "partial": bool(verification.get("partial")),
            "issues": verification.get("issues") or [],
            "verified_evidence": verification.get("usable_results") or [],
            "evidence_index": verification.get("evidence") or [],
            "validation_feedback": validation_feedback,
        }
        try:
            raw = await complete_json(
                config,
                system_prompt=SYNTHESIZER_SYSTEM_PROMPT,
                user_payload=payload,
                timeout=90.0,
            )
        except AIProviderError:
            raise
        except Exception as exc:
            raise AIProviderError("回答生成失败，无法从已核验数据生成可靠答复。") from exc
        answer = str(raw.get("answer") or "").strip()
        if not answer:
            raise AIProviderError("模型没有生成有效的最终回答。")
        used = raw.get("used_evidence_ids") or []
        if not isinstance(used, list):
            used = []
        return answer[:12000], [str(item) for item in used[:20]]


__all__ = ["ResponseSynthesizer", "SYNTHESIZER_SYSTEM_PROMPT"]
