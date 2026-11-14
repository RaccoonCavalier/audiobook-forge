"""SQLite 持久化层。

只做三件事：书籍元数据、任务（job）、分段（segment）。
分段状态是断点续跑的基石 —— 进程被杀掉后重启，只补 pending/failed 的段。
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import Book, Job, JobStatus, Segment, SegmentStatus

SCHEMA = """
CREATE TABLE IF NOT EXISTS books (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    author          TEXT,
    language        TEXT,
    source_format   TEXT NOT NULL,
    source_name     TEXT,
    chapters        INTEGER NOT NULL DEFAULT 0,
    blocks          INTEGER NOT NULL DEFAULT 0,
    chars           INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id              TEXT PRIMARY KEY,
    book_id         TEXT NOT NULL,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL DEFAULT 'queued',
    message         TEXT NOT NULL DEFAULT '',
    total           INTEGER NOT NULL DEFAULT 0,
    done            INTEGER NOT NULL DEFAULT 0,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    options_json    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_jobs_book ON jobs(book_id, created_at DESC);

CREATE TABLE IF NOT EXISTS segments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    book_id         TEXT NOT NULL,
    chapter_index   INTEGER NOT NULL,
    chapter_title   TEXT NOT NULL DEFAULT '',
    seq             INTEGER NOT NULL,
    speaker         TEXT,
    voice           TEXT NOT NULL,
    style           TEXT,
    text            TEXT NOT NULL,
    text_hash       TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    audio_path      TEXT,
    duration_ms     INTEGER NOT NULL DEFAULT 0,
    attempts        INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    UNIQUE(book_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_segments_book_status ON segments(book_id, status);
CREATE INDEX IF NOT EXISTS idx_segments_book_seq ON segments(book_id, seq);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=NORMAL")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_schema(self) -> None:
        # journal_mode 是写进数据库文件的持久属性，只需设一次。
        # 每次连接都设会让每个连接都去抢写锁，实测开销变成 6 倍。
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    # ---------------------------------------------------------------- books

    def save_book(self, book: Book) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO books (id, title, author, language, source_format, source_name,
                                   chapters, blocks, chars, created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(id) DO UPDATE SET
                    title=excluded.title, author=excluded.author, language=excluded.language,
                    chapters=excluded.chapters, blocks=excluded.blocks, chars=excluded.chars
                """,
                (
                    book.id, book.title, book.author, book.language,
                    book.source_format, book.source_name,
                    len(book.chapters), book.block_count, book.char_count, book.created_at,
                ),
            )

    def list_books(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*,
                       (SELECT COUNT(*) FROM segments s WHERE s.book_id=b.id AND s.status='done') AS done_segments,
                       (SELECT COUNT(*) FROM segments s WHERE s.book_id=b.id) AS total_segments
                FROM books b ORDER BY b.created_at DESC
                """
            ).fetchall()
        return [dict(r) for r in rows]

    def get_book_meta(self, book_id: str) -> dict | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM books WHERE id=?", (book_id,)).fetchone()
        return dict(row) if row else None

    def delete_book(self, book_id: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM segments WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM jobs WHERE book_id=?", (book_id,))
            conn.execute("DELETE FROM books WHERE id=?", (book_id,))

    # ----------------------------------------------------------------- jobs

    def create_job(self, job: Job) -> Job:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO jobs (id, book_id, status, stage, message, total, done,
                                     created_at, updated_at, options_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (job.id, job.book_id, job.status.value, job.stage, job.message,
                 job.total, job.done, job.created_at, job.updated_at,
                 json.dumps(job.options, ensure_ascii=False)),
            )
        return job

    def update_job(self, job_id: str, **fields) -> None:
        if not fields:
            return
        fields["updated_at"] = time.time()
        assignments = ", ".join(f"{k}=?" for k in fields)
        values = list(fields.values())
        if "status" in fields and isinstance(fields["status"], JobStatus):
            values[values.index(fields["status"])] = fields["status"].value
        with self.connect() as conn:
            conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*values, job_id))

    def get_job(self, job_id: str) -> Job | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._row_to_job(row) if row else None

    def latest_job_for_book(self, book_id: str) -> Job | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM jobs WHERE book_id=? ORDER BY created_at DESC LIMIT 1", (book_id,)
            ).fetchone()
        return self._row_to_job(row) if row else None

    def list_jobs(self, limit: int = 50) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    def unfinished_jobs(self) -> list[Job]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status IN ('pending','running') ORDER BY created_at"
            ).fetchall()
        return [self._row_to_job(r) for r in rows]

    @staticmethod
    def _row_to_job(row: sqlite3.Row) -> Job:
        return Job(
            id=row["id"], book_id=row["book_id"], status=JobStatus(row["status"]),
            stage=row["stage"], message=row["message"], total=row["total"], done=row["done"],
            created_at=row["created_at"], updated_at=row["updated_at"],
            options=json.loads(row["options_json"] or "{}"),
        )

    # ------------------------------------------------------------- segments

    def replace_segments(self, book_id: str, segments: list[Segment]) -> None:
        """重建某本书的分段表（重新分析时调用）。"""
        with self.connect() as conn:
            conn.execute("DELETE FROM segments WHERE book_id=?", (book_id,))
            conn.executemany(
                """INSERT INTO segments (book_id, chapter_index, chapter_title, seq, speaker,
                                         voice, style, text, text_hash, status, audio_path,
                                         duration_ms, attempts, error)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                [
                    (s.book_id, s.chapter_index, s.chapter_title, s.seq, s.speaker,
                     s.voice, s.style, s.text, s.text_hash, s.status.value,
                     s.audio_path, s.duration_ms, s.attempts, s.error)
                    for s in segments
                ],
            )

    def add_segment(self, seg: Segment) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                """INSERT INTO segments (book_id, chapter_index, chapter_title, seq, speaker,
                                         voice, style, text, text_hash, status)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (seg.book_id, seg.chapter_index, seg.chapter_title, seg.seq, seg.speaker,
                 seg.voice, seg.style, seg.text, seg.text_hash, seg.status.value),
            )
            return int(cur.lastrowid)

    def segments_for_book(self, book_id: str, status: SegmentStatus | None = None) -> list[Segment]:
        query = "SELECT * FROM segments WHERE book_id=?"
        params: list = [book_id]
        if status is not None:
            query += " AND status=?"
            params.append(status.value)
        query += " ORDER BY seq"
        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def pending_segments(
        self, book_id: str, chapters: list[int] | None = None
    ) -> list[Segment]:
        """待合成的分片。chapters 为 None 表示全书，否则只取指定章节（0 基下标）。"""
        query = (
            "SELECT * FROM segments WHERE book_id=? "
            "AND status IN ('pending','failed','running')"
        )
        params: list = [book_id]
        if chapters:
            placeholders = ",".join("?" * len(chapters))
            query += f" AND chapter_index IN ({placeholders})"
            params.extend(int(c) for c in chapters)
        query += " ORDER BY seq"

        with self.connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_segment(r) for r in rows]

    def next_pending_segment(self, book_id: str) -> Segment | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM segments WHERE book_id=? AND status IN ('pending','running') ORDER BY seq LIMIT 1",
                (book_id,),
            ).fetchone()
        return self._row_to_segment(row) if row else None

    def reset_running(self) -> int:
        """进程崩溃后，把卡在 running 的段放回 pending。"""
        with self.connect() as conn:
            cur = conn.execute("UPDATE segments SET status='pending' WHERE status='running'")
            return cur.rowcount

    def mark_segment(
        self,
        segment_id: int,
        status: SegmentStatus,
        *,
        audio_path: str | None = None,
        duration_ms: int = 0,
        error: str | None = None,
        bump_attempts: bool = False,
        text_hash: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """UPDATE segments
                   SET status=?, audio_path=COALESCE(?, audio_path),
                       duration_ms=CASE WHEN ? > 0 THEN ? ELSE duration_ms END,
                       error=?, attempts=attempts + ?, text_hash=COALESCE(?, text_hash)
                   WHERE id=?""",
                (status.value, audio_path, duration_ms, duration_ms, error,
                 1 if bump_attempts else 0, text_hash, segment_id),
            )

    def segment_stats(self, book_id: str) -> dict[str, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM segments WHERE book_id=? GROUP BY status",
                (book_id,),
            ).fetchall()
        stats = {s.value: 0 for s in SegmentStatus}
        for row in rows:
            stats[row["status"]] = row["n"]
        stats["total"] = sum(v for k, v in stats.items() if k != "total")
        return stats

    def chapter_segment_stats(self, book_id: str) -> dict[int, dict[str, int]]:
        """每一章的分片总数与已完成数，供界面按需勾选时展示。"""
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT chapter_index,
                          COUNT(*) AS total,
                          SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                          SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) AS failed
                   FROM segments WHERE book_id=? GROUP BY chapter_index""",
                (book_id,),
            ).fetchall()
        return {
            int(r["chapter_index"]): {
                "total": int(r["total"]),
                "done": int(r["done"] or 0),
                "failed": int(r["failed"] or 0),
            }
            for r in rows
        }

    @staticmethod
    def _row_to_segment(row: sqlite3.Row) -> Segment:
        return Segment(
            id=row["id"], book_id=row["book_id"], chapter_index=row["chapter_index"],
            chapter_title=row["chapter_title"], seq=row["seq"], speaker=row["speaker"],
            voice=row["voice"], style=row["style"], text=row["text"],
            text_hash=row["text_hash"], status=SegmentStatus(row["status"]),
            audio_path=row["audio_path"], duration_ms=row["duration_ms"],
            attempts=row["attempts"], error=row["error"],
        )
