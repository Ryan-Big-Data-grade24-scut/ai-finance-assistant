"""
backend/main.py —— FastAPI 入口

原则：本文件【不含任何业务逻辑】。
它只做四件事：注册路由 / 启动建表 / 统一错误响应 / CORS。
看到 if/else 里的业务判断，说明逻辑漏进来了，该挪到 reports/ 或 overrides/。

路由分四类：
    /api/import/*   导入      → importer/
    /api/report/*   只读查询   → reports/
    /api/tx/*       写修正    → overrides.py（唯一入口）
    /api/ledger/*   账本      → ledger/service.py（唯一写入口 /api/ledger/op）

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
from backend import importer as importer_mod                   # noqa: E402
from backend.importer.base import ParseIssues  # noqa: E402
from backend.overrides import ForbiddenFieldError, TxNotFound  # noqa: E402
from backend import schemas as S                              # noqa: E402
from backend.ledger import service as ledger_service          # noqa: E402
from backend.ledger.errors import LedgerError                # noqa: E402

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
    snap = db.health_snapshot()
    return {
        "status": "ok",
        "db": str(db.DB_PATH),
        "transactions": snap["transactions"],
        "batches": snap["batches"],
    }


# ── 账本（第二轮）───────────────────────────────────────────
#  唯一写路径：POST /api/ledger/op
#  每个请求都显式带 book_id —— 不引入「当前账本」这个隐式状态
#  （MCP 没有会话，HTTP 多标签页会互相踩）。

@app.exception_handler(LedgerError)
async def _ledger_error(_req, exc: LedgerError):
    """引擎拒绝了一次操作。响应体 {code, message, detail}。

    message 是**人话** —— 前端直接弹给用户，MCP 直接念给模型听。
    """
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=exc.http_status, content=exc.to_payload())

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
    return {"batches": importer_mod.list_batches()}


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


# ═══════════════════════════════════════════════════════════
#  账本（第二轮 · 账本代数引擎）
#
#  口径提醒：账本的数字和 /api/report/* 的数字**不是一回事**。
#  reports 是平台口径的平铺统计，账本是用户自己分的组。两者不相加。
#
#  写路径只有一个：POST /api/ledger/op
#  每次都带 book_id —— 不引入「当前账本」这种隐式状态。
# ═══════════════════════════════════════════════════════════

@app.get("/api/ledger/books", response_model=S.LedgerBookListResp, tags=["ledger"])
def ledger_books():
    """账本列表。用户定义的若干「阶段」，每阶段一本独立账。"""
    books = ledger_service.list_books()
    out = []
    for b in books:
        st = ledger_service.get_status(b.book_id)
        out.append(S.LedgerBookOut(
            book_id=b.book_id,
            name=b.name,
            date_from=b.date_from,
            date_to=b.date_to,
            member_count=st["total_tx"],
            created_at=b.created_at,
        ))
    return S.LedgerBookListResp(books=out)


@app.post("/api/ledger/books", response_model=S.LedgerTreeResp, tags=["ledger"])
def ledger_create_book(body: S.LedgerBookCreate):
    """建一本账：按区间冻结成员集，生成 M₀（根 + 全部腿的一层平铺树）。"""
    return ledger_service.create_book(
        body.name, body.date_from, body.date_to,
        created_by="ui", note=body.note,
    )


@app.get("/api/ledger/tree", response_model=S.LedgerTreeResp, tags=["ledger"])
def ledger_tree(book_id: str, focus_node_id: str | None = None):
    """整棵树 + 每个节点的聚合值。focus_node_id 只是焦点提示，不裁剪内容。"""
    return ledger_service.get_tree(book_id, focus_node_id)


@app.get("/api/ledger/status", response_model=S.LedgerStatusResp, tags=["ledger"])
def ledger_status(book_id: str):
    """客观状态：多少笔已归位、多少笔待归、不变量是否通过、能不能撤销。"""
    return ledger_service.get_status(book_id)


@app.post("/api/ledger/op", response_model=S.LedgerOpResp, tags=["ledger"])
def ledger_op(body: S.LedgerOpRequest):
    """**唯一写入口**。body.op 是判别联合，按 op 字段选分支。"""
    op = body.op.model_dump()
    return ledger_service.apply_op(body.book_id, op, created_by=body.updated_by)


@app.post("/api/ledger/undo", response_model=S.LedgerOpResp, tags=["ledger"])
def ledger_undo(body: S.LedgerBookRef):
    """撤销一步。靠操作日志里的逆操作，不是靠算术。"""
    return ledger_service.undo(body.book_id)


@app.post("/api/ledger/redo", response_model=S.LedgerOpResp, tags=["ledger"])
def ledger_redo(body: S.LedgerBookRef):
    """重做一步。"""
    return ledger_service.redo(body.book_id)


@app.post("/api/ledger/reset", response_model=S.LedgerOpResp, tags=["ledger"])
def ledger_reset(body: S.LedgerBookRef):
    """回到 M₀ —— 树全 collapse 成「根 + 全部腿」。"""
    return ledger_service.reset_to_m0(body.book_id)
