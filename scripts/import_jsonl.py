"""存量 JSONL 会话归档导入工具。

把旧机制（会话结束时回拉拼装）生成的 archives/{uid}_{date}.jsonl 记录
导入 SQLite 会话库（src/storage.py），使历史会话可继续检索。

用法（在仓库根目录执行）：
    uv run python scripts/import_jsonl.py [--dry-run] [--archive-dir archives] [--db PATH]

- 幂等：按（客户 QQ，周期起点）判断会话是否已导入，重复执行安全；
  消息按 message_id 唯一索引自动去重。
- 角色推断：发送者 QQ == 归档文件所属客户 QQ → customer_message，否则 staff_reply。
"""

import argparse
import glob
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

from src import storage  # noqa: E402  （依赖仓库根目录的 config.yaml）


def load_sessions_from_jsonl(archive_dir: str) -> tuple[dict[tuple[int, float], dict], int]:
    """解析归档目录下所有 JSONL，按（客户，周期起点）聚合会话。"""
    sessions: dict[tuple[int, float], dict] = {}
    skipped = 0
    for filepath in sorted(glob.glob(os.path.join(archive_dir, "*.jsonl"))):
        if os.path.basename(filepath) == "failed_events.jsonl":
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                    meta = msg["_s"]
                    uid = int(meta["uid"])
                    start = float(meta["start"])
                except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                    skipped += 1
                    print(f"  跳过无法解析的行 {filepath}:{line_no}: {e}")
                    continue

                key = (uid, start)
                entry = sessions.get(key)
                if entry is None:
                    entry = {
                        "uid": uid,
                        "start": start,
                        "end": float(meta.get("end", start)),
                        "dur": meta.get("dur"),
                        "msgs": [],
                    }
                    sessions[key] = entry
                entry["msgs"].append({
                    "id": msg.get("id"),
                    "t": msg.get("t"),
                    "u": msg.get("u"),
                    "n": msg.get("n", ""),
                    "msg": msg.get("msg", []),
                })
    return sessions, skipped


def main() -> int:
    parser = argparse.ArgumentParser(description="导入存量 JSONL 会话归档到 SQLite 会话库")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument("--archive-dir", default=None, help="归档目录（默认读取 config.yaml 的 archive_dir）")
    parser.add_argument("--db", default=None, help="会话库文件路径（默认 archives/archive.db）")
    args = parser.parse_args()

    from src.config import ARCHIVE_DIR as configured_dir
    archive_dir = args.archive_dir or configured_dir
    if args.db:
        storage.DB_PATH = args.db

    if not os.path.isdir(archive_dir):
        print(f"归档目录不存在: {archive_dir}")
        return 1

    sessions, skipped = load_sessions_from_jsonl(archive_dir)
    ordered = sorted(sessions.values(), key=lambda s: (s["uid"], s["start"]))
    total_msgs = sum(len(s["msgs"]) for s in ordered)
    print(f"解析完成: {len(ordered)} 个会话周期, {total_msgs} 条消息记录, 跳过 {skipped} 行"
          f" (归档目录: {archive_dir})")

    if args.dry_run:
        for s in ordered:
            print(f"  客户 {s['uid']}: 周期 [{s['start']:.0f} ~ {s['end']:.0f}], {len(s['msgs'])} 条消息")
        print("dry-run 模式，未写库。")
        return 0

    storage.init_db_sync()

    imported_sessions = imported_msgs = dedup_msgs = existing_sessions = 0
    for s in ordered:
        if storage.find_session_id_sync(s["uid"], s["start"]) is not None:
            existing_sessions += 1
            continue
        sid = storage.open_session_sync(s["uid"], s["start"])
        storage.record_event_sync(sid, "session_open", time=s["start"], payload={"imported": True})
        for m in sorted(s["msgs"], key=lambda x: x.get("t") or 0):
            sender_uid = m.get("u")
            role = "customer_message" if sender_uid is not None and int(sender_uid) == s["uid"] else "staff_reply"
            mid = int(m["id"]) if m.get("id") is not None else None
            ok = storage.record_event_sync(
                sid, role,
                time=float(m.get("t") or 0),
                actor_uid=int(sender_uid) if sender_uid is not None else None,
                message_id=mid,
                payload={"n": m.get("n", ""), "msg": m.get("msg", []), "source": "import"},
            )
            if ok:
                imported_msgs += 1
            else:
                dedup_msgs += 1
        storage.close_session_by_window_sync(sid, s["start"], s["end"], "imported")
        imported_sessions += 1

    print(f"导入完成: 新增会话 {imported_sessions} 个（已存在跳过 {existing_sessions} 个），"
          f"写入消息 {imported_msgs} 条（去重跳过 {dedup_msgs} 条）")
    print(f"会话库: {storage.get_db_path()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
