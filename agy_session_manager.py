#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Antigravity Session Manager (GUI + CLI)
Утилита для выгрузки (экспорта) и заливки (импорта) сессий Google Antigravity.

Хранилище сессий Antigravity (~/.gemini/antigravity):
  - conversation_summaries.db     (таблица conversation_summaries + raw_summary protobuf)
  - agyhub_summaries_proto.pb     (protobuf-карта всех raw_summary для отображения в UI)
  - conversations/<id>.db         (SQLite база шагов и метаданных траектории)
  - brain/<id>/                   (артефакты, медиа, логи transcript.jsonl)
  - annotations/<id>.pbtxt        (заголовок и время последнего просмотра)
  - chapter_titles/<id>.pb        (оглавления сессии)
"""

from __future__ import annotations

import argparse
import base64
import datetime
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import tkinter as tk
from tkinter import filedialog, messagebox, ttk


DEFAULT_AGY_DIR = Path.home() / ".gemini" / "antigravity"

SUMMARY_COLUMNS = [
    "conversation_id",
    "title",
    "preview",
    "step_count",
    "last_modified_time",
    "workspace_uris",
    "status",
    "source",
    "project_id",
    "agent_name",
    "parent_conversation_id",
    "nesting_depth",
    "battle_id",
    "winning_conversation_id",
    "not_fully_idle",
    "killed",
    "last_user_input_time",
    "last_user_input_step_index",
    "app_data_dir",
    "raw_summary",
    "group_id",
]

# Расширения уже сжатых файлов, которые не имеет смысла повторно сжимать через DEFLATE
ALREADY_COMPRESSED_EXTS: Set[str] = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico",
    ".mp4", ".mkv", ".webm", ".avi", ".mov", ".flv",
    ".mp3", ".ogg", ".wav", ".aac", ".flac",
    ".zip", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".zst",
    ".whl", ".jar", ".apk", ".pdf"
}


# ---------------------------------------------------------------------------
# Protobuf helpers for agyhub_summaries_proto.pb
# ---------------------------------------------------------------------------

def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while n >= 0x80:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n)
    return bytes(out)


def encode_agyhub_entry(conversation_id: str, raw_summary: bytes) -> bytes:
    """
    Кодирует запись карты map<string, Summary> для agyhub_summaries_proto.pb:
    field 1 (repeated message):
      field 1 (string): conversation_id
      field 2 (bytes):  raw_summary
    """
    key_bytes = conversation_id.encode("utf-8")
    entry = (
        b"\x0a" + _encode_varint(len(key_bytes)) + key_bytes
        + b"\x12" + _encode_varint(len(raw_summary)) + raw_summary
    )
    return b"\x0a" + _encode_varint(len(entry)) + entry


def rebuild_agyhub_proto(agy_dir: Path) -> int:
    """
    Пересобирает agyhub_summaries_proto.pb на основе актуального содержимого
    conversation_summaries.db. Возвращает количество записанных сессий.
    """
    db_path = agy_dir / "conversation_summaries.db"
    pb_path = agy_dir / "agyhub_summaries_proto.pb"
    if not db_path.exists():
        return 0

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        chunks: List[bytes] = []
        count = 0
        for cid, raw in cur.execute(
            "SELECT conversation_id, raw_summary FROM conversation_summaries"
        ):
            if cid and raw:
                chunks.append(encode_agyhub_entry(cid, bytes(raw)))
                count += 1
    finally:
        conn.close()

    if chunks:
        tmp_path = pb_path.with_suffix(".pb.tmp")
        tmp_path.write_bytes(b"".join(chunks))
        tmp_path.replace(pb_path)
    return count


# ---------------------------------------------------------------------------
# Data models & Helpers
# ---------------------------------------------------------------------------

def format_size(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} Б"
    val = float(num_bytes)
    for unit in ["КБ", "МБ", "ГБ"]:
        val /= 1024.0
        if val < 1024.0 or unit == "ГБ":
            return f"{val:.1f} {unit}"
    return f"{val:.1f} ГБ"


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


@dataclass
class SessionRecord:
    conversation_id: str
    title: str
    preview: str
    step_count: int
    last_modified_time: str
    workspace_uris: str
    status: str
    project_id: str
    agent_name: str
    parent_conversation_id: str
    nesting_depth: int
    raw_row: Dict[str, Any] = field(default_factory=dict)
    children: List["SessionRecord"] = field(default_factory=list)
    db_size: int = 0
    brain_size: int = 0
    annotation_title: str = ""

    @property
    def display_title(self) -> str:
        t = (self.title or self.annotation_title or self.preview or "").strip()
        if not t:
            return f"[Без названия] ({self.conversation_id[:8]})"
        return t.replace("\n", " ")[:90]

    @property
    def total_size(self) -> int:
        own = self.db_size + self.brain_size
        return own + sum(c.db_size + c.brain_size for c in self.children)

    @property
    def short_date(self) -> str:
        if not self.last_modified_time:
            return "—"
        return self.last_modified_time[:19].replace("T", " ")


# ---------------------------------------------------------------------------
# Core Session Store Engine
# ---------------------------------------------------------------------------

class AntigravitySessionStore:
    def __init__(self, agy_dir: Path = DEFAULT_AGY_DIR):
        self.agy_dir = Path(agy_dir)

    @property
    def summaries_db(self) -> Path:
        return self.agy_dir / "conversation_summaries.db"

    def is_valid(self) -> bool:
        return self.agy_dir.exists() and (
            self.summaries_db.exists() or (self.agy_dir / "conversations").exists()
        )

    def _read_annotation_title(self, cid: str) -> str:
        ann_file = self.agy_dir / "annotations" / f"{cid}.pbtxt"
        if not ann_file.exists():
            return ""
        try:
            text = ann_file.read_text(encoding="utf-8", errors="replace")
            if 'title:"' in text:
                start = text.index('title:"') + 7
                end = text.index('"', start)
                return text[start:end]
        except Exception:
            pass
        return ""

    def list_sessions(self) -> List[SessionRecord]:
        """Возвращает список корневых сессий с привязанными дочерними сабагентами."""
        records_by_id: Dict[str, SessionRecord] = {}

        if self.summaries_db.exists():
            conn = sqlite3.connect(str(self.summaries_db))
            conn.row_factory = sqlite3.Row
            try:
                cur = conn.cursor()
                for row in cur.execute("SELECT * FROM conversation_summaries"):
                    d = dict(row)
                    cid = d["conversation_id"]
                    raw_b64 = (
                        base64.b64encode(d["raw_summary"]).decode("ascii")
                        if d.get("raw_summary")
                        else ""
                    )
                    serializable_row = {k: v for k, v in d.items() if k != "raw_summary"}
                    serializable_row["raw_summary_b64"] = raw_b64

                    db_file = self.agy_dir / "conversations" / f"{cid}.db"
                    db_sz = db_file.stat().st_size if db_file.exists() else 0
                    brain_sz = dir_size(self.agy_dir / "brain" / cid)
                    ann_title = self._read_annotation_title(cid)

                    rec = SessionRecord(
                        conversation_id=cid,
                        title=d.get("title") or "",
                        preview=d.get("preview") or "",
                        step_count=int(d.get("step_count") or 0),
                        last_modified_time=str(d.get("last_modified_time") or ""),
                        workspace_uris=str(d.get("workspace_uris") or ""),
                        status=str(d.get("status") or ""),
                        project_id=str(d.get("project_id") or ""),
                        agent_name=str(d.get("agent_name") or ""),
                        parent_conversation_id=str(d.get("parent_conversation_id") or ""),
                        nesting_depth=int(d.get("nesting_depth") or 0),
                        raw_row=serializable_row,
                        db_size=db_sz,
                        brain_size=brain_sz,
                        annotation_title=ann_title,
                    )
                    records_by_id[cid] = rec
            finally:
                conn.close()

        # Дополнительно проверяем .db файлы в conversations/, которых могло не быть в summaries
        conv_dir = self.agy_dir / "conversations"
        if conv_dir.exists():
            for db_file in conv_dir.glob("*.db"):
                cid = db_file.stem
                if cid not in records_by_id:
                    mtime = datetime.datetime.fromtimestamp(
                        db_file.stat().st_mtime, tz=datetime.timezone.utc
                    ).isoformat()
                    records_by_id[cid] = SessionRecord(
                        conversation_id=cid,
                        title=self._read_annotation_title(cid),
                        preview="",
                        step_count=0,
                        last_modified_time=mtime,
                        workspace_uris="",
                        status="ORPHAN_DB",
                        project_id="",
                        agent_name="",
                        parent_conversation_id="",
                        nesting_depth=0,
                        raw_row={"conversation_id": cid},
                        db_size=db_file.stat().st_size,
                        brain_size=dir_size(self.agy_dir / "brain" / cid),
                        annotation_title=self._read_annotation_title(cid),
                    )

        # Связываем дочерние сессии (сабагенты) с корневыми
        roots: List[SessionRecord] = []
        for cid, rec in records_by_id.items():
            pid = rec.parent_conversation_id
            if pid and pid in records_by_id:
                # Ищем самый верхний корень
                curr_parent = records_by_id[pid]
                visited = {cid}
                while (
                    curr_parent.parent_conversation_id
                    and curr_parent.parent_conversation_id in records_by_id
                    and curr_parent.parent_conversation_id not in visited
                ):
                    visited.add(curr_parent.conversation_id)
                    curr_parent = records_by_id[curr_parent.parent_conversation_id]
                curr_parent.children.append(rec)
            else:
                roots.append(rec)

        roots.sort(key=lambda r: r.last_modified_time, reverse=True)
        for r in roots:
            r.children.sort(key=lambda c: c.last_modified_time)
        return roots

    def get_session_details(self, rec: SessionRecord) -> Dict[str, Any]:
        """Возвращает подробную информацию о сессии: артефакты и последние реплики."""
        brain_path = self.agy_dir / "brain" / rec.conversation_id
        artifacts: List[str] = []
        transcript_preview: List[str] = []

        if brain_path.exists():
            for item in sorted(brain_path.rglob("*")):
                if item.is_file():
                    rel = item.relative_to(brain_path).as_posix()
                    if not rel.startswith(".system_generated/"):
                        artifacts.append(f"{rel} ({format_size(item.stat().st_size)})")

            transcript_file = (
                brain_path / ".system_generated" / "logs" / "transcript.jsonl"
            )
            if transcript_file.exists():
                try:
                    lines = transcript_file.read_text(
                        encoding="utf-8", errors="replace"
                    ).splitlines()
                    for line in lines:
                        if not line.strip():
                            continue
                        obj = json.loads(line)
                        stype = obj.get("type", "")
                        content = (obj.get("content") or "").strip()
                        if stype == "USER_INPUT" and content:
                            clean = content.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()
                            transcript_preview.append(f"👤 Пользователь: {clean[:300]}")
                        elif stype == "PLANNER_RESPONSE" and content:
                            transcript_preview.append(f"🤖 Antigravity: {content[:300]}")
                except Exception:
                    pass

        return {
            "artifacts": artifacts,
            "transcript_preview": transcript_preview[-12:],
        }

    @staticmethod
    def _safe_backup_sqlite(src_db: Path, dst_db: Path) -> None:
        """Безопасное снятие снапшота SQLite (включая незакоммиченные данные из WAL)."""
        dst_db.parent.mkdir(parents=True, exist_ok=True)
        src_conn = sqlite3.connect(str(src_db))
        dst_conn = sqlite3.connect(str(dst_db))
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()

    def export_sessions(
        self,
        sessions: List[SessionRecord],
        output_zip: Path,
        include_children: bool = True,
        include_brain: bool = True,
        exclude_browser_sandbox: bool = False,
        progress_cb: Optional[Callable[[str, float, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Выгружает выбранные сессии в ZIP-архив (.agysession.zip).
        Запись выполняется атомарно через временный файл (.part_<pid>), чтобы при прерывании
        или долгом копировании архив не оставался недописанным и повреждённым.
        """
        all_records: List[SessionRecord] = []
        seen_ids = set()
        for s in sessions:
            if s.conversation_id not in seen_ids:
                all_records.append(s)
                seen_ids.add(s.conversation_id)
            if include_children:
                for ch in s.children:
                    if ch.conversation_id not in seen_ids:
                        all_records.append(ch)
                        seen_ids.add(ch.conversation_id)

        manifest: Dict[str, Any] = {
            "format": "antigravity-session-archive",
            "version": 1,
            "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "source_agy_dir": str(self.agy_dir),
            "include_children": include_children,
            "include_brain": include_brain,
            "root_session_ids": [s.conversation_id for s in sessions],
            "sessions": [],
        }

        output_zip = Path(output_zip)
        output_zip.parent.mkdir(parents=True, exist_ok=True)

        # Атомарный временный файл в том же каталоге
        tmp_zip = output_zip.with_name(f".{output_zip.name}.part_{os.getpid()}")
        if tmp_zip.exists():
            try:
                tmp_zip.unlink()
            except OSError:
                pass

        def report(main_title: str, frac: float, detail: str) -> None:
            if progress_cb:
                try:
                    progress_cb(main_title, frac, detail)
                except Exception:
                    pass

        # Оценка общего объёма для точного прогресс-бара
        total_bytes_est = sum(r.db_size + (r.brain_size if include_brain else 0) for r in all_records) or 1
        processed_bytes = 0

        try:
            with tempfile.TemporaryDirectory(prefix="agy_export_") as tmp_dir_str:
                tmp_dir = Path(tmp_dir_str)
                with zipfile.ZipFile(
                    tmp_zip, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
                ) as zf:
                    for idx, rec in enumerate(all_records, 1):
                        if cancel_event and cancel_event.is_set():
                            raise InterruptedError("Выгрузка отменена пользователем.")

                        cid = rec.conversation_id
                        step_header = f"Выгрузка [{idx}/{len(all_records)}]: {rec.display_title}"
                        frac = min(0.99, processed_bytes / total_bytes_est)
                        report(step_header, frac, "Подготовка метаданных...")

                        manifest["sessions"].append(rec.raw_row)

                        # 1. conversations/<id>.db через sqlite backup API
                        src_db = self.agy_dir / "conversations" / f"{cid}.db"
                        if src_db.exists():
                            if cancel_event and cancel_event.is_set():
                                raise InterruptedError("Выгрузка отменена пользователем.")
                            report(
                                step_header,
                                frac,
                                f"Сжатие базы данных conversations/{cid[:8]}...db ({format_size(rec.db_size)})",
                            )
                            tmp_db = tmp_dir / f"{cid}.db"
                            try:
                                self._safe_backup_sqlite(src_db, tmp_db)
                                zf.write(tmp_db, f"conversations/{cid}.db")
                                tmp_db.unlink(missing_ok=True)
                            except Exception:
                                zf.write(src_db, f"conversations/{cid}.db")
                            processed_bytes += rec.db_size

                        # 2. annotations/<id>.pbtxt
                        src_ann = self.agy_dir / "annotations" / f"{cid}.pbtxt"
                        if src_ann.exists():
                            zf.write(src_ann, f"annotations/{cid}.pbtxt")

                        # 3. chapter_titles/<id>.pb
                        src_chap = self.agy_dir / "chapter_titles" / f"{cid}.pb"
                        if src_chap.exists():
                            zf.write(src_chap, f"chapter_titles/{cid}.pb")

                        # 4. brain/<id>/
                        if include_brain:
                            src_brain = self.agy_dir / "brain" / cid
                            if src_brain.exists():
                                for root, _, files in os.walk(src_brain):
                                    for f in files:
                                        if cancel_event and cancel_event.is_set():
                                            raise InterruptedError("Выгрузка отменена пользователем.")

                                        full_path = Path(root) / f
                                        rel_path = full_path.relative_to(src_brain).as_posix()
                                        if (
                                            exclude_browser_sandbox
                                            and rel_path.startswith("browser_sandbox/")
                                        ):
                                            continue

                                        try:
                                            f_sz = full_path.stat().st_size
                                        except OSError:
                                            f_sz = 0

                                        # Ускорение: уже сжатые медиа пишем без повторной компрессии DEFLATE
                                        ext = full_path.suffix.lower()
                                        comp = (
                                            zipfile.ZIP_STORED
                                            if ext in ALREADY_COMPRESSED_EXTS
                                            else zipfile.ZIP_DEFLATED
                                        )

                                        frac = min(0.99, processed_bytes / total_bytes_est)
                                        report(
                                            step_header,
                                            frac,
                                            f"brain/{cid[:8]}…/{rel_path} ({format_size(f_sz)})",
                                        )
                                        try:
                                            zf.write(
                                                full_path,
                                                f"brain/{cid}/{rel_path}",
                                                compress_type=comp,
                                            )
                                        except OSError:
                                            # Файл временно заблокирован другой программой
                                            pass
                                        processed_bytes += f_sz

                    # Записываем manifest.json
                    report("Завершение", 0.99, "Запись manifest.json и закрытие оглавления архива...")
                    manifest_bytes = json.dumps(
                        manifest, ensure_ascii=False, indent=2
                    ).encode("utf-8")
                    zf.writestr("manifest.json", manifest_bytes)

            # После успешного закрытия архива zf переименовываем временный файл в целевой
            report("Завершение", 1.0, "Архив успешно сформирован.")
            if output_zip.exists():
                try:
                    output_zip.unlink()
                except OSError:
                    pass
            tmp_zip.replace(output_zip)

        except Exception:
            # При любой ошибке или отмене удаляем недописанный временный файл
            if tmp_zip.exists():
                try:
                    tmp_zip.unlink()
                except OSError:
                    pass
            raise

        return {
            "output_zip": str(output_zip),
            "root_count": len(sessions),
            "total_conversations": len(all_records),
            "archive_size": output_zip.stat().st_size,
        }

    @staticmethod
    def inspect_archive(archive_path: Path) -> Dict[str, Any]:
        """
        Проверяет целостность архива и читает manifest.json.
        При ошибках даёт подробные, понятные подсказки на русском языке.
        """
        archive_path = Path(archive_path)
        if not archive_path.exists():
            raise FileNotFoundError(f"Файл не найден: {archive_path}")

        sz = archive_path.stat().st_size
        if sz == 0:
            raise ValueError("Файл архива пуст (размер 0 байт).")

        if not zipfile.is_zipfile(archive_path):
            with open(archive_path, "rb") as f:
                head = f.read(4)
            if head == b"PK\x03\x04":
                raise ValueError(
                    f"Файл {archive_path.name} повреждён или недописан (текущий размер: {format_size(sz)}).\n\n"
                    "Причина: в конце файла отсутствует оглавление ZIP (Central Directory).\n"
                    "Это происходит, если архив был скопирован до полного окончания выгрузки "
                    "или если программа была закрыта в процессе записи.\n\n"
                    "Пожалуйста, повторите выгрузку на исходном ПК и дождитесь появления сообщения «Выгрузка завершена»."
                )
            raise ValueError(
                f"Файл {archive_path.name} не является ZIP-архивом (сигнатура файла не совпадает)."
            )

        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as zf:
            names = zf.namelist()
            if "manifest.json" not in names:
                raise ValueError(
                    f"В архиве {archive_path.name} отсутствует manifest.json.\n"
                    "Этот файл не является архивом сессии Antigravity."
                )
            try:
                manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
            except Exception as e:
                raise ValueError(f"Ошибка парсинга manifest.json в архиве: {e}")

            total_uncompressed = sum(info.file_size for info in zf.infolist())
            manifest["file_count"] = len(names)
            manifest["total_uncompressed_size"] = total_uncompressed
            return manifest

    def import_archive(
        self,
        archive_path: Path,
        conflict_mode: str = "overwrite",  # "overwrite" | "skip"
        backup_existing: bool = True,
        reset_running_status: bool = True,
        progress_cb: Optional[Callable[[str, float, str], None]] = None,
        cancel_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        """
        Заливает (импортирует) сессии из архива в каталог Antigravity.
        """
        manifest = self.inspect_archive(archive_path)
        sessions_meta: List[Dict[str, Any]] = manifest.get("sessions", [])

        def report(main_title: str, frac: float, detail: str) -> None:
            if progress_cb:
                try:
                    progress_cb(main_title, frac, detail)
                except Exception:
                    pass

        # Определяем, какие ID уже существуют
        existing_ids = set()
        if self.summaries_db.exists():
            conn = sqlite3.connect(str(self.summaries_db))
            try:
                for (cid,) in conn.execute(
                    "SELECT conversation_id FROM conversation_summaries"
                ):
                    existing_ids.add(cid)
            finally:
                conn.close()

        for p in (self.agy_dir / "conversations").glob("*.db"):
            existing_ids.add(p.stem)

        to_import_ids = set()
        skipped_ids = set()
        for s_row in sessions_meta:
            cid = s_row.get("conversation_id")
            if not cid:
                continue
            if cid in existing_ids and conflict_mode == "skip":
                skipped_ids.add(cid)
            else:
                to_import_ids.add(cid)

        # Страховочный бэкап перезаписываемых сессий
        backup_zip_path: Optional[Path] = None
        overlapping = to_import_ids.intersection(existing_ids)
        if overlapping and backup_existing:
            report(
                "Резервное копирование",
                0.05,
                "Создание страховочного бэкапа перезаписываемых сессий...",
            )
            all_current = self.list_sessions()
            by_id: Dict[str, SessionRecord] = {}
            for r in all_current:
                by_id[r.conversation_id] = r
                for ch in r.children:
                    by_id[ch.conversation_id] = ch
            to_backup = [by_id[cid] for cid in overlapping if cid in by_id]
            if to_backup:
                backups_dir = self.agy_dir / "session_backups"
                backups_dir.mkdir(parents=True, exist_ok=True)
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                backup_zip_path = backups_dir / f"pre_import_backup_{ts}.agysession.zip"
                self.export_sessions(
                    to_backup,
                    backup_zip_path,
                    include_children=False,
                    include_brain=True,
                )

        # Распаковка файлов
        for subdir in ["conversations", "annotations", "chapter_titles", "brain"]:
            (self.agy_dir / subdir).mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(archive_path, "r", allowZip64=True) as zf:
            members = [
                m
                for m in zf.infolist()
                if m.filename != "manifest.json" and not m.filename.endswith("/")
            ]
            total_members = len(members) or 1

            for idx, member in enumerate(members, 1):
                if cancel_event and cancel_event.is_set():
                    raise InterruptedError("Импорт отменён пользователем.")

                name = member.filename
                parts = name.split("/")
                if len(parts) < 2:
                    continue
                top_folder = parts[0]
                if top_folder in ("conversations", "annotations", "chapter_titles"):
                    cid = Path(parts[1]).stem
                elif top_folder == "brain":
                    cid = parts[1]
                else:
                    continue

                if cid not in to_import_ids:
                    continue

                frac = 0.10 + 0.80 * (idx / total_members)
                report(
                    f"Импорт файлов [{idx}/{total_members}]",
                    frac,
                    f"Распаковка: {name}",
                )

                if top_folder == "conversations":
                    for ext in (".db-wal", ".db-shm"):
                        wal_file = self.agy_dir / "conversations" / f"{cid}{ext}"
                        if wal_file.exists():
                            try:
                                wal_file.unlink()
                            except OSError:
                                pass

                target_path = self.agy_dir / name
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member, "r") as src_f, open(target_path, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)

        # Обновление conversation_summaries.db
        report("Синхронизация данных", 0.92, "Обновление conversation_summaries.db...")
        conn = sqlite3.connect(str(self.summaries_db))
        try:
            cur = conn.cursor()
            cur.execute(
                """CREATE TABLE IF NOT EXISTS `conversation_summaries` (
                    `conversation_id` text,
                    `title` text NOT NULL DEFAULT "",
                    `preview` text NOT NULL DEFAULT "",
                    `step_count` integer NOT NULL DEFAULT 0,
                    `last_modified_time` datetime NOT NULL,
                    `workspace_uris` text NOT NULL,
                    `status` text NOT NULL DEFAULT "",
                    `source` text NOT NULL DEFAULT "",
                    `project_id` text NOT NULL DEFAULT "",
                    `agent_name` text NOT NULL DEFAULT "",
                    `parent_conversation_id` text NOT NULL DEFAULT "",
                    `nesting_depth` integer NOT NULL DEFAULT 0,
                    `battle_id` text NOT NULL DEFAULT "",
                    `winning_conversation_id` text NOT NULL DEFAULT "",
                    `not_fully_idle` numeric NOT NULL DEFAULT false,
                    `killed` numeric NOT NULL DEFAULT false,
                    `last_user_input_time` datetime NOT NULL,
                    `last_user_input_step_index` integer NOT NULL DEFAULT -1,
                    `app_data_dir` text NOT NULL DEFAULT "",
                    `raw_summary` blob,
                    `group_id` text NOT NULL DEFAULT "",
                    PRIMARY KEY (`conversation_id`)
                )"""
            )

            placeholders = ", ".join(["?"] * len(SUMMARY_COLUMNS))
            col_names = ", ".join(f"`{c}`" for c in SUMMARY_COLUMNS)
            sql = f"INSERT OR REPLACE INTO `conversation_summaries` ({col_names}) VALUES ({placeholders})"

            for s_row in sessions_meta:
                cid = s_row.get("conversation_id")
                if not cid or cid not in to_import_ids:
                    continue
                if "last_modified_time" not in s_row:
                    continue

                row_copy = dict(s_row)
                raw_b64 = row_copy.pop("raw_summary_b64", "")
                row_copy["raw_summary"] = (
                    base64.b64decode(raw_b64) if raw_b64 else None
                )

                if reset_running_status:
                    if row_copy.get("status") == "CASCADE_RUN_STATUS_RUNNING":
                        row_copy["status"] = "CASCADE_RUN_STATUS_IDLE"
                    row_copy["not_fully_idle"] = 0

                values = [row_copy.get(col) for col in SUMMARY_COLUMNS]
                cur.execute(sql, values)

            conn.commit()
        finally:
            conn.close()

        # Пересборка agyhub_summaries_proto.pb
        report(
            "Синхронизация данных",
            0.97,
            "Пересборка индекса agyhub_summaries_proto.pb...",
        )
        proto_count = rebuild_agyhub_proto(self.agy_dir)
        report("Завершено", 1.0, f"Импорт завершён успешно ({len(to_import_ids)} сессий).")

        return {
            "imported_count": len(to_import_ids),
            "skipped_count": len(skipped_ids),
            "proto_synced_count": proto_count,
            "backup_zip": str(backup_zip_path) if backup_zip_path else None,
        }

    def export_markdown_report(self, rec: SessionRecord, out_md: Path) -> Path:
        """Экспортирует читаемую историю чата из transcript.jsonl в Markdown."""
        lines_out = [
            f"# {rec.display_title}",
            "",
            f"- **ID сессии**: `{rec.conversation_id}`",
            f"- **Последнее изменение**: `{rec.last_modified_time}`",
            f"- **Шагов**: `{rec.step_count}`",
            f"- **Сабагентов**: `{len(rec.children)}`",
            "",
            "---",
            "",
        ]
        transcript_file = (
            self.agy_dir
            / "brain"
            / rec.conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if transcript_file.exists():
            for raw_line in transcript_file.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines():
                if not raw_line.strip():
                    continue
                try:
                    obj = json.loads(raw_line)
                except Exception:
                    continue
                stype = obj.get("type", "")
                created = obj.get("created_at", "")
                content = (obj.get("content") or "").strip()
                if stype == "USER_INPUT" and content:
                    clean = (
                        content.replace("<USER_REQUEST>", "")
                        .replace("</USER_REQUEST>", "")
                        .strip()
                    )
                    lines_out.append(f"### 👤 Пользователь ({created})")
                    lines_out.append("")
                    lines_out.append(clean)
                    lines_out.append("")
                elif stype == "PLANNER_RESPONSE" and (content or obj.get("tool_calls")):
                    lines_out.append(f"### 🤖 Antigravity ({created})")
                    lines_out.append("")
                    if content:
                        lines_out.append(content)
                        lines_out.append("")
                    tcalls = obj.get("tool_calls") or []
                    if tcalls:
                        names = [tc.get("name", "tool") for tc in tcalls if isinstance(tc, dict)]
                        if names:
                            lines_out.append(f"*Вызовы инструментов: `{', '.join(names)}`*")
                            lines_out.append("")
        else:
            lines_out.append("*Лог transcript.jsonl не найден в каталоге brain этой сессии.*")

        out_md.write_text("\n".join(lines_out), encoding="utf-8")
        return out_md


# ---------------------------------------------------------------------------
# Modal Progress Dialog
# ---------------------------------------------------------------------------

class OperationProgressDialog(tk.Toplevel):
    """
    Модальное окно с подробным индикатором выполнения операции,
    секундомером и защитой от случайного прерывания.
    """

    def __init__(self, parent: tk.Tk, title: str, header_text: str):
        super().__init__(parent)
        self.title(title)
        self.geometry("540x240")
        self.minsize(480, 220)
        self.resizable(False, False)
        self.configure(bg="#0f172a")
        self.transient(parent)
        self.grab_set()

        self.cancel_event = threading.Event()
        self.start_time = time.time()
        self.is_done = False

        # Центрирование относительно родителя
        self.update_idletasks()
        pw = parent.winfo_width()
        ph = parent.winfo_height()
        px = parent.winfo_rootx()
        py = parent.winfo_rooty()
        x = px + (pw - 540) // 2
        y = py + (ph - 240) // 2
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

        frame = ttk.Frame(self, padding=20)
        frame.pack(fill=tk.BOTH, expand=True)

        self.lbl_header = ttk.Label(
            frame,
            text=header_text,
            style="Header.TLabel",
        )
        self.lbl_header.pack(anchor=tk.W, pady=(0, 6))

        self.lbl_main = ttk.Label(
            frame,
            text="Инициализация...",
            font=("Segoe UI Semibold", 10),
        )
        self.lbl_main.pack(anchor=tk.W, pady=(0, 4))

        self.lbl_detail = ttk.Label(
            frame,
            text="",
            style="Muted.TLabel",
        )
        self.lbl_detail.pack(anchor=tk.W, pady=(0, 12))

        self.pbar = ttk.Progressbar(
            frame, orient=tk.HORIZONTAL, mode="determinate", maximum=100
        )
        self.pbar.pack(fill=tk.X, pady=(0, 8))

        bottom_bar = ttk.Frame(frame)
        bottom_bar.pack(fill=tk.X)

        self.lbl_stats = ttk.Label(
            bottom_bar,
            text="0% | Прошло: 00:00",
            style="Muted.TLabel",
        )
        self.lbl_stats.pack(side=tk.LEFT)

        self.btn_cancel = ttk.Button(
            bottom_bar,
            text="Отмена",
            style="Secondary.TButton",
            command=self._on_cancel_click,
        )
        self.btn_cancel.pack(side=tk.RIGHT)

        self.protocol("WM_DELETE_WINDOW", self._on_close_attempt)
        self._update_timer()

    def _update_timer(self) -> None:
        if self.is_done or not self.winfo_exists():
            return
        elapsed = int(time.time() - self.start_time)
        mins, secs = divmod(elapsed, 60)
        curr_val = self.pbar["value"]
        self.lbl_stats.configure(
            text=f"{curr_val:.0f}% | Прошло: {mins:02d}:{secs:02d}"
        )
        self.after(500, self._update_timer)

    def update_progress(self, main_title: str, frac: float, detail: str) -> None:
        if not self.winfo_exists() or self.is_done:
            return
        pct = max(0.0, min(100.0, frac * 100.0))
        self.pbar["value"] = pct
        self.lbl_main.configure(text=main_title)
        # Сокращаем путь для отображения
        if len(detail) > 65:
            detail = detail[:30] + "…" + detail[-32:]
        self.lbl_detail.configure(text=detail)

    def finish(self) -> None:
        self.is_done = True
        self.pbar["value"] = 100
        self.lbl_main.configure(text="Завершено!")
        self.lbl_detail.configure(text="Все операции успешно выполнены.")
        self.btn_cancel.configure(state=tk.DISABLED)
        self.destroy()

    def _on_cancel_click(self) -> None:
        if messagebox.askyesno(
            "Прерывание",
            "Вы уверены, что хотите отменить операцию?\n"
            "Недописанный файл архива будет удалён.",
            parent=self,
        ):
            self.cancel_event.set()
            self.lbl_main.configure(text="Отмена операции...")
            self.btn_cancel.configure(state=tk.DISABLED)

    def _on_close_attempt(self) -> None:
        if self.is_done:
            self.destroy()
            return
        self._on_cancel_click()


# ---------------------------------------------------------------------------
# Modern Tkinter GUI
# ---------------------------------------------------------------------------

class SessionManagerGUI(tk.Tk):
    BG_MAIN = "#0f172a"
    BG_CARD = "#1e293b"
    BG_INPUT = "#0b1120"
    FG_TEXT = "#f8fafc"
    FG_MUTED = "#94a3b8"
    ACCENT = "#38bdf8"
    ACCENT_HOVER = "#0284c7"
    SUCCESS = "#22c55e"
    WARN = "#f59e0b"

    def __init__(self, initial_dir: Path = DEFAULT_AGY_DIR):
        super().__init__()
        self.title("Antigravity Session Manager — Выгрузка и заливка сессий")
        self.geometry("1180x740")
        self.minsize(960, 620)
        self.configure(bg=self.BG_MAIN)

        self.store = AntigravitySessionStore(initial_dir)
        self.sessions: List[SessionRecord] = []
        self.filtered_sessions: List[SessionRecord] = []
        self.checked_ids: Set[str] = set()

        self._setup_styles()
        self._build_ui()
        self.refresh_sessions()

    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=self.BG_MAIN, foreground=self.FG_TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=self.BG_MAIN)
        style.configure("Card.TFrame", background=self.BG_CARD)
        style.configure("TLabel", background=self.BG_MAIN, foreground=self.FG_TEXT)
        style.configure("Card.TLabel", background=self.BG_CARD, foreground=self.FG_TEXT)
        style.configure("Muted.TLabel", background=self.BG_MAIN, foreground=self.FG_MUTED, font=("Segoe UI", 9))
        style.configure("Header.TLabel", background=self.BG_MAIN, foreground=self.FG_TEXT, font=("Segoe UI Semibold", 13))

        style.configure(
            "Accent.TButton",
            background=self.ACCENT,
            foreground="#0f172a",
            font=("Segoe UI Semibold", 10),
            padding=(14, 7),
            borderwidth=0,
        )
        style.map(
            "Accent.TButton",
            background=[("active", self.ACCENT_HOVER), ("disabled", "#334155")],
            foreground=[("disabled", "#64748b")],
        )

        style.configure(
            "Secondary.TButton",
            background="#334155",
            foreground=self.FG_TEXT,
            font=("Segoe UI", 9),
            padding=(10, 6),
            borderwidth=0,
        )
        style.map("Secondary.TButton", background=[("active", "#475569")])

        style.configure(
            "Treeview",
            background=self.BG_CARD,
            fieldbackground=self.BG_CARD,
            foreground=self.FG_TEXT,
            rowheight=30,
            borderwidth=0,
            font=("Segoe UI", 10),
        )
        style.configure(
            "Treeview.Heading",
            background="#334155",
            foreground=self.FG_TEXT,
            font=("Segoe UI Semibold", 9),
            padding=(6, 6),
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", "#0369a1")],
            foreground=[("selected", "#ffffff")],
        )
        style.configure("TCheckbutton", background=self.BG_MAIN, foreground=self.FG_TEXT)
        style.map("TCheckbutton", background=[("active", self.BG_MAIN)])

        style.configure(
            "Horizontal.TProgressbar",
            background=self.ACCENT,
            troughcolor=self.BG_CARD,
            borderwidth=0,
            thickness=10,
        )

    def _build_ui(self) -> None:
        # 1. Верхняя панель
        top_bar = ttk.Frame(self, padding=(16, 14, 16, 8))
        top_bar.pack(fill=tk.X)

        title_box = ttk.Frame(top_bar)
        title_box.pack(side=tk.LEFT)
        ttk.Label(
            title_box,
            text="🚀 Antigravity Session Manager",
            style="Header.TLabel",
        ).pack(anchor=tk.W)
        ttk.Label(
            title_box,
            text="Выгрузка (экспорт) и заливка (импорт) чатов, сабагентов, артефактов brain и индексов",
            style="Muted.TLabel",
        ).pack(anchor=tk.W)

        path_box = ttk.Frame(top_bar)
        path_box.pack(side=tk.RIGHT)
        ttk.Label(path_box, text="Папка Antigravity:", style="Muted.TLabel").pack(
            side=tk.LEFT, padx=(0, 6)
        )
        self.dir_var = tk.StringVar(value=str(self.store.agy_dir))
        dir_entry = tk.Entry(
            path_box,
            textvariable=self.dir_var,
            width=38,
            bg=self.BG_INPUT,
            fg=self.FG_TEXT,
            insertbackground=self.FG_TEXT,
            relief=tk.FLAT,
            font=("Consolas", 9),
        )
        dir_entry.pack(side=tk.LEFT, ipady=5, padx=(0, 6))
        ttk.Button(
            path_box,
            text="Обзор...",
            style="Secondary.TButton",
            command=self._choose_agy_dir,
        ).pack(side=tk.LEFT, padx=(0, 4))
        ttk.Button(
            path_box,
            text="⟳ Обновить",
            style="Secondary.TButton",
            command=self.refresh_sessions,
        ).pack(side=tk.LEFT)

        # 2. Панель поиска и управления выбором
        filter_bar = ttk.Frame(self, padding=(16, 6, 16, 8))
        filter_bar.pack(fill=tk.X)

        ttk.Label(filter_bar, text="🔎 Поиск:").pack(side=tk.LEFT, padx=(0, 6))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._apply_filter())
        search_entry = tk.Entry(
            filter_bar,
            textvariable=self.search_var,
            width=34,
            bg=self.BG_INPUT,
            fg=self.FG_TEXT,
            insertbackground=self.FG_TEXT,
            relief=tk.FLAT,
            font=("Segoe UI", 10),
        )
        search_entry.pack(side=tk.LEFT, ipady=4, padx=(0, 14))

        ttk.Button(
            filter_bar,
            text="☑ Выбрать все",
            style="Secondary.TButton",
            command=self._check_all,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            filter_bar,
            text="☐ Снять выбор",
            style="Secondary.TButton",
            command=self._uncheck_all,
        ).pack(side=tk.LEFT, padx=(0, 16))

        self.inc_children_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            filter_bar,
            text="Включать сабагентов",
            variable=self.inc_children_var,
        ).pack(side=tk.LEFT, padx=(0, 12))

        self.inc_brain_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            filter_bar,
            text="Включать артефакты (brain/)",
            variable=self.inc_brain_var,
        ).pack(side=tk.LEFT)

        ttk.Button(
            filter_bar,
            text="📥 Залить сессию из архива...",
            style="Accent.TButton",
            command=self._open_import_dialog,
        ).pack(side=tk.RIGHT, padx=(8, 0))

        ttk.Button(
            filter_bar,
            text="📤 Выгрузить выбранные (.zip)",
            style="Accent.TButton",
            command=self._export_selected,
        ).pack(side=tk.RIGHT)

        # 3. Основная рабочая область
        paned = tk.PanedWindow(
            self,
            orient=tk.HORIZONTAL,
            bg=self.BG_MAIN,
            sashwidth=6,
            bd=0,
        )
        paned.pack(fill=tk.BOTH, expand=True, padx=16, pady=(4, 8))

        # Левая часть — список сессий
        left_frame = ttk.Frame(paned)
        paned.add(left_frame, width=720)

        cols = ("checked", "title", "steps", "children", "size", "modified", "id")
        self.tree = ttk.Treeview(
            left_frame,
            columns=cols,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("checked", text="Выбор")
        self.tree.heading("title", text="Название / Превью сессии")
        self.tree.heading("steps", text="Шагов")
        self.tree.heading("children", text="Агенты")
        self.tree.heading("size", text="Размер")
        self.tree.heading("modified", text="Изменена (UTC)")
        self.tree.heading("id", text="ID сессии")

        self.tree.column("checked", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("title", width=265, anchor=tk.W)
        self.tree.column("steps", width=65, anchor=tk.E, stretch=False)
        self.tree.column("children", width=65, anchor=tk.CENTER, stretch=False)
        self.tree.column("size", width=85, anchor=tk.E, stretch=False)
        self.tree.column("modified", width=135, anchor=tk.CENTER, stretch=False)
        self.tree.column("id", width=110, anchor=tk.W, stretch=False)

        vsb = ttk.Scrollbar(left_frame, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        self.tree.bind("<Button-1>", self._on_tree_click)
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<space>", self._on_tree_space)

        # Правая часть — карточка деталей выбранной сессии
        right_frame = ttk.Frame(paned, style="Card.TFrame", padding=12)
        paned.add(right_frame, width=410)

        ttk.Label(
            right_frame,
            text="Детали сессии и превью диалога",
            style="Card.TLabel",
            font=("Segoe UI Semibold", 11),
        ).pack(anchor=tk.W, pady=(0, 8))

        btn_row = ttk.Frame(right_frame, style="Card.TFrame")
        btn_row.pack(fill=tk.X, pady=(0, 8))

        ttk.Button(
            btn_row,
            text="📤 Выгрузить эту",
            style="Secondary.TButton",
            command=self._export_current_single,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            btn_row,
            text="📝 Экспорт в .MD",
            style="Secondary.TButton",
            command=self._export_current_md,
        ).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(
            btn_row,
            text="📂 Папка brain",
            style="Secondary.TButton",
            command=self._open_current_brain_folder,
        ).pack(side=tk.LEFT)

        self.detail_text = tk.Text(
            right_frame,
            bg=self.BG_INPUT,
            fg=self.FG_TEXT,
            insertbackground=self.FG_TEXT,
            relief=tk.FLAT,
            wrap=tk.WORD,
            font=("Consolas", 9),
            padx=10,
            pady=10,
        )
        self.detail_text.pack(fill=tk.BOTH, expand=True)

        # 4. Статус-бар внизу
        status_bar = ttk.Frame(self, padding=(16, 4, 16, 10))
        status_bar.pack(fill=tk.X)
        self.status_var = tk.StringVar(value="Готово.")
        ttk.Label(status_bar, textvariable=self.status_var, style="Muted.TLabel").pack(
            side=tk.LEFT
        )
        ttk.Button(
            status_bar,
            text="🔧 Пересобрать индекс agyhub_summaries_proto.pb",
            style="Secondary.TButton",
            command=self._manual_rebuild_proto,
        ).pack(side=tk.RIGHT)

    # -----------------------------------------------------------------------
    # UI Callbacks & Actions
    # -----------------------------------------------------------------------

    def _choose_agy_dir(self) -> None:
        chosen = filedialog.askdirectory(
            title="Выберите папку данных Antigravity (.gemini/antigravity)",
            initialdir=self.dir_var.get(),
        )
        if chosen:
            self.dir_var.set(chosen)
            self.refresh_sessions()

    def refresh_sessions(self) -> None:
        self.store = AntigravitySessionStore(Path(self.dir_var.get()))
        if not self.store.is_valid():
            self.status_var.set(
                f"⚠ Папка {self.store.agy_dir} не содержит данных Antigravity."
            )
            self.sessions = []
            self._apply_filter()
            return

        self.sessions = self.store.list_sessions()
        valid_ids = {s.conversation_id for s in self.sessions}
        self.checked_ids.intersection_update(valid_ids)
        self._apply_filter()

        total_size = sum(s.total_size for s in self.sessions)
        self.status_var.set(
            f"Найдено сессий: {len(self.sessions)} | Общий объём: {format_size(total_size)} | Выбрано: {len(self.checked_ids)}"
        )

    def _apply_filter(self) -> None:
        query = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        self.filtered_sessions = []

        for s in self.sessions:
            haystack = f"{s.display_title} {s.conversation_id} {s.workspace_uris} {s.project_id}".lower()
            if query and query not in haystack:
                continue
            self.filtered_sessions.append(s)
            chk = "☑" if s.conversation_id in self.checked_ids else "☐"
            self.tree.insert(
                "",
                tk.END,
                iid=s.conversation_id,
                values=(
                    chk,
                    s.display_title,
                    s.step_count,
                    len(s.children),
                    format_size(s.total_size),
                    s.short_date,
                    s.conversation_id[:8] + "…",
                ),
            )

        if self.filtered_sessions:
            first_id = self.filtered_sessions[0].conversation_id
            self.tree.selection_set(first_id)
            self._show_session_details(self.filtered_sessions[0])

    def _toggle_check(self, cid: str) -> None:
        if cid in self.checked_ids:
            self.checked_ids.remove(cid)
        else:
            self.checked_ids.add(cid)
        if self.tree.exists(cid):
            vals = list(self.tree.item(cid, "values"))
            vals[0] = "☑" if cid in self.checked_ids else "☐"
            self.tree.item(cid, values=vals)
        self.status_var.set(
            f"Выбрано сессий для выгрузки: {len(self.checked_ids)} из {len(self.sessions)}"
        )

    def _on_tree_click(self, event: tk.Event) -> None:
        region = self.tree.identify_region(event.x, event.y)
        col = self.tree.identify_column(event.x)
        row_id = self.tree.identify_row(event.y)
        if region == "cell" and col == "#1" and row_id:
            self._toggle_check(row_id)

    def _on_tree_space(self, _event: tk.Event) -> None:
        sel = self.tree.selection()
        if sel:
            self._toggle_check(sel[0])

    def _check_all(self) -> None:
        for s in self.filtered_sessions:
            self.checked_ids.add(s.conversation_id)
        self._apply_filter()
        self.status_var.set(f"Выбрано сессий: {len(self.checked_ids)}")

    def _uncheck_all(self) -> None:
        self.checked_ids.clear()
        self._apply_filter()
        self.status_var.set("Выбор снят.")

    def _get_selected_record(self) -> Optional[SessionRecord]:
        sel = self.tree.selection()
        if not sel:
            return None
        cid = sel[0]
        for s in self.sessions:
            if s.conversation_id == cid:
                return s
        return None

    def _on_tree_select(self, _event: tk.Event) -> None:
        rec = self._get_selected_record()
        if rec:
            self._show_session_details(rec)

    def _show_session_details(self, rec: SessionRecord) -> None:
        info = self.store.get_session_details(rec)
        lines = [
            f"Название:     {rec.display_title}",
            f"ID сессии:    {rec.conversation_id}",
            f"Статус:       {rec.status}",
            f"Изменена:     {rec.last_modified_time}",
            f"Шагов:        {rec.step_count}",
            f"Размер БД:    {format_size(rec.db_size)}",
            f"Размер brain: {format_size(rec.brain_size)}",
            f"Сабагентов:   {len(rec.children)} (общий вес: {format_size(rec.total_size)})",
        ]
        if rec.workspace_uris:
            lines.append(f"Workspace:    {rec.workspace_uris}")

        if rec.children:
            lines.append("\n--- Дочерние сабагенты ---")
            for ch in rec.children:
                lines.append(
                    f" • [{ch.agent_name or 'subagent'}] {ch.conversation_id[:8]}… ({ch.step_count} шаг., {format_size(ch.db_size + ch.brain_size)})"
                )

        artifacts = info.get("artifacts", [])
        lines.append(f"\n--- Артефакты в brain ({len(artifacts)}) ---")
        if artifacts:
            for a in artifacts[:25]:
                lines.append(f" • {a}")
            if len(artifacts) > 25:
                lines.append(f"   ... и ещё {len(artifacts) - 25} файлов")
        else:
            lines.append(" (нет пользовательских файлов)")

        preview = info.get("transcript_preview", [])
        lines.append("\n--- Фрагменты диалога ---")
        if preview:
            for msg in preview:
                lines.append(msg)
                lines.append("")
        else:
            lines.append(" (история сообщений в transcript.jsonl пуста или недоступна)")

        self.detail_text.configure(state=tk.NORMAL)
        self.detail_text.delete("1.0", tk.END)
        self.detail_text.insert(tk.END, "\n".join(lines))
        self.detail_text.configure(state=tk.DISABLED)

    def _export_current_single(self) -> None:
        rec = self._get_selected_record()
        if not rec:
            messagebox.showwarning("Нет выбора", "Выберите сессию в таблице.")
            return
        self._run_export_flow([rec])

    def _export_selected(self) -> None:
        targets = [s for s in self.sessions if s.conversation_id in self.checked_ids]
        if not targets:
            rec = self._get_selected_record()
            if rec:
                if messagebox.askyesno(
                    "Выгрузка текущей сессии",
                    f"Галочки не отмечены.\nВыгрузить текущую выделенную сессию «{rec.display_title}»?",
                ):
                    targets = [rec]
            if not targets:
                return
        self._run_export_flow(targets)

    def _run_export_flow(self, targets: List[SessionRecord]) -> None:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        if len(targets) == 1:
            slug = "".join(
                c if c.isalnum() or c in "-_" else "_"
                for c in targets[0].display_title[:30]
            ).strip("_")
            default_name = f"agy_session_{slug}_{targets[0].conversation_id[:8]}_{ts}.agysession.zip"
        else:
            default_name = f"agy_sessions_{len(targets)}items_{ts}.agysession.zip"

        out_path = filedialog.asksaveasfilename(
            title="Сохранить архив сессий Antigravity",
            initialfile=default_name,
            defaultextension=".zip",
            filetypes=[
                ("Архив сессий Antigravity (*.agysession.zip)", "*.agysession.zip"),
                ("ZIP архив (*.zip)", "*.zip"),
            ],
        )
        if not out_path:
            return

        inc_children = self.inc_children_var.get()
        inc_brain = self.inc_brain_var.get()

        # Создаём модальное окно прогресса
        prog_dlg = OperationProgressDialog(
            self,
            title="Выгрузка сессий Antigravity",
            header_text="📤 Создание архива сессий...",
        )

        def worker() -> None:
            try:
                res = self.store.export_sessions(
                    targets,
                    Path(out_path),
                    include_children=inc_children,
                    include_brain=inc_brain,
                    progress_cb=lambda main_t, frac, det: self.after(
                        0, lambda m=main_t, f=frac, d=det: prog_dlg.update_progress(m, f, d)
                    ),
                    cancel_event=prog_dlg.cancel_event,
                )
                def on_success() -> None:
                    prog_dlg.finish()
                    self.status_var.set(
                        f"✅ Выгружено: {res['root_count']} сессий (всего с сабагентами: {res['total_conversations']}), размер: {format_size(res['archive_size'])}"
                    )
                    messagebox.showinfo(
                        "Выгрузка завершена",
                        f"Архив успешно сохранён:\n{res['output_zip']}\n\n"
                        f"Корневых сессий: {res['root_count']}\n"
                        f"Всего траекторий (с сабагентами): {res['total_conversations']}\n"
                        f"Итоговый размер архива: {format_size(res['archive_size'])}",
                    )
                self.after(0, on_success)
            except InterruptedError:
                self.after(
                    0,
                    lambda: (
                        prog_dlg.finish(),
                        self.status_var.set("Выгрузка отменена пользователем."),
                    ),
                )
            except Exception as exc:
                self.after(
                    0,
                    lambda e=exc: (
                        prog_dlg.finish(),
                        messagebox.showerror(
                            "Ошибка выгрузки", f"Не удалось выгрузить сессии:\n{e}"
                        ),
                    ),
                )

        threading.Thread(target=worker, daemon=True).start()

    def _export_current_md(self) -> None:
        rec = self._get_selected_record()
        if not rec:
            return
        default_name = f"transcript_{rec.conversation_id[:8]}.md"
        out_path = filedialog.asksaveasfilename(
            title="Экспортировать переписку в Markdown",
            initialfile=default_name,
            defaultextension=".md",
            filetypes=[("Markdown (*.md)", "*.md")],
        )
        if not out_path:
            return
        try:
            self.store.export_markdown_report(rec, Path(out_path))
            self.status_var.set(f"✅ Markdown сохранён: {out_path}")
            messagebox.showinfo("Готово", f"Диалог сохранён в файл:\n{out_path}")
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _open_current_brain_folder(self) -> None:
        rec = self._get_selected_record()
        if not rec:
            return
        brain_dir = self.store.agy_dir / "brain" / rec.conversation_id
        if not brain_dir.exists():
            messagebox.showinfo(
                "Папка не найдена", f"Каталог {brain_dir} ещё не создан."
            )
            return
        os.startfile(str(brain_dir))

    def _manual_rebuild_proto(self) -> None:
        try:
            cnt = rebuild_agyhub_proto(self.store.agy_dir)
            self.status_var.set(
                f"✅ Файл agyhub_summaries_proto.pb успешно синхронизирован ({cnt} записей)."
            )
            messagebox.showinfo(
                "Синхронизация завершена",
                f"Индекс agyhub_summaries_proto.pb обновлён из БД.\nЗаписано траекторий: {cnt}",
            )
        except Exception as exc:
            messagebox.showerror("Ошибка", str(exc))

    def _open_import_dialog(self) -> None:
        archive_file = filedialog.askopenfilename(
            title="Выберите архив сессии Antigravity для заливки",
            filetypes=[
                ("Архивы сессий (*.zip; *.agysession.zip)", "*.zip"),
                ("Все файлы (*.*)", "*.*"),
            ],
        )
        if not archive_file:
            return

        try:
            manifest = self.store.inspect_archive(Path(archive_file))
        except Exception as exc:
            messagebox.showerror("Ошибка чтения архива", str(exc))
            return

        dlg = tk.Toplevel(self)
        dlg.title("Заливка (импорт) сессий из архива")
        dlg.geometry("620x460")
        dlg.configure(bg=self.BG_MAIN)
        dlg.transient(self)
        dlg.grab_set()

        frame = ttk.Frame(dlg, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(
            frame,
            text="📥 Параметры заливки архива",
            style="Header.TLabel",
        ).pack(anchor=tk.W, pady=(0, 8))

        sessions_in_arc = manifest.get("sessions", [])
        roots_in_arc = manifest.get("root_session_ids", [])

        info_box = tk.Text(
            frame,
            height=10,
            bg=self.BG_CARD,
            fg=self.FG_TEXT,
            relief=tk.FLAT,
            font=("Consolas", 9),
            padx=10,
            pady=8,
        )
        info_box.pack(fill=tk.BOTH, expand=True, pady=(0, 12))

        summary_lines = [
            f"Файл архива:         {Path(archive_file).name}",
            f"Дата выгрузки:       {manifest.get('exported_at', '—')}",
            f"Корневых сессий:     {len(roots_in_arc)}",
            f"Всего траекторий:    {len(sessions_in_arc)} (файлов в архиве: {manifest.get('file_count', 0)})",
            f"Распакованный объём: {format_size(manifest.get('total_uncompressed_size', 0))}",
            "",
            "Сессии в архиве:",
        ]
        for s in sessions_in_arc:
            if s.get("nesting_depth", 0) == 0 or s.get("conversation_id") in roots_in_arc:
                t = s.get("title") or s.get("preview") or "[Без названия]"
                summary_lines.append(
                    f" • {t} ({s.get('conversation_id', '')[:8]}…, шагов: {s.get('step_count', 0)})"
                )
        info_box.insert(tk.END, "\n".join(summary_lines))
        info_box.configure(state=tk.DISABLED)

        conflict_var = tk.StringVar(value="overwrite")
        backup_var = tk.BooleanVar(value=True)
        reset_status_var = tk.BooleanVar(value=True)

        opts = ttk.Frame(frame)
        opts.pack(fill=tk.X, pady=(0, 12))

        ttk.Label(opts, text="При совпадении ID сессии:").grid(
            row=0, column=0, sticky=tk.W, pady=2
        )
        ttk.Radiobutton(
            opts,
            text="Перезаписать и обновить сессию (рекомендуется)",
            variable=conflict_var,
            value="overwrite",
        ).grid(row=1, column=0, sticky=tk.W, padx=12)
        ttk.Radiobutton(
            opts,
            text="Пропустить уже существующие сессии",
            variable=conflict_var,
            value="skip",
        ).grid(row=2, column=0, sticky=tk.W, padx=12)

        ttk.Checkbutton(
            opts,
            text="Создать авто-бэкап существующих сессий перед перезаписью (в session_backups/)",
            variable=backup_var,
        ).grid(row=3, column=0, sticky=tk.W, pady=(8, 2))

        ttk.Checkbutton(
            opts,
            text="Сбросить статус RUNNING -> IDLE (чтобы залитая сессия не зависала в статусе выполнения)",
            variable=reset_status_var,
        ).grid(row=4, column=0, sticky=tk.W, pady=2)

        btn_bar = ttk.Frame(frame)
        btn_bar.pack(fill=tk.X)

        def start_import() -> None:
            mode = conflict_var.get()
            do_backup = backup_var.get()
            do_reset = reset_status_var.get()
            dlg.destroy()

            prog_dlg = OperationProgressDialog(
                self,
                title="Заливка архива Antigravity",
                header_text="📥 Заливка сессий в Antigravity...",
            )

            def worker() -> None:
                try:
                    res = self.store.import_archive(
                        Path(archive_file),
                        conflict_mode=mode,
                        backup_existing=do_backup,
                        reset_running_status=do_reset,
                        progress_cb=lambda main_t, frac, det: self.after(
                            0, lambda m=main_t, f=frac, d=det: prog_dlg.update_progress(m, f, d)
                        ),
                        cancel_event=prog_dlg.cancel_event,
                    )
                    def on_done() -> None:
                        prog_dlg.finish()
                        self.refresh_sessions()
                        backup_note = (
                            f"\nСтраховочный бэкап сохранён в:\n{res['backup_zip']}"
                            if res.get("backup_zip")
                            else ""
                        )
                        messagebox.showinfo(
                            "Заливка успешно завершена",
                            f"Импортировано траекторий: {res['imported_count']}\n"
                            f"Пропущено: {res['skipped_count']}\n"
                            f"Синхронизировано в agyhub_summaries_proto.pb: {res['proto_synced_count']}"
                            f"{backup_note}",
                        )
                    self.after(0, on_done)
                except InterruptedError:
                    self.after(
                        0,
                        lambda: (
                            prog_dlg.finish(),
                            self.status_var.set("Импорт отменён пользователем."),
                        ),
                    )
                except Exception as exc:
                    self.after(
                        0,
                        lambda e=exc: (
                            prog_dlg.finish(),
                            messagebox.showerror(
                                "Ошибка импорта", f"Не удалось залить архив:\n{e}"
                            ),
                        ),
                    )

            threading.Thread(target=worker, daemon=True).start()

        ttk.Button(
            btn_bar,
            text="Отмена",
            style="Secondary.TButton",
            command=dlg.destroy,
        ).pack(side=tk.RIGHT, padx=(8, 0))
        ttk.Button(
            btn_bar,
            text="🚀 Залить сессии",
            style="Accent.TButton",
            command=start_import,
        ).pack(side=tk.RIGHT)


# ---------------------------------------------------------------------------
# CLI Interface
# ---------------------------------------------------------------------------

def run_cli(args: argparse.Namespace) -> int:
    store = AntigravitySessionStore(Path(args.agy_dir))
    if args.command == "list":
        sessions = store.list_sessions()
        print(f"Каталог Antigravity: {store.agy_dir}")
        print(f"Найдено корневых сессий: {len(sessions)}\n")
        print(f"{'ID сессии':<38} | {'Шагов':>6} | {'Агенты':>6} | {'Размер':>10} | {'Изменена':<19} | Название")
        print("-" * 115)
        for s in sessions:
            print(
                f"{s.conversation_id:<38} | {s.step_count:>6} | {len(s.children):>6} | {format_size(s.total_size):>10} | {s.short_date:<19} | {s.display_title}"
            )
        return 0

    if args.command == "export":
        sessions = store.list_sessions()
        if args.all:
            targets = sessions
        else:
            wanted = set(args.ids or [])
            targets = [
                s
                for s in sessions
                if s.conversation_id in wanted
                or any(s.conversation_id.startswith(w) for w in wanted)
            ]
        if not targets:
            print("Не найдено подходящих сессий для выгрузки.", file=sys.stderr)
            return 1

        out_path = Path(args.output)
        res = store.export_sessions(
            targets,
            out_path,
            include_children=not args.no_children,
            include_brain=not args.no_brain,
            progress_cb=lambda main_t, frac, det: print(f"[{frac*100:5.1f}%] {main_t} -> {det}"),
        )
        print(
            f"\nГотово! Выгружено {res['root_count']} корневых сессий (всего {res['total_conversations']}) -> {res['output_zip']} ({format_size(res['archive_size'])})"
        )
        return 0

    if args.command == "import":
        res = store.import_archive(
            Path(args.archive),
            conflict_mode="skip" if args.skip_existing else "overwrite",
            backup_existing=not args.no_backup,
            progress_cb=lambda main_t, frac, det: print(f"[{frac*100:5.1f}%] {main_t} -> {det}"),
        )
        print(
            f"\nГотово! Импортировано: {res['imported_count']}, пропущено: {res['skipped_count']}, индекс обновлён ({res['proto_synced_count']} записей)."
        )
        if res.get("backup_zip"):
            print(f"Страховочный бэкап: {res['backup_zip']}")
        return 0

    if args.command == "inspect":
        manifest = store.inspect_archive(Path(args.archive))
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Утилита для выгрузки и заливки сессий Google Antigravity (GUI + CLI)"
    )
    parser.add_argument(
        "--agy-dir",
        default=str(DEFAULT_AGY_DIR),
        help="Путь к папке .gemini/antigravity",
    )
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("gui", help="Запустить графический интерфейс (по умолчанию)")
    sub.add_parser("list", help="Вывести список всех сессий в консоль")

    exp = sub.add_parser("export", help="Выгрузить одну или несколько сессий в .zip")
    exp.add_argument("ids", nargs="*", help="ID сессий (или первые символы ID)")
    exp.add_argument("--all", action="store_true", help="Выгрузить все сессии")
    exp.add_argument(
        "-o", "--output", required=True, help="Путь к выходному .zip файлу"
    )
    exp.add_argument(
        "--no-children", action="store_true", help="Не включать дочерних сабагентов"
    )
    exp.add_argument(
        "--no-brain", action="store_true", help="Не включать папку артефактов brain/"
    )

    imp = sub.add_parser("import", help="Залить сессии из .zip архива")
    imp.add_argument("archive", help="Путь к архиву .agysession.zip / .zip")
    imp.add_argument(
        "--skip-existing",
        action="store_true",
        help="Не перезаписывать существующие сессии",
    )
    imp.add_argument(
        "--no-backup",
        action="store_true",
        help="Не делать резервную копию перед перезаписью",
    )

    insp = sub.add_parser("inspect", help="Показать содержимое архива сессии")
    insp.add_argument("archive", help="Путь к архиву")

    return parser


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()

    if not args.command or args.command == "gui":
        app = SessionManagerGUI(Path(args.agy_dir))
        app.mainloop()
        return 0

    return run_cli(args)


if __name__ == "__main__":
    raise SystemExit(main())
