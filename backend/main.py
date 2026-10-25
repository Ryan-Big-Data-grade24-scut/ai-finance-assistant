"""
backend/main.py —— FastAPI 入口

原则：本文件【不含任何业务逻辑】。
它只做四件事：注册路由 / 启动建表 / 统一错误响应 / CORS。
看到 if/else 里的业务判断，说明逻辑漏进来了，该挪到 reports/ 或 overrides/。

路由分三类：
    /api/import/*   导入      → importer/
    /api/report/*   只读查询   → reports/
    /api/tx/*       写修正    → overrides.py（唯一入口）

启动后打开 http://localhost:8000/docs
    所有 11 个接口 + 字段说明 + 可直接 Try it out
"""
from __future__ import annotations

import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware

# 让 `python -m backend.main` 和 `uvicorn backend.main:app` 都能跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend import db, overrides, reports                    # noqa: E402
from backend import overrides as overrides_mod                # noqa: E402
from backend.importer import ImportResult, parse_file, import_file  # noqa: E402
from backend.importer.base import ParseIssues  # noqa: E402
from backend.overrides import ForbiddenFieldError, TxNotFound  # noqa: E402
from backend import schemas as S                              # noqa: E402

# ── 控制台编码容错（Windows 必做）─────────────────────────────
# Windows 控制台默认 GBK。启动期只要有一个 print 碰到 GBK 编不出的字符
# （✓、emoji、全角标点……），就会 UnicodeEncodeError 让整个应用起不来。
# 这里统一兜底：编不出来就替换，绝不让打印把服务干掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:      # 非标准流（被测试捕获等）直接跳过
        pass

app = FastAPI(
    title="AI Finance Assistant",
    description="跨平台账单导入 · 可追溯 preset 报告 · MCP 接入",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 生命周期 ────────────────────────────────────────────────

@app.get("/health", tags=["meta"])
def health() -> dict:
    """存活探针。前端/脚本/部署平台靠它判断后端是否起来了。

    刻意放在 /health 而不是 /api/health —— 它不是业务接口，
    不该出现在 /docs 的业务列表里被当成"第 13 个接口"。
    """
    conn = db.get_conn()
    try:
        total = conn.execute("SELECT COUNT(*) c FROM transactions_raw").fetchone()["c"]
        batches = conn.execute("SELECT COUNT(*) c FROM import_batches").fetchone()["c"]
    finally:
        conn.close()
    return {
        "status": "ok",
        "db": str(db.DB_PATH),
        "transactions": total,
        "batches": batches,
    }

@app.on_event("startup")
def _startup() -> None:
    db.init_db()
    # 用 [OK] 而不是 ✓：Windows 控制台默认 GBK，✓ 编不出来。
    print(f"[OK] 数据库已就绪: {db.DB_PATH}")


# ── 统一错误响应 ────────────────────────────────────────────

@app.exception_handler(ForbiddenFieldError)
async def _forbidden(_req, exc: ForbiddenFieldError):
    """403 是本项目的招牌行为，不是异常。"""
    return _json_error(403, str(exc))


@app.exception_handler(TxNotFound)
async def _not_found(_req, exc: TxNotFound):
    return _json_error(404, str(exc))


@app.exception_handler(RequestValidationError)
async def _validation(_req, exc: RequestValidationError):
    """把「试图修改原始字段」识别出来，返回 403 而不是普通 400。

    OverrideIn 用了 extra="forbid"，所以传 amount 会在 Pydantic 层被拒，
    FastAPI 默认返回 422。这里把它翻译成项目的招牌 403。
    """
    for err in exc.errors():
        loc = err.get("loc", ())
        field = str(loc[-1]) if loc else ""
        if field in overrides_mod.FORBIDDEN_FIELDS:
            return _json_error(403, f"原始字段不可修改: {field}")
    return _json_error(400, exc.errors())


def _json_error(code: int, msg: str):
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=code, content={"detail": msg})


# ═══════════════════════════════════════════════════════════
#  导入
# ═══════════════════════════════════════════════════════════

# 上传文件仅暂存于随机临时目录，绝不依赖用户文件名作为完整路径。
# 将原始文件名保留为 basename，平台识别和导入批次仍可使用原文件名。
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
ALLOWED_EXTENSIONS = {".xlsx", ".csv", ".zip"}


async def _stage_upload(file: UploadFile, target_dir: Path) -> Path:
    name = Path((file.filename or "").replace("\\", "/")).name
    if not name or Path(name).suffix.lower() not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, "请上传 .xlsx、.csv 或 .zip 格式的账单")

    target = target_dir / name
    size = 0
    with target.open("wb") as stream:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "账单文件超过 25 MB 限制")
            stream.write(chunk)
    if size == 0:
        raise HTTPException(400, "上传文件为空")
    return target


@app.post("/api/import/preview", response_model=S.ImportPreviewResp)
async def import_preview(
    file: UploadFile = File(...),
    alipay_password: str | None = Form(None),
):
    """解析但不写库。支付宝加密 ZIP 可通过 multipart 表单传密码。"""
    with TemporaryDirectory(prefix="finance_import_") as temp_dir:
        tmp = await _stage_upload(file, Path(temp_dir))
        try:
            issues = ParseIssues()
            rows = parse_file(tmp, alipay_password=alipay_password, issues=issues)
            if not rows:
                raise HTTPException(400, "解析成功但没有任何有效数据行")

            dates = sorted(r.date for r in rows)
            by_status: dict[str, int] = {}
            by_direction: dict[str, int] = {}
            for r in rows:
                by_status[r.status or "未知"] = by_status.get(r.status or "未知", 0) + 1
                by_direction[r.direction] = by_direction.get(r.direction, 0) + 1

            first = rows[0]
            return S.ImportPreviewResp(
                batch_id="preview",
                platform_id=first.platform_id,
                platform_name=first.source,
                confidence=0.98,
                total=len(rows),
                by_status=by_status,
                by_direction=by_direction,
                date_from=dates[0],
                date_to=dates[-1],
                amount_sum=round(sum(r.amount for r in rows if r.direction == "支出"), 2),
                sample=[r.raw for r in rows[:5]],
                warnings=issues.warnings,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.post("/api/import/commit", response_model=S.ImportCommitResp)
async def import_commit(
    file: UploadFile = File(...),
    alipay_password: str | None = Form(None),
):
    """解析后批量入库，沿用已有事务逻辑和去重机制。"""
    with TemporaryDirectory(prefix="finance_import_") as temp_dir:
        tmp = await _stage_upload(file, Path(temp_dir))
        try:
            r: ImportResult = import_file(tmp, alipay_password=alipay_password)
            return S.ImportCommitResp(
                batch_id=r.batch_id, inserted=r.inserted, skipped=r.skipped,
                failed=r.failed, first_error=r.first_error,
                date_from=r.date_from, date_to=r.date_to,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc


@app.get("/api/import/history")
async def import_history():
    """导入批次记录。"""
    conn = db.get_conn()
    try:
        rows = conn.execute(
            "SELECT b.*, p.name AS platform_name FROM import_batches b "
            "LEFT JOIN platforms p ON p.id = b.platform_id "
            "ORDER BY b.created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    return {"batches": [dict(r) for r in rows]}


# ═══════════════════════════════════════════════════════════
#  报告（全部只读 GET）
# ═══════════════════════════════════════════════════════════

@app.get("/api/report/summary", response_model=S.SummaryResp)
async def rpt_summary(req: S.ReportReq = Depends()):
    return reports.get_summary(req)


@app.get("/api/report/calendar", response_model=S.CalendarResp)
async def rpt_calendar(req: S.ReportReq = Depends()):
    return reports.get_calendar(req)


@app.get("/api/report/trend", response_model=S.TrendResp)
async def rpt_trend(req: S.ReportReq = Depends(), n: int = 5):
    return reports.get_trend(req, n=n)


@app.get("/api/report/by-category", response_model=S.CategoryResp)
async def rpt_category(req: S.ReportReq = Depends()):
    return reports.get_by_category(req)


@app.get("/api/report/by-method", response_model=S.MethodResp)
async def rpt_method(req: S.ReportReq = Depends()):
    return reports.get_by_method(req)


@app.get("/api/report/income-rank", response_model=S.IncomeRankResp)
async def rpt_income_rank(period: str, value: str):
    req = S.ReportReq(type="income", period=period, value=value)
    return reports.get_income_rank(req)


@app.get("/api/report/transactions", response_model=S.PaginatedResp)
async def rpt_transactions(q: S.TxQuery = Depends()):
    return reports.query_transactions(q)


# ═══════════════════════════════════════════════════════════
#  写修正 —— 唯一通往 tx_overrides 的路
# ═══════════════════════════════════════════════════════════

@app.get("/api/tx/{tx_id}", response_model=S.TxDetailResp)
async def tx_detail(tx_id: int):
    """单笔全貌：原始 + 修正并排。"""
    return overrides.get_tx_detail(tx_id)


@app.delete("/api/tx/{tx_id}")
async def tx_delete(tx_id: int):
    """从明细和所有统计中移除交易，同时保留原始凭证用于审计和去重。"""
    overrides.delete_transaction(tx_id, deleted_by="ui")
    return {"tx_id": tx_id, "deleted": True}


@app.patch("/api/tx/{tx_id}/override", response_model=S.OverrideResp)
async def tx_set_override(tx_id: int, data: S.OverrideIn):
    """修改一笔的分类 / 豁免 / 资金来源 / 备注。

    ⚠️ 请求体只接受 6 个修正字段。任何原始字段（amount / tx_time /
       direction …）都会触发 403 —— 这是设计，不是 bug。
    """
    return overrides.set_override(tx_id, data, updated_by="ui")


@app.delete("/api/tx/{tx_id}/override")
async def tx_clear_override(tx_id: int):
    """撤销修正，恢复到完全按原始数据计算。只删 tx_overrides 一行。"""
    overrides.clear_override(tx_id, updated_by="ui")
    return {"tx_id": tx_id, "cleared": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
