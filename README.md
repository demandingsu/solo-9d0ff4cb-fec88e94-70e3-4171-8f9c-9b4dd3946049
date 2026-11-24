# 电影资料馆放映档案（FastAPI + SQLite）

把乱序到达、带整数版次的放映清单并入可查的放映档案。每份清单标明版次与覆盖起止日期
（含首尾两天），列出范围内的全部场次；清单可跨年、可为空。

## 业务规则

1. **最高版次优先**：每一天以覆盖当日的清单中版次最高的一份为准。
2. **迟到旧版补齐**：旧版清单晚到时，仍可补齐新版**覆盖范围之外**的日期；
   落入新版范围内的旧场次一律撤下（新版范围内未列的场次即视为当天无放映）。
3. **同版冲突整份退回**：同版清单与已入库清单在交叠日期内内容有出入时，
   返回 `409`，整份不入库，原档案保持不变；完全相同的重复提交按幂等处理。
4. **空日期用区间表达**：空清单只存覆盖区间，不逐日展开；查询结果中的
   “确认无放映”和“尚未收到清单”日期都以合并后的闭区间返回。
5. **快照一致性**：查询在单条 SQLite 快照事务内完成，整份结果来自同一个已提交状态，
   不会与并发入库出现混合态；入库在单条写事务内完成校验与插入，冲突整体回滚。

## 数据模型（不逐日展开）

- `manifests(id, version, start_date, end_date, accepted_at)`：清单与覆盖区间
- `screenings(id, manifest_id, date, session_no, title)`：清单内实际场次，
  同日场次号由库内唯一约束保证唯一

空日期没有任何行——它是清单覆盖区间挖去有场次日期后的剩余区间。

## 用 Docker Compose 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs ，在 Swagger UI 里直接录入和查档
```

SQLite 数据文件位于命名卷 `archive-data` 内的 `/data/archive.db`（WAL 模式）。

## 本地直接运行（不用 Docker）

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload
# http://localhost:8000/docs
```

## API

### `POST /manifests` 录入清单

```json
{
  "version": 2,
  "start_date": "2026-01-05",
  "end_date": "2026-01-10",
  "screenings": [
    {"date": "2026-01-05", "session_no": 1, "title": "新版片A"},
    {"date": "2026-01-05", "session_no": 2, "title": "新版片B"}
  ]
}
```

空清单：`"screenings": []`。
成功返回 `201`（重复提交返回 `201` 且 `status=duplicate`）；同版交叠冲突返回 `409`；
入参非法（反向日期、场次越界、同日场次号重复、空白片名）返回 `422`。

### `GET /schedule?start_date=...&end_date=...` 查档

```json
{
  "query_start": "2026-01-01",
  "query_end": "2026-01-10",
  "screenings": [
    {"date": "2026-01-05", "session_no": 1, "title": "新版片A", "version": 2}
  ],
  "confirmed_no_screening": [
    {"start_date": "2026-01-06", "end_date": "2026-01-10"}
  ],
  "no_manifest_yet": []
}
```

- `screenings`：当前生效场次，按日期、场次号排序，带生效版次；
- `confirmed_no_screening`：已被清单覆盖、但生效清单确认当天无放映的日期段；
- `no_manifest_yet`：还没有任何清单覆盖到的日期段。

另有 `GET /manifests`（已入库清单）与 `GET /healthz`（健康检查）。

## 测试

```bash
pip install pytest httpx
pytest -q
```

覆盖：迟到旧版补空档、旧场在新版范围内撤下、空清单覆盖、同版冲突整份退回且原档不变、
幂等重复提交、同版不相交共存、跨年、入参校验、并发读写快照一致性、区间纯函数。
