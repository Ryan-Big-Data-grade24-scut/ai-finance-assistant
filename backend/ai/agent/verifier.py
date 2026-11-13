"""Evidence-chain verifier for financial tool results and generated answers."""
from __future__ import annotations

from typing import Any, Callable

from .models import AgentPlan, VerificationResult


class Verifier:
    def __init__(self, registry, amount_validator: Callable[[str, list[dict]], bool] | None = None) -> None:
        self.registry = registry
        self.amount_validator = amount_validator

    def verify(
        self,
        *,
        plan: AgentPlan,
        executions: list[dict[str, Any]],
        requires_evidence: bool,
    ) -> VerificationResult:
        evidence: list[dict[str, Any]] = []
        usable_results: list[dict[str, Any]] = []
        issues: list[str] = []
        failed = False
        partial = False
        verified_read_count = 0

        for index, execution in enumerate(executions, start=1):
            tool_name = str(execution.get("tool") or "")
            spec = self.registry.get(tool_name)
            result = execution.get("result")
            result = result if isinstance(result, dict) else {}
            state = "verified"
            reason = None

            if spec is None:
                state = "failed"
                failed = True
                reason = "工具不在当前注册表中。"
            elif requires_evidence and not spec.read_only and not spec.proposal_only:
                state = "failed"
                failed = True
                reason = "财务事实任务不允许使用非提案写入工具作为证据。"
            elif execution.get("status") != "succeeded" or result.get("error"):
                state = "failed"
                failed = True
                reason = str(execution.get("error") or result.get("error") or "工具执行失败")[:300]
            # An incomplete Ledger result must never be downgraded to a harmless
            # no-data result. Check completeness before checking has_data.
            elif result.get("ledger_complete") is False:
                state = "incomplete"
                failed = True
                reason = str(result.get("accounting_note") or "Ledger 证据不完整。")[:300]
            elif self._internally_inconsistent(result):
                state = "conflict"
                failed = True
                reason = "工具返回的空数据标记与真实笔数或明细不一致。"
            elif self._result_scope_mismatch(execution.get("arguments") or {}, result):
                state = "failed"
                failed = True
                reason = "查询返回的月份或方向与计划参数不一致，已拒绝跨范围证据。"
            elif spec.proposal_only:
                if result.get("status") == "pending_confirmation":
                    state = "proposal"
                    reason = "操作提案已创建，但尚未执行，等待用户确认。"
                else:
                    state = "failed"
                    failed = True
                    reason = "写入提案工具没有返回有效的待确认状态。"
            elif result.get("has_data") is False:
                state = "no_data"
                reason = str(result.get("message") or "该查询范围没有可用数据。")[:300]
            elif isinstance(result.get("month_coverage"), dict) and any(
                not bool(item.get("has_data"))
                for item in result["month_coverage"].values() if isinstance(item, dict)
            ):
                state = "partial"
                partial = True
                reason = "比较范围中至少一个月份没有可用交易记录，不能把该月解释为零消费。"
            elif self._has_empty_collection_without_scope_data(result):
                state = "no_data"
                reason = "查询成功，但没有找到匹配记录；不能将其解释为零支出。"

            if state in {"verified", "partial"} and spec and spec.read_only:
                verified_read_count += 1
            if state in {"verified", "no_data", "partial", "proposal"}:
                usable_results.append({
                    "evidence_id": f"E{index:03d}",
                    "step_id": execution.get("step_id"),
                    "tool": tool_name,
                    "read_only": bool(spec.read_only) if spec else False,
                    "proposal_only": bool(spec.proposal_only) if spec else False,
                    "scope": self._scope(execution.get("arguments") or {}, result),
                    "state": state,
                    "reason": reason,
                    "result": result,
                })
            else:
                issues.append(f"{tool_name}：{reason or '证据核验失败'}")

            evidence.append({
                "evidence_id": f"E{index:03d}",
                "step_id": execution.get("step_id"),
                "tool": tool_name,
                "scope": self._scope(execution.get("arguments") or {}, result),
                "state": state,
                "read_only": bool(spec.read_only) if spec else False,
                "proposal_only": bool(spec.proposal_only) if spec else False,
                "reason": reason,
                "transaction_ids": self._transaction_ids(result),
                "summary": self._summary(result, state),
            })

        # Detect mutually inconsistent readings of the same exact scope. A
        # second query is not evidence that the first one was false; report the
        # disagreement and quarantine those results instead of choosing one.
        conflicts = self._find_conflicts(usable_results)
        if conflicts:
            conflict_ids = set().union(*conflicts)
            issues.append("同一查询口径返回了相互矛盾的数据，相关结果已隔离，不能据此下结论。")
            failed = True
            usable_results = [
                item for item in usable_results if item["evidence_id"] not in conflict_ids
            ]
            for item in evidence:
                if item["evidence_id"] in conflict_ids:
                    item["state"] = "conflict"
                    item["reason"] = "相同查询口径的证据存在冲突，已暂停使用。"

        if requires_evidence and verified_read_count == 0 and not any(
            item["state"] in {"no_data", "partial"} and self.registry.get(item["tool"]) and self.registry.get(item["tool"]).read_only
            for item in evidence
        ):
            issues.append("没有取得成功的本地只读财务证据。")
            failed = True

        if len(executions) < len(plan.steps):
            issues.append("并非所有计划步骤都完成执行。")
            failed = True

        return VerificationResult(
            verified=not failed and not partial and (not requires_evidence or bool(usable_results)),
            partial=(failed or partial) and bool(usable_results),
            issues=issues,
            evidence=evidence,
            usable_results=usable_results,
        )

    @staticmethod
    def _internally_inconsistent(result: dict[str, Any]) -> bool:
        if result.get("has_data") is not False:
            return False
        if isinstance(result.get("count"), int) and result["count"] > 0:
            return True
        if any(isinstance(result.get(key), list) and bool(result[key])
               for key in ("transactions", "categories", "merchants")):
            return True
        return False

    @staticmethod
    def _result_scope_mismatch(arguments: dict[str, Any], result: dict[str, Any]) -> bool:
        for key in ("month", "direction", "date"):
            expected = arguments.get(key)
            actual = result.get(key)
            if expected not in (None, "") and actual not in (None, ""):
                if isinstance(expected, str) and isinstance(actual, str) and expected != actual:
                    return True
        compare = arguments.get("compare_month")
        if compare and result.get("previous_month") and result["previous_month"] != compare:
            return True
        return False

    @staticmethod
    def _find_conflicts(usable_results: list[dict[str, Any]]) -> list[set[str]]:
        """Compare comparable queries; never equate unlike filter scopes."""
        import json
        by_scope: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in usable_results:
            if item.get("read_only") is False:
                continue
            scope = item.get("scope") or {}
            identity = (
                str(item.get("tool") or ""),
                json.dumps(scope, sort_keys=True, ensure_ascii=False, default=str),
            )
            by_scope.setdefault(identity, []).append(item)
        conflicts = []
        for group in by_scope.values():
            if len(group) < 2:
                continue
            signatures = set()
            for item in group:
                result = item.get("result") or {}
                # Compare only well-defined data indicators. A missing field is
                # not evidence of a contradictory value.
                signature = (
                    item.get("state") == "no_data",
                    result.get("has_data"),
                    result.get("count"),
                    result.get("total_amount"),
                )
                signatures.add(str(signature))
            if len(signatures) > 1:
                conflicts.append({str(item["evidence_id"]) for item in group})

        # Some aggregate tools cover every category in one month/direction.
        # If such a broad aggregate reports no rows but a transaction-detail
        # query proves rows exist for that same month/direction, the two sources
        # disagree. A narrow keyword search returning empty is NOT equivalent.
        for item in usable_results:
            if (item.get("state") != "no_data"
                or item.get("tool") != "get_category_breakdown"):
                continue
            scope = item.get("scope") or {}
            month = scope.get("month")
            direction = scope.get("direction", "支出")
            if not month:
                continue
            for other in usable_results:
                if other is item or other.get("state") != "verified":
                    continue
                other_scope = other.get("scope") or {}
                result = other.get("result") or {}
                if (other_scope.get("month") == month
                    and other_scope.get("direction", "支出") == direction
                    and other.get("tool") == "get_category_transactions"
                    and isinstance(result.get("count"), int)
                    and result["count"] > 0):
                    conflicts.append({str(item["evidence_id"]), str(other["evidence_id"])})
        return conflicts

    @staticmethod
    def factual_fallback(verification: VerificationResult, plan: AgentPlan) -> str:
        """Deterministic answer when LLM prose cannot be evidence-validated.

        Never invent numbers, derive ratios, or turn no-data into a zero amount.
        """
        purposes = {step.id: step.purpose for step in plan.steps}
        lines = ["本轮有部分结果可以直接从本地账本核实。以下只列出已确认的数据："]
        has_values = False
        for item in verification.usable_results[:12]:
            if not item.get("read_only"):
                continue
            state = str(item.get("state") or "")
            scope = item.get("scope") or {}
            result = item.get("result") or {}
            title = purposes.get(str(item.get("step_id") or "")) or str(item.get("tool") or "查询")
            scope_label = "、".join(str(value) for key, value in scope.items()
                                   if key in {"month", "compare_month", "category", "direction", "keyword"})
            label = f"{title}（{scope_label}）" if scope_label else title
            if state == "no_data":
                lines.append(f"- {label}：该筛选条件下未取得匹配记录，不能据此判断整月消费为零。")
                has_values = True
                continue
            if state not in {"verified", "partial"}:
                continue
            detail = []
            for direction, name in (("expense", "支出"), ("income", "收入")):
                value = result.get(direction)
                if isinstance(value, dict) and isinstance(value.get("amount"), (int, float)):
                    detail.append(f"{name} ¥{float(value['amount']):.2f}"
                                  + (f"、{int(value['count'])}笔" if isinstance(value.get("count"), int) else ""))
            count = result.get("count")
            if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
                detail.append(f"匹配 {count} 笔" if count else "未找到匹配交易")
            amount = result.get("total_amount")
            if isinstance(amount, (int, float)):
                detail.append(f"返回总额 ¥{float(amount):.2f}")
            for key, description in (("categories", "分类"), ("merchants", "商户"), ("transactions", "交易明细")):
                value = result.get(key)
                if isinstance(value, list) and value:
                    detail.append(f"已取得{description}数据")
            if detail:
                lines.append(f"- {label}：" + "；".join(detail) + "。")
                has_values = True
        if not has_values:
            return "本轮没有足够的可核实账本数据，暂时不能给出财务结论。请核对账单和查询范围。"
        lines.append("由于完整分析文字未通过逐项核验，我没有把未经证实的比例、日期或消费判断写入结论。")
        if verification.issues:
            lines.append("部分查询存在问题：" + "；".join(str(x) for x in verification.issues[:3]))
        return "\n".join(lines)

    def verify_answer(
        self,
        answer: str,
        evidence_results: list[dict[str, Any]],
        *,
        used_evidence_ids: list[str] | None = None,
    ) -> tuple[bool, str | None]:
        allowed_ids = {str(item.get("evidence_id")) for item in evidence_results}
        if used_evidence_ids is not None:
            invalid_ids = [item for item in used_evidence_ids if item not in allowed_ids]
            if invalid_ids:
                return False, "回答引用了不存在或未核验的证据编号。"
            if evidence_results and not used_evidence_ids:
                return False, "回答没有声明所依据的证据编号。"
        raw_results = [item.get("result", {}) for item in evidence_results]
        if self.amount_validator and not self.amount_validator(answer, raw_results):
            return False, "回答中出现了无法从本轮数据库证据核验的金额。"

        import re
        supported_merchants: set[str] = set()
        supported_categories: set[str] = set()

        def collect_entities(value: Any) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if isinstance(item, str) and item.strip():
                        if key in {"merchant", "merchant_name", "counterparty"}:
                            supported_merchants.add(item.strip())
                        elif key in {"category", "subcat"}:
                            supported_categories.add(item.strip())
                    collect_entities(item)
            elif isinstance(value, list):
                for item in value:
                    collect_entities(item)

        for result in raw_results:
            collect_entities(result)

        merchant_mentions = re.findall(
            r"(?:商户|商家|店铺|交易对象)[^「“\"]{0,12}[「“\"]([^」”\"]{1,60})[」”\"]",
            answer,
        )
        for merchant in merchant_mentions:
            if merchant.strip() not in supported_merchants:
                return False, f"回答提及的商户「{merchant.strip()}」不在本轮核验结果中。"
        category_mentions = re.findall(
            r"(?:分类|类别|大类|小类)[^「“\"]{0,12}[「“\"]([^」”\"]{1,60})[」”\"]",
            answer,
        )
        for category in category_mentions:
            if category.strip() not in supported_categories:
                return False, f"回答提及的分类「{category.strip()}」不在本轮核验结果中。"
        for evidence_id in re.findall(r"\bE\d{3}\b", answer):
            if evidence_id not in allowed_ids:
                return False, f"回答引用了不存在的证据编号 {evidence_id}。"

        supported_dates: set[str] = set()
        supported_months: set[str] = set()
        supported_counts: set[int] = set()
        supported_percentages: set[float] = set()
        supported_tx_ids: set[int] = set()

        def walk(value: Any, key: str = "") -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    walk(child, str(child_key).lower())
            elif isinstance(value, list):
                for child in value:
                    walk(child, key)
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                if key in {"count", "current_count", "previous_count", "transaction_count", "total_count"}:
                    supported_counts.add(int(value))
                if "percent" in key or key.endswith("_pct"):
                    supported_percentages.add(round(float(value), 1))
                if key in {"id", "tx_id"} and int(value) > 0:
                    supported_tx_ids.add(int(value))
            elif isinstance(value, str):
                if re.fullmatch(r"20\d{2}-\d{2}-\d{2}", value):
                    supported_dates.add(value)
                    supported_months.add(value[:7])
                elif re.fullmatch(r"20\d{2}-(0[1-9]|1[0-2])", value):
                    supported_months.add(value)

        for result in raw_results:
            walk(result)

        for month_text in re.findall(r"20\d{2}-(?:0[1-9]|1[0-2])", answer):
            if month_text not in supported_months:
                return False, f"回答中的月份 {month_text} 不在本轮查询证据范围内。"
        chinese_months = {
            "一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
            "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
        }
        supported_month_numbers = {int(item[-2:]) for item in supported_months}
        supported_years = {item[:4] for item in supported_months}
        for match in re.finditer(
            r"(?:(20\d{2})\s*年\s*)?(十二|十一|十|[一二三四五六七八九]|1[0-2]|0?[1-9])\s*月",
            answer,
        ):
            year, raw_month = match.groups()
            month_number = chinese_months.get(raw_month, int(raw_month) if raw_month.isdigit() else None)
            if month_number not in supported_month_numbers:
                return False, f"回答中的月份 {match.group(0)} 不在本轮查询证据范围内。"
            if year and year not in supported_years:
                return False, f"回答中的年份 {year} 不在本轮查询证据范围内。"
        for date_text in re.findall(r"20\d{2}-\d{2}-\d{2}", answer):
            if date_text not in supported_dates:
                return False, f"回答中的日期 {date_text} 不在本轮查询证据中。"
        for count_text in re.findall(r"(?<!\d)(\d+)\s*笔", answer):
            if int(count_text) not in supported_counts:
                return False, f"回答中的交易笔数 {count_text} 无法由本轮查询结果核验。"
        for percentage_text in re.findall(r"(?<![\d.])(\d+(?:\.\d+)?)\s*%", answer):
            if round(float(percentage_text), 1) not in supported_percentages:
                return False, f"回答中的比例 {percentage_text}% 无法由本轮查询结果核验。"
        for tx_id_text in re.findall(r"(?:交易\s*)?#\s*(\d+)", answer):
            if int(tx_id_text) not in supported_tx_ids:
                return False, f"回答引用了不存在于本轮证据中的交易 ID #{tx_id_text}。"
        return True, None

    @staticmethod
    def _has_empty_collection_without_scope_data(result: dict[str, Any]) -> bool:
        # Explicit count=0 is valid evidence for an empty search, but it is not a
        # numeric aggregate and must never be turned into a statement of zero spend.
        if result.get("count") == 0 and isinstance(result.get("transactions"), list):
            return True
        for key in ("categories", "merchants", "transactions"):
            value = result.get(key)
            if isinstance(value, list) and not value:
                if key == "transactions" and result.get("count") not in (None, 0):
                    continue
                if result.get("month") or result.get("keyword") or result.get("date") or key == "transactions":
                    return True
        return False

    @staticmethod
    def _scope(arguments: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        keys = (
            "month", "compare_month", "date", "date_from", "date_to",
            "start_date", "end_date", "direction", "keyword", "category",
            "subcat", "pay_method", "merchant", "counterparty", "tx_id",
        )
        scope = {key: arguments[key] for key in keys if arguments.get(key) not in (None, "")}
        for key in ("month", "previous_month", "date", "direction", "keyword", "start_date", "end_date"):
            if key in result and key not in scope:
                scope[key] = result[key]
        return scope

    @staticmethod
    def _transaction_ids(result: dict[str, Any]) -> list[int]:
        ids: set[int] = set()
        rows = result.get("transactions")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    value = row.get("id", row.get("tx_id"))
                    if isinstance(value, int) and value > 0:
                        ids.add(value)
        for key in ("tx_id",):
            value = result.get(key)
            if isinstance(value, int) and value > 0:
                ids.add(value)
        return sorted(ids)[:200]

    @staticmethod
    def _summary(result: dict[str, Any], state: str) -> dict[str, Any]:
        summary: dict[str, Any] = {"state": state}
        for key in ("month", "previous_month", "date", "keyword", "count", "history_sample_count", "ledger_complete"):
            if key in result and isinstance(result[key], (str, int, float, bool, type(None))):
                summary[key] = result[key]
        for key in ("transactions", "categories", "merchants", "large_spending_candidates", "possible_duplicate_candidates", "category_spikes"):
            if isinstance(result.get(key), list):
                summary[f"{key}_count"] = len(result[key])
        return summary
