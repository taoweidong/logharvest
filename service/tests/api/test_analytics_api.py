"""任务统计（§8）、跨版本规则索引（§9）与作业状态筛选（§10）。"""

from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlmodel import Session

from src.domain.models.entities import DataSource, FileTask, Job, Match, RuleSet, utcnow
from src.domain.rules.models import (
    KeywordMatch,
    RegexMatch,
    Rule,
    RuleSetConfig,
    Threshold,
)

WINDOW_RULE_SET_ID = 1


def _config(name: str, *rules: Rule) -> dict:
    # mode="json"：threshold.value 是 Decimal，JSON 列的默认编码器不接受，
    # 生产路径（POST /api/rule-sets）走的也是 Pydantic 的 JSON 序列化，这里必须同形。
    return RuleSetConfig(name=name, rules=list(rules)).model_dump(mode="json")


def _match(job_id: int, file_task_id: int, rule_id: str, version: int = 1) -> Match:
    return Match(
        job_id=job_id,
        file_task_id=file_task_id,
        rule_id=rule_id,
        rule_set_id=WINDOW_RULE_SET_ID,
        rule_version=version,
        source_path=f"/nas/{rule_id}.zip",
        member_path="app.log",
        file_name="app.log",
        line_no=1,
        raw_line="ERROR boom",
    )


def _seed(session: Session) -> None:
    """窗口内一个成功作业（两个文件、三条命中）+ 窗口外一个失败作业 + 两个规则版本。

    插入严格按外键依赖顺序分批提交：SQLite 默认不检查外键，一把 add_all 也能过，
    但 PostgreSQL 立即检查，同一批 flush 里 jobs 可能先于 rule_sets 落库而报 FK 违例。
    """
    now = utcnow()
    session.add_all(
        [
            DataSource(name="nas-a", root_path="/nas/a"),
            DataSource(name="nas-b", root_path="/nas/b"),
        ]
    )
    session.commit()
    session.add(
        RuleSet(
            name="app",
            version=1,
            config=_config(
                "app",
                Rule(id="err", severity="error", match=KeywordMatch(any=["ERROR"])),
                Rule(
                    id="slow",
                    severity="warn",
                    match=RegexMatch(pattern=r"cost=(?P<cost>\d+)"),
                    threshold=Threshold(field="cost", op="gt", value=Decimal("100")),
                ),
            ),
        )
    )
    session.commit()
    session.add(
        RuleSet(
            name="app",
            version=2,
            config=_config(
                "app",
                Rule(id="err", severity="error", match=KeywordMatch(any=["ERROR"])),
                Rule(id="pct", severity="info", match=KeywordMatch(any=["50%完成"])),
            ),
        )
    )
    session.commit()
    session.add_all(
        [
            Job(
                name="in-window",
                data_source_id=1,
                source_path="/nas/a/x",
                rule_set_id=WINDOW_RULE_SET_ID,
                status="succeeded",
                created_at=now,
            ),
            Job(
                name="out-window",
                data_source_id=2,
                source_path="/nas/b/y",
                rule_set_id=WINDOW_RULE_SET_ID,
                status="failed",
                created_at=now - timedelta(days=40),
            ),
        ]
    )
    session.commit()
    session.add_all(
        [
            FileTask(
                job_id=1,
                source_path="/nas/a/1.zip",
                file_name="1.zip",
                size_bytes=1000,
                status="succeeded",
            ),
            FileTask(
                job_id=1,
                source_path="/nas/a/2.zip",
                file_name="2.zip",
                size_bytes=2000,
                status="failed",
            ),
            FileTask(
                job_id=2,
                source_path="/nas/b/3.zip",
                file_name="3.zip",
                size_bytes=9999,
                status="succeeded",
            ),
        ]
    )
    session.commit()
    session.add_all(
        [_match(1, 1, "err"), _match(1, 1, "err"), _match(1, 2, "slow"), _match(2, 3, "err")]
    )
    session.commit()


def test_job_stats_counts_only_window_jobs_and_their_files(
    client, auth_headers, engine: Engine
) -> None:
    with Session(engine) as session:
        _seed(session)

    body = client.get("/api/stats/jobs?days=14", headers=auth_headers).json()

    totals = body["totals"]
    assert body["days"] == 14 and body["data_source_id"] is None
    assert totals["jobs"] == 1 and totals["succeeded"] == 1
    # 窗口外作业的文件与命中不得混进来，否则统计口径就是错的
    assert totals["files_total"] == 2 and totals["bytes_total"] == 3000
    assert totals["file_succeeded"] == 1 and totals["file_failed"] == 1
    assert totals["matched_rows_total"] == 3

    daily = body["daily"]
    assert [row["date"] for row in daily] == [utcnow().date().isoformat()]
    assert daily[0]["jobs_created"] == 1
    assert daily[0]["files_succeeded"] == 1 and daily[0]["files_failed"] == 1
    assert daily[0]["matched_rows"] == 3 and daily[0]["bytes_total"] == 3000

    top = {row["rule_id"]: row for row in body["top_rules"]}
    assert top["err"]["hits"] == 2 and top["err"]["jobs"] == 1
    assert top["slow"]["hits"] == 1 and top["slow"]["jobs"] == 1


def test_job_stats_data_source_narrows_the_window(client, auth_headers, engine: Engine) -> None:
    with Session(engine) as session:
        _seed(session)

    body = client.get("/api/stats/jobs?days=90&data_source_id=2", headers=auth_headers).json()

    assert body["data_source_id"] == 2
    assert body["totals"]["jobs"] == 1 and body["totals"]["failed"] == 1
    assert body["totals"]["bytes_total"] == 9999


def test_job_stats_days_bounds(client, auth_headers) -> None:
    assert client.get("/api/stats/jobs?days=0", headers=auth_headers).status_code == 422
    assert client.get("/api/stats/jobs?days=91", headers=auth_headers).status_code == 422


def test_rule_index_flattens_versions(client, auth_headers, engine: Engine) -> None:
    with Session(engine) as session:
        _seed(session)

    rows = client.get("/api/rules", headers=auth_headers).json()
    # 版本 1 两条 + 版本 2 两条；规则不落新表，靠查询期展开 config
    assert len(rows) == 4
    assert {row["rule_set_name"] for row in rows} == {"app"}
    assert {(row["version"], row["rule_id"]) for row in rows} == {
        (1, "err"),
        (1, "slow"),
        (2, "err"),
        (2, "pct"),
    }
    slow = next(row for row in rows if row["rule_id"] == "slow")
    assert slow["type"] == "regex" and "cost=" in slow["summary"] and "阈值" in slow["summary"]

    only_regex = client.get("/api/rules", params={"type": "regex"}, headers=auth_headers).json()
    assert [row["rule_id"] for row in only_regex] == ["slow"]

    by_keyword = client.get("/api/rules", params={"keyword": "ERROR"}, headers=auth_headers).json()
    assert {row["rule_id"] for row in by_keyword} == {"err"}


def test_rule_index_treats_wildcards_as_literals(client, auth_headers, engine: Engine) -> None:
    with Session(engine) as session:
        _seed(session)

    pct = client.get("/api/rules", params={"keyword": "50%完成"}, headers=auth_headers).json()
    assert [row["rule_id"] for row in pct] == ["pct"]

    # 单个 % 只能命中真的含 % 的规则；若被当通配就会把四条都返回
    lone = client.get("/api/rules", params={"keyword": "%"}, headers=auth_headers).json()
    assert [row["rule_id"] for row in lone] == ["pct"]

    miss = client.get("/api/rules", params={"rule_set_name": "nope"}, headers=auth_headers).json()
    assert miss == []


def test_jobs_status_filter_pushes_down(client, auth_headers, engine: Engine) -> None:
    with Session(engine) as session:
        _seed(session)

    succeeded = client.get("/api/jobs", params={"status": "succeeded"}, headers=auth_headers).json()
    assert [job["name"] for job in succeeded] == ["in-window"]
    assert (
        client.get("/api/jobs", params={"status": "bogus"}, headers=auth_headers).status_code == 422
    )


def test_read_only_roles_can_read_analytics(
    client: TestClient, viewer_headers: dict[str, str]
) -> None:
    """统计与规则索引都挂在读权限码上：viewer 应当可用（不需要写权限）。"""
    assert client.get("/api/stats/jobs", headers=viewer_headers).status_code == 200
    assert client.get("/api/rules", headers=viewer_headers).status_code == 200
    assert (
        client.get("/api/jobs", params={"status": "succeeded"}, headers=viewer_headers).status_code
        == 200
    )
