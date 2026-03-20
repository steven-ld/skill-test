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

from __future__ import annotations

import asyncio
import json
import queue
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, List

from .config import load_config, build_default_config, validate_config, config_to_dict, save_config
from .log import setup_logging, get_logger
from .models import AppConfig, RunSession, ReportFormat, SkillConfig

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

    class ConfigSaveRequest(BaseModel):
        path: str
        data: dict

    class DiscoverRequest(BaseModel):
        path: str

    class RepoDiscoverRequest(BaseModel):
        path: str

    class SelectedSkillRequest(BaseModel):
        name: str
        system_prompt: str = ""
        skill_file: str = ""
        ref_files: List[str] = []
        tool: str = "manual"
        origin: str = ""
        description: str = ""

    class RunRequest(BaseModel):
        mode: str = "auto"
        experiment_mode: str = "task"
        repo_path: str = ""
        repo_paths: List[str] = []
        work_dir: str = ""
        commit: bool = False
        push: bool = False
        include_baseline: bool = True
        use_config_skills: bool = True
        task_ids: List[str] = []
        skill_names: List[str] = []
        selected_skills: List[SelectedSkillRequest] = []


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

    app = FastAPI(title="AI Skill Test Platform", version="4.0.0")
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
            "version": "4.0.0",
            "product_mode": "platform",
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
            "tasks": [
                {
                    "id": t.id,
                    "name": t.name,
                    "prompt": t.prompt[:200],
                    "mode": t.mode,
                    "repo_targets": t.repo_targets,
                }
                for t in c.tasks
            ],
            "skills": [
                {
                    "name": s.name,
                    "type": "baseline" if s.is_baseline else ("file" if s.skill_file else "inline"),
                    "tool": s.tool,
                    "origin": s.origin,
                    "description": s.description,
                    "ref_files": s.ref_files,
                }
                for s in c.skills
            ],
        }

    @app.get("/api/config/files")
    async def list_config_files(root: str = "."):
        root_path = Path(root).resolve()
        files = sorted(root_path.glob("*.y*ml"))
        return {"files": [str(path) for path in files]}

    @app.get("/api/config/file")
    async def get_config_file(path: str):
        try:
            config = load_config(path)
            warnings = validate_config(config)
            return {"success": True, "path": path, "config": config_to_dict(config), "warnings": warnings}
        except Exception as e:
            return JSONResponse({"success": False, "error": str(e)}, status_code=400)

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

    @app.post("/api/config/save")
    async def save_config_api(req: ConfigSaveRequest):
        try:
            config = load_config(overrides=req.data)
            path = save_config(config, req.path)
            warnings = validate_config(config)
            state.config = config
            state.config_path = str(path)
            return {"success": True, "path": str(path), "warnings": warnings}
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
                "ref_files": s.ref_files,
                "tool": s.tool,
                "origin": s.origin,
                "description": s.description,
            })
        return {"skills": skills}

    @app.get("/api/presets")
    async def get_presets():
        from .discovery import list_presets
        return {
            "presets": [
                {
                    "name": n,
                    "prompt": (s.system_prompt or "")[:100],
                    "tool": s.tool,
                    "description": s.description,
                }
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
                {
                    "name": s.name,
                    "file": s.skill_file,
                    "tool": s.tool,
                    "origin": s.origin,
                    "description": s.description,
                    "refs": len(s.ref_files),
                    "ref_files": s.ref_files,
                }
                for s in skills
            ],
        }

    @app.post("/api/repos/discover")
    async def discover_repos(req: RepoDiscoverRequest):
        from .git_manager import discover_git_repos
        repos = discover_git_repos(req.path)
        return {
            "path": req.path,
            "repos": [
                {"name": repo.name, "path": str(repo)}
                for repo in repos
            ],
        }

    # ── 执行 API ─────────────────────────────────────────────────────

    @app.post("/api/runs")
    async def start_run(req: RunRequest):
        from .git_manager import resolve_git_repos

        if state.is_running:
            return JSONResponse({"error": "已有测试在运行中"}, status_code=409)

        filtered_tasks = state.config.tasks
        if req.task_ids:
            filtered_tasks = [t for t in state.config.tasks if t.id in req.task_ids]

        filtered_skills: list[SkillConfig] = []
        if req.use_config_skills:
            if req.skill_names:
                filtered_skills = [s for s in state.config.skills if s.name in req.skill_names]
            else:
                filtered_skills = list(state.config.skills)

        if req.selected_skills:
            filtered_skills.extend(
                SkillConfig(
                    name=s.name,
                    system_prompt=s.system_prompt or None,
                    skill_file=s.skill_file or None,
                    ref_files=s.ref_files,
                    tool=s.tool,
                    origin=s.origin,
                    description=s.description,
                )
                for s in req.selected_skills
            )

        deduped_skills: list[SkillConfig] = []
        seen_skill_names: set[str] = set()
        for skill in filtered_skills:
            if skill.name in seen_skill_names:
                continue
            seen_skill_names.add(skill.name)
            deduped_skills.append(skill)

        filtered_skills = deduped_skills

        if req.include_baseline and "baseline" not in seen_skill_names:
            filtered_skills.insert(
                0,
                SkillConfig(
                    name="baseline",
                    tool="builtin",
                    origin="builtin:baseline",
                    description="普通提示词基线",
                ),
            )

        resolved_repo_paths = list(req.repo_paths)
        if req.repo_path or resolved_repo_paths:
            try:
                resolved_repo_paths = [
                    str(path)
                    for path in resolve_git_repos(
                        req.repo_path or resolved_repo_paths[0],
                        tasks=filtered_tasks,
                        work_dir=req.work_dir or None,
                        explicit_repo_paths=resolved_repo_paths or None,
                    )
                ]
            except Exception as e:
                return JSONResponse({"error": str(e)}, status_code=400)

        session = RunSession(
            config_name=state.config_path,
            mode=req.mode,
            experiment_mode=req.experiment_mode,
            repo_path=", ".join(resolved_repo_paths),
            total_tasks=len(filtered_tasks) * len(filtered_skills),
        )
        state.sessions[session.id] = session
        state.active_run_id = session.id

        thread = threading.Thread(
            target=_run_in_background,
            args=(state, ws_mgr, session.id, req),
            daemon=True,
        )
        thread.start()

        return {
            "run_id": session.id,
            "total": session.total_tasks,
            "experiment_mode": req.experiment_mode,
            "resolved_repo_paths": resolved_repo_paths,
        }

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
    from .git_manager import resolve_git_repos

    session = state.sessions[run_id]

    try:
        config = state.config
        filtered_tasks = config.tasks
        if req.task_ids:
            filtered_tasks = [t for t in config.tasks if t.id in req.task_ids]

        filtered_skills: list[SkillConfig] = []
        if req.use_config_skills:
            if req.skill_names:
                filtered_skills = [s for s in config.skills if s.name in req.skill_names]
            else:
                filtered_skills = list(config.skills)

        if req.selected_skills:
            filtered_skills.extend(
                SkillConfig(
                    name=s.name,
                    system_prompt=s.system_prompt or None,
                    skill_file=s.skill_file or None,
                    ref_files=s.ref_files,
                    tool=s.tool,
                    origin=s.origin,
                    description=s.description,
                )
                for s in req.selected_skills
            )

        deduped_skills: list[SkillConfig] = []
        seen_skill_names: set[str] = set()
        for skill in filtered_skills:
            if skill.name in seen_skill_names:
                continue
            seen_skill_names.add(skill.name)
            deduped_skills.append(skill)
        filtered_skills = deduped_skills

        if req.include_baseline and "baseline" not in seen_skill_names:
            filtered_skills.insert(
                0,
                SkillConfig(
                    name="baseline",
                    tool="builtin",
                    origin="builtin:baseline",
                    description="普通提示词基线",
                ),
            )

        session.total_tasks = len(filtered_tasks) * len(filtered_skills)
        effective_repo_paths: list[str] = list(req.repo_paths)
        if req.repo_path or effective_repo_paths:
            resolved_repos = resolve_git_repos(
                req.repo_path or effective_repo_paths[0],
                tasks=filtered_tasks,
                work_dir=req.work_dir or None,
                explicit_repo_paths=effective_repo_paths or None,
            )
            effective_repo_paths = [str(path) for path in resolved_repos]
            session.repo_path = ", ".join(effective_repo_paths)

        runner = TestRunner(
            config,
            repo_path=effective_repo_paths or None,
            work_dir=req.work_dir or None,
            enable_progress=False,
            enable_history=True,
        )

        def _broadcast_sync(data):
            data["run_id"] = run_id
            asyncio.run(ws_mgr.broadcast(data))

        runner.events.on("task_registered", _broadcast_sync)
        runner.events.on("task_started", _broadcast_sync)
        runner.events.on("task_completed", lambda d: (
            _broadcast_sync(d),
            _update_session(session, d),
        ))

        results = runner.run(
            mode=req.mode,
            experiment_mode=req.experiment_mode,
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

    except Exception as e:
        log.error("测试执行失败: %s", e)
        session.status = "failed"
        try:
            asyncio.run(
                ws_mgr.broadcast({"event": "run_error", "run_id": run_id, "error": str(e)})
            )
        except Exception:
            pass
    finally:
        state.active_run_id = None


def _update_session(session: RunSession, data: dict):
    session.completed_tasks += 1
    result_data = data.get("result")
    if result_data:
        from .models import TaskResult, TaskStatus
        r = TaskResult(
            task_id=result_data.get("task_id", ""),
            task_name=result_data.get("task_name", ""),
            skill_name=result_data.get("skill_name", ""),
            experiment_mode=result_data.get("experiment_mode", "coding"),
            status=TaskStatus(result_data.get("status", "failed")),
            duration=result_data.get("duration", 0),
            change_summary=result_data.get("change_summary"),
            metadata=result_data.get("metadata", {}),
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

    print(f"\n  AI Skill Test Platform v4.0.0")
    print(f"  http://{host}:{port}")
    print(f"  按 Ctrl+C 停止\n")

    uvicorn.run(app, host=host, port=port, log_level=log_level)
