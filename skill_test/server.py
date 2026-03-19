"""
Web 平台服务器 — FastAPI + WebSocket 实时推送。

路由:
  GET  /                    仪表盘页面
  GET  /api/status          服务状态
  GET  /api/config          当前配置
  POST /api/config/load     加载配置文件
  GET  /api/skills          可用 Skills
  GET  /api/presets         预设 Skills
  POST /api/discover        发现 Skills
  POST /api/runs            启动测试
  GET  /api/runs            运行列表
  GET  /api/runs/{id}       运行详情
  GET  /api/history/stats   历史统计
  GET  /api/history/trend   趋势数据
  WS   /ws                  实时事件流
"""

import asyncio
import json
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from .config import load_config, build_default_config, validate_config
from .log import setup_logging, get_logger
from .models import AppConfig, RunSession, ReportFormat

log = get_logger("server")

try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.middleware.cors import CORSMiddleware
    from pydantic import BaseModel
    FASTAPI_AVAILABLE = True
except ImportError:
    FASTAPI_AVAILABLE = False


def _require_fastapi():
    if not FASTAPI_AVAILABLE:
        raise RuntimeError(
            "Web 平台需要安装 FastAPI: pip install fastapi uvicorn"
        )


if FASTAPI_AVAILABLE:
    class ConfigLoadRequest(BaseModel):
        path: str

    class DiscoverRequest(BaseModel):
        path: str

    class RunRequest(BaseModel):
        mode: str = "auto"
        repo_path: str = ""
        work_dir: str = ""
        commit: bool = False
        push: bool = False
        task_ids: List[str] = []
        skill_names: List[str] = []


class _WSManager:
    """WebSocket 连接管理器。"""

    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)

    async def broadcast(self, data: dict):
        dead = []
        for ws in self._connections:
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._connections)


class PlatformState:
    """全局平台状态。"""

    def __init__(self):
        self.config: AppConfig = build_default_config()
        self.config_path: str = ""
        self.sessions: dict[str, RunSession] = {}
        self.active_run_id: Optional[str] = None
        self._event_queue: queue.Queue = queue.Queue()

    def load(self, path: str):
        self.config = load_config(path)
        self.config_path = path

    @property
    def is_running(self) -> bool:
        return self.active_run_id is not None


def create_app() -> FastAPI:
    _require_fastapi()

    app = FastAPI(title="AI Skill Test Platform", version="3.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    ws_mgr = _WSManager()
    state = PlatformState()

    # ── 页面 ──────────────────────────────────────────────────────────

    @app.get("/", response_class=HTMLResponse)
    async def dashboard():
        html_path = Path(__file__).parent / "dashboard.html"
        return HTMLResponse(html_path.read_text(encoding="utf-8"))

    # ── 状态 API ─────────────────────────────────────────────────────

    @app.get("/api/status")
    async def status():
        return {
            "version": "3.0.0",
            "running": state.is_running,
            "active_run": state.active_run_id,
            "ws_clients": ws_mgr.count,
            "config_loaded": bool(state.config_path),
            "config_path": state.config_path,
            "tasks_count": len(state.config.tasks),
            "skills_count": len(state.config.skills),
            "sessions_count": len(state.sessions),
        }

    @app.get("/api/config")
    async def get_config():
        c = state.config
        return {
            "path": state.config_path,
            "cli": {"command": c.cli.command, "timeout": c.cli.timeout},
            "git": {"base_branch": c.git.base_branch, "cleanup": c.git.cleanup_on_finish},
            "retry": {"max_retries": c.retry.max_retries, "retry_on_timeout": c.retry.retry_on_timeout},
            "max_workers": c.max_workers,
            "output_dir": c.output_dir,
            "tasks": [{"id": t.id, "name": t.name, "prompt": t.prompt[:200]} for t in c.tasks],
            "skills": [{"name": s.name, "type": "baseline" if s.is_baseline else ("file" if s.skill_file else "inline")} for s in c.skills],
        }

    @app.post("/api/config/load")
    async def load_config_api(req: ConfigLoadRequest):
        try:
            state.load(req.path)
            warnings = validate_config(state.config)
            return {
                "success": True,
                "tasks": len(state.config.tasks),
                "skills": len(state.config.skills),
                "warnings": warnings,
            }
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)

    # ── Skills API ───────────────────────────────────────────────────

    @app.get("/api/skills")
    async def get_skills():
        skills = []
        for s in state.config.skills:
            skills.append({
                "name": s.name,
                "type": "baseline" if s.is_baseline else ("file" if s.skill_file else "inline"),
                "source": s.skill_file or (s.system_prompt or "")[:100],
                "ref_count": len(s.ref_files),
            })
        return {"skills": skills}

    @app.get("/api/presets")
    async def get_presets():
        from .discovery import list_presets
        return {
            "presets": [
                {"name": n, "prompt": (s.system_prompt or "")[:100]}
                for n, s in list_presets().items()
            ]
        }

    @app.post("/api/discover")
    async def discover(req: DiscoverRequest):
        from .discovery import discover_skills
        skills = discover_skills(req.path)
        return {
            "path": req.path,
            "found": len(skills),
            "skills": [
                {"name": s.name, "file": s.skill_file, "refs": len(s.ref_files)}
                for s in skills
            ],
        }

    # ── 执行 API ─────────────────────────────────────────────────────

    @app.post("/api/runs")
    async def start_run(req: RunRequest):
        if state.is_running:
            return JSONResponse({"error": "已有测试在运行中"}, status_code=409)

        session = RunSession(
            config_name=state.config_path,
            mode=req.mode,
            repo_path=req.repo_path,
            total_tasks=len(state.config.tasks) * len(state.config.skills),
        )
        state.sessions[session.id] = session
        state.active_run_id = session.id

        thread = threading.Thread(
            target=_run_in_background,
            args=(state, ws_mgr, session.id, req),
            daemon=True,
        )
        thread.start()

        return {"run_id": session.id, "total": session.total_tasks}

    @app.get("/api/runs")
    async def list_runs():
        runs = sorted(state.sessions.values(), key=lambda s: s.started_at, reverse=True)
        return {"runs": [s.to_dict() for s in runs[:50]]}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        session = state.sessions.get(run_id)
        if not session:
            return JSONResponse({"error": "不存在"}, status_code=404)
        return session.to_dict()

    # ── 历史 API ─────────────────────────────────────────────────────

    @app.get("/api/history/stats")
    async def history_stats():
        try:
            from .history import HistoryDB
            db_path = Path(state.config.output_dir) / "history.db"
            if not db_path.exists():
                return {"stats": [], "total": 0}
            with HistoryDB(db_path) as db:
                return {"stats": db.skill_stats(), "total": db.total_runs}
        except Exception as e:
            return {"error": str(e), "stats": [], "total": 0}

    @app.get("/api/history/trend/{skill}")
    async def history_trend(skill: str, limit: int = 30):
        try:
            from .history import HistoryDB
            db_path = Path(state.config.output_dir) / "history.db"
            if not db_path.exists():
                return {"data": []}
            with HistoryDB(db_path) as db:
                return {"data": db.trend(skill, limit=limit)}
        except Exception as e:
            return {"error": str(e), "data": []}

    # ── WebSocket ────────────────────────────────────────────────────

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await ws_mgr.connect(ws)
        log.info("WebSocket 客户端连接 (总计: %d)", ws_mgr.count)
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=30)
                    data = json.loads(msg)
                    if data.get("type") == "ping":
                        await ws.send_json({"type": "pong"})
                except asyncio.TimeoutError:
                    await ws.send_json({"type": "heartbeat"})
        except WebSocketDisconnect:
            pass
        except Exception:
            pass
        finally:
            ws_mgr.disconnect(ws)
            log.info("WebSocket 客户端断开 (剩余: %d)", ws_mgr.count)

    return app


def _run_in_background(state: PlatformState, ws_mgr: _WSManager, run_id: str, req):
    """在后台线程中执行测试，通过事件总线推送进度。"""
    from .runner import TestRunner
    from .reporter import save_report

    config = state.config
    session = state.sessions[run_id]
    loop = asyncio.new_event_loop()

    try:
        filtered_tasks = config.tasks
        if req.task_ids:
            filtered_tasks = [t for t in config.tasks if t.id in req.task_ids]

        filtered_skills = config.skills
        if req.skill_names:
            filtered_skills = [s for s in config.skills if s.name in req.skill_names]

        session.total_tasks = len(filtered_tasks) * len(filtered_skills)

        runner = TestRunner(
            config,
            repo_path=req.repo_path or None,
            work_dir=req.work_dir or None,
            enable_progress=False,
            enable_history=True,
        )

        def _broadcast_sync(data):
            data["run_id"] = run_id
            asyncio.run_coroutine_threadsafe(ws_mgr.broadcast(data), loop)

        runner.events.on("task_registered", _broadcast_sync)
        runner.events.on("task_started", _broadcast_sync)
        runner.events.on("task_completed", lambda d: (
            _broadcast_sync(d),
            _update_session(session, d),
        ))

        _broadcast_sync({"event": "run_started", "total": session.total_tasks})

        results = runner.run(
            mode=req.mode,
            tasks=filtered_tasks,
            skills=filtered_skills,
            commit=req.commit,
            push=req.push,
        )

        session.status = "completed"
        session.completed_at = datetime.now().isoformat()

        try:
            save_report(results, config.output_dir, config.report_formats)
        except Exception:
            pass

        ok = sum(1 for r in results if r.success)
        _broadcast_sync({
            "event": "run_complete",
            "total": len(results),
            "success": ok,
            "failed": len(results) - ok,
        })

    except Exception as e:
        log.error("测试执行失败: %s", e)
        session.status = "failed"
        try:
            asyncio.run_coroutine_threadsafe(
                ws_mgr.broadcast({"event": "run_error", "run_id": run_id, "error": str(e)}),
                loop,
            )
        except Exception:
            pass
    finally:
        state.active_run_id = None
        loop.close()


def _update_session(session: RunSession, data: dict):
    session.completed_tasks += 1
    result_data = data.get("result")
    if result_data:
        from .models import TaskResult, TaskStatus
        r = TaskResult(
            task_id=result_data.get("task_id", ""),
            task_name=result_data.get("task_name", ""),
            skill_name=result_data.get("skill_name", ""),
            status=TaskStatus(result_data.get("status", "failed")),
            duration=result_data.get("duration", 0),
            change_summary=result_data.get("change_summary"),
        )
        session.results.append(r)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8080,
    config_path: Optional[str] = None,
    log_level: str = "info",
):
    """启动 Web 平台。"""
    _require_fastapi()
    import uvicorn

    setup_logging("DEBUG" if log_level == "debug" else "INFO")
    app = create_app()

    if config_path:
        app_state = None
        for route in app.routes:
            if hasattr(route, 'endpoint'):
                pass
        log.info("配置文件: %s", config_path)

    print(f"\n  AI Skill Test Platform v3.0.0")
    print(f"  http://{host}:{port}")
    print(f"  按 Ctrl+C 停止\n")

    uvicorn.run(app, host=host, port=port, log_level=log_level)
